"""Supervisor — dispatches to the deterministic create flow or the agentic flow.

Only the four CREATE operations (create_tenant, create_bd, bind_epg,
define_port) are deterministic. Everything else — all reads, lookups, and
troubleshooting — goes through the agentic flow, which is driven by the
markdown skills served by the `skills` MCP server.
"""
import re

from .nodes_common import (recognize_intent, set_tools, interpret_operation,
                           resolve_node_name, detect_switch_convention)
from .graph_create import build_create_graph, build_prepare_graph, build_execute_graph
from .graph_agentic import build_agentic_graph
from .agentic_aci_kb import aci_lookup_class
from .mgmt_ports import find_management_ports
from .chat_memory import normalize_chat_history
from .mcp_runtime import get_runtime_manager
from .nodes_agentic import MAX_AGENT_ITERATIONS
from .nodes_create import (build_static_binding, resolve_epg_location, resolve_epg_encap,
                           read_bindings_on_path, build_copy_bindings, resolve_vmm_domain_epgs,
                           _path_tdn)
from .nodes_port import (build_port_definition, resolve_switch_profile,
                         resolve_port_ref, read_port_template)

AGENTIC_RECURSION_LIMIT = MAX_AGENT_ITERATIONS + 12

async def _load_tools(fabric):
    """Publish this request's tool registry into the task-local ContextVar.

    No spawn happens here: the persistent runtime returns an already-open,
    session-bound tool dict; we only add the in-process aci_kb lookup tool
    (used by the agentic flow, no MCP hop) and set it for the current task.
    """
    registry = await get_runtime_manager().get_registry(fabric)
    registry[aci_lookup_class.name] = aci_lookup_class
    registry[find_management_ports.name] = find_management_ports
    set_tools(registry)

def _new_state(user_input, chat_history=None):
    return {"user_input": user_input, "errors": [], "trail": [],
            "validation_attempts": 0, "agent_messages": [], "agent_iterations": 0,
            "chat_history": normalize_chat_history(chat_history)}

async def _resolve_node_list(nodes, trail, convention=None):
    """Resolve any non-numeric entries in *nodes* to numeric node IDs via
    resolve_node_name. Returns (resolved_nodes, error_state_or_None)."""
    resolved = []
    for n in nodes:
        #if between 1-9999 goes for node id
        if re.match(r"^\d{1,4}$", str(n).strip()):
            resolved.append(str(int(str(n).strip())))
        else:
            #resolves node id from switch name
            res = await resolve_node_name(n, convention)
            if res.get("error"):
                return None, res["error"]
            resolved.append(res["node_id"])
            trail.append(f"resolve_node_name: {n} -> {res['node_id']} ({res.get('node_name', '')})")
    return resolved, None

async def _resolve_port_bindings(bindings, intent, user_input, trail):
    """Expand serial/full-name port references to real path tDn(s). A single
    reference may expand to MANY ports: dual-homed access hosts produce one tDn
    per (leaf x port); PC/vPC produce one bundle tDn. Explicit {node,port} /
    {node,node2,policy_group} entries pass through unchanged.

    Returns (resolved_bindings, None) on success, or (None, error_state) on
    failure. `trail` is appended to in place.
    """
    resolved = []
    for b in bindings:
        if not isinstance(b, dict):
            return None, {"flow": "create", "intent": intent, "user_input": user_input,
                          "errors": [f"Invalid port reference: {b!r}"],
                          "trail": trail + ["resolve_port: invalid"]}
        ref = b.get("serial") or b.get("name")
        has_explicit = b.get("tdn") or (b.get("node") and (b.get("port") or b.get("policy_group") or b.get("vpc")))
        if ref and not has_explicit:
            pref = {"serial": ref}
            for k in ("scope", "nodes"):
                if b.get(k):
                    pref[k] = b[k]
            res = await resolve_port_ref(pref)
            if res.get("error"):
                return None, {"flow": "create", "intent": intent, "user_input": user_input,
                              "errors": [f"Port lookup failed for '{ref}': {res['error']}"],
                              "trail": trail + ["resolve_port: error"]}
            tdns = res.get("tdns") or ([res["tdn"]] if res.get("tdn") else [])
            for t in tdns:
                resolved.append({"tdn": t, "label": res.get("label", ref)})
            trail.append(f"resolve_port: {ref} -> {res.get('label', ref)} ({len(tdns)} path(s))")
        else:
            resolved.append(b)
    return resolved, None

async def _build_binding(params, user_input):
    epg_names = params.get("epgs")
    if not epg_names:
        single = params.get("epg")
        epg_names = [single] if single else []
    epg_names = [e for e in epg_names if e]

    bindings = params.get("bindings")
    if not bindings:
        single = {k: params[k] for k in ("node", "port", "node2", "policy_group", "vpc", "serial", "name", "scope") if params.get(k)}
        bindings = [single] if single else []

    mode = params.get("mode", "regular")

    if not epg_names:
        return {"flow": "create", "intent": "bind_epg", "user_input": user_input,
                "errors": ["No EPG specified."], "trail": ["bind: no epg"]}
    if not bindings:
        return {"flow": "create", "intent": "bind_epg", "user_input": user_input,
                "errors": ["No port specified."], "trail": ["bind: no port"]}

    epg_specs = []
    trail = []
    for name in epg_names:
        res = await resolve_epg_location(name)
        if res.get("error"):
            return {"flow": "create", "intent": "bind_epg", "user_input": user_input,
                    "errors": [f"EPG lookup failed for '{name}': {res['error']}"],
                    "trail": trail + ["resolve_epg: error"]}
        matches = res.get("matches", [])
        if len(matches) == 0:
            return {"flow": "create", "intent": "bind_epg", "user_input": user_input,
                    "errors": [f"EPG '{name}' not found in the fabric."],
                    "trail": trail + ["resolve_epg: not found"]}
        if len(matches) > 1:
            locs = ", ".join(f"{m['tenant']}/{m['ap']}" for m in matches)
            return {"flow": "create", "intent": "bind_epg", "user_input": user_input,
                    "errors": [f"EPG '{name}' is ambiguous: {locs}. Be more specific."],
                    "trail": trail + ["resolve_epg: ambiguous"]}
        m = matches[0]
        encap, source, eerr = await resolve_epg_encap(
            m["tenant"], m["ap"], m["epg"], params.get("encap"))
        if eerr:
            return {"flow": "create", "intent": "bind_epg", "user_input": user_input,
                    "errors": [eerr], "trail": trail + [f"resolve_encap: failed for {name}"]}
        epg_specs.append({"tenant": m["tenant"], "ap": m["ap"], "epg": m["epg"], "encap": encap})
        trail.append(f"resolve_epg: {m['epg']} -> {m['tenant']}/{m['ap']} {encap} (via {source})")

    # Resolve serial / full-name port references to real path tDn(s). A single
    # reference may expand to MANY ports: dual-homed access hosts produce one
    # tDn per (leaf x port); PC/vPC produce one bundle tDn. Explicit
    # {node,port} / {node,node2,policy_group} entries pass through unchanged.
    # -- Resolve switch-name fragments in binding "node" fields --
    for b in bindings:
        if isinstance(b, dict) and "node" in b and not re.match(r"^\d{1,4}$", str(b["node"]).strip()):
            nres = await resolve_node_name(b["node"])
            if nres.get("error"):
                return {"flow": "create", "intent": "bind_epg", "user_input": user_input,
                        "errors": [f"Node name resolution failed for '{b['node']}': {nres['error']}"],
                        "trail": trail + ["resolve_node_name: error"]}
            trail.append(f"resolve_node_name: {b['node']} -> {nres['node_id']} ({nres.get('node_name', '')})")
            b["node"] = nres["node_id"]
        if isinstance(b, dict) and "node2" in b and not re.match(r"^\d{1,4}$", str(b["node2"]).strip()):
            nres = await resolve_node_name(b["node2"])
            if nres.get("error"):
                return {"flow": "create", "intent": "bind_epg", "user_input": user_input,
                        "errors": [f"Node name resolution failed for '{b['node2']}': {nres['error']}"],
                        "trail": trail + ["resolve_node_name: error"]}
            trail.append(f"resolve_node_name: {b['node2']} -> {nres['node_id']} ({nres.get('node_name', '')})")
            b["node2"] = nres["node_id"]

    resolved_bindings, err_state = await _resolve_port_bindings(
        bindings, "bind_epg", user_input, trail)
    if err_state:
        return err_state

    prep = build_static_binding(epg_specs, resolved_bindings, mode, user_input)
    prep["trail"] = trail + prep.get("trail", [])
    return prep

async def _build_vmm_binding(params, user_input):
    """Static-bind EVERY EPG of a VMM (VDS) domain to the given port(s).

    The EPGs and their encap VLANs are discovered from the domain via
    resolve_vmm_domain_epgs (fvRsDomAtt.encap, then deployed fvIfConn, then the
    EPG name). The user only names the domain and the port(s).
    """
    intent = "bind_vmm_domain"
    dom = params.get("vmm_domain") or params.get("domain") or params.get("vds") or params.get("dvs")
    if not dom:
        return {"flow": "create", "intent": intent, "user_input": user_input,
                "errors": ["No VMM domain specified."], "trail": ["bind_vmm: no domain"]}

    bindings = params.get("bindings")
    if not bindings:
        single = {k: params[k] for k in ("node", "port", "node2", "policy_group", "vpc", "serial", "name", "scope") if params.get(k)}
        bindings = [single] if single else []
    if not bindings:
        return {"flow": "create", "intent": intent, "user_input": user_input,
                "errors": ["No port specified."], "trail": ["bind_vmm: no port"]}

    mode = params.get("mode", "regular")
    vmmp = params.get("vmm_provider", "VMware")

    trail = []
    res = await resolve_vmm_domain_epgs(dom, vmmp)
    if res.get("error"):
        return {"flow": "create", "intent": intent, "user_input": user_input,
                "errors": [f"VMM domain lookup failed for '{dom}': {res['error']}"],
                "trail": trail + ["resolve_vmm_domain: error"]}
    epg_specs = res.get("specs", [])
    unresolved = res.get("unresolved", [])
    if not epg_specs:
        msg = f"No bindable EPGs found on VMM domain '{dom}'."
        if unresolved:
            msg += f" {len(unresolved)} EPG(s) had an unresolvable encap VLAN."
        return {"flow": "create", "intent": intent, "user_input": user_input,
                "errors": [msg], "trail": trail + ["resolve_vmm_domain: empty"]}
    trail.append(f"resolve_vmm_domain: {dom} -> {len(epg_specs)} EPG(s)"
                 + (f", {len(unresolved)} unresolved" if unresolved else ""))

    # -- Resolve switch-name fragments in binding "node" fields --
    for b in bindings:
        if isinstance(b, dict) and "node" in b and not re.match(r"^\d{1,4}$", str(b["node"]).strip()):
            nres = await resolve_node_name(b["node"])
            if nres.get("error"):
                return {"flow": "create", "intent": intent, "user_input": user_input,
                        "errors": [f"Node name resolution failed for '{b['node']}': {nres['error']}"],
                        "trail": trail + ["resolve_node_name: error"]}
            trail.append(f"resolve_node_name: {b['node']} -> {nres['node_id']} ({nres.get('node_name', '')})")
            b["node"] = nres["node_id"]
        if isinstance(b, dict) and "node2" in b and not re.match(r"^\d{1,4}$", str(b["node2"]).strip()):
            nres = await resolve_node_name(b["node2"])
            if nres.get("error"):
                return {"flow": "create", "intent": intent, "user_input": user_input,
                        "errors": [f"Node name resolution failed for '{b['node2']}': {nres['error']}"],
                        "trail": trail + ["resolve_node_name: error"]}
            trail.append(f"resolve_node_name: {b['node2']} -> {nres['node_id']} ({nres.get('node_name', '')})")
            b["node2"] = nres["node_id"]

    resolved_bindings, err_state = await _resolve_port_bindings(
        bindings, intent, user_input, trail)
    if err_state:
        return err_state

    prep = build_static_binding(epg_specs, resolved_bindings, mode, user_input)
    prep["intent"] = intent
    for u in unresolved:
        trail.append(f"skipped {u['tenant']}/{u['ap']}/{u['epg']}: {u['reason']}")
    prep["trail"] = trail + prep.get("trail", [])
    return prep

async def _build_port(params, user_input, team="", convention=None):
    ptype = (params.get("port_type") or params.get("type") or "").lower()
    nodes = params.get("nodes") or []
    if isinstance(nodes, (str, int)):
        nodes = [str(nodes)]
    nodes = [str(n) for n in nodes if str(n).strip()]

    def perr(msg, t=None):
        return {"flow": "create", "intent": "define_port", "user_input": user_input,
                "errors": [f"Port definition: {msg}"], "trail": t or ["_build_port: error"]}

    if ptype not in ("access", "pc", "vpc"):
        return perr("port_type must be access, pc, or vpc (state it in the prompt)")
    if not nodes:
        return perr("no node(s) specified")
    if ptype == "vpc" and len(nodes) != 2:
        return perr("vPC requires exactly 2 nodes")
    if ptype == "pc" and len(nodes) != 1:
        return perr("pc requires exactly 1 node")

    trail = []
    # Resolve switch-name fragments to numeric node IDs
    nodes, nerr = await _resolve_node_list(nodes, trail, convention)
    if nerr:
        return perr(f"node name resolution: {nerr}", trail)

    # Apply TEAM_SERIAL naming: if the user gave just a serial (e.g. CX123F421)
    # and the user has a team, prefix it so the name becomes e.g. MSY_CX123F421.
    name = params.get("name", "")
    if team and name and not name.upper().startswith(team.upper() + "_"):
        params = dict(params)
        params["name"] = f"{team}_{name}"
        trail.append(f"team_prefix: {name} -> {params['name']}")

    sw_name = params.get("switch_profile")
    if not sw_name:
        if ptype == "access" and len(nodes) > 1:
            # Multi-node access (e.g. VCF hosts): each node has its own switch
            # profile — resolve them individually and pass a list.
            sw_names = []
            for n in nodes:
                sn, err = await resolve_switch_profile([n])
                if err:
                    return perr(f"node {n}: {err}", trail + ["resolve_switch_profile: failed"])
                sw_names.append(sn)
                trail.append(f"resolve_switch_profile: node {n} -> {sn}")
            sw_name = sw_names
        else:
            sw_name, err = await resolve_switch_profile(nodes)
            if err:
                return perr(err, ["resolve_switch_profile: failed"])
            trail.append(f"resolve_switch_profile: node(s) {'-'.join(nodes)} -> {sw_name}")

    prep = build_port_definition(params, user_input, sw_name)
    prep["trail"] = trail + prep.get("trail", [])
    return prep

async def _build_copy_port(params, user_input, team="", convention=None):
    """Define a NEW port by copying ALL access-policy config (type, AEP, and
    every policy) from an existing source port. The user supplies the new name
    (TEAM_SERIAL) and the new leaf+port; nothing about the policy is overridden.
    """
    intent = "copy_port_definition"

    def cerr(msg, t=None):
        return {"flow": "create", "intent": intent, "user_input": user_input,
                "errors": [f"Copy port definition: {msg}"],
                "trail": t or ["_build_copy_port: error"]}

    name = params.get("name")
    source = params.get("source")
    if isinstance(source, dict):
        source = source.get("serial") or source.get("name")
    nodes = params.get("nodes") or []
    if isinstance(nodes, (str, int)):
        nodes = [str(nodes)]
    nodes = [str(n) for n in nodes if str(n).strip()]
    ports = params.get("ports") or []
    if isinstance(ports, str):
        ports = [ports]

    if not name:
        return cerr("missing the new port name (TEAM_SERIAL)")
    if not source:
        return cerr("missing the source port to copy from")
    if not nodes:
        return cerr("no node(s) specified for the new port")
    if not ports:
        return cerr("no port(s) specified for the new port")

    # Apply TEAM_SERIAL naming: if the user gave just a serial (e.g. CX123F421)
    # and the user has a team, prefix it so the name becomes e.g. MSY_CX123F421.
    trail = []
    if team and name and not name.upper().startswith(team.upper() + "_"):
        name = f"{team}_{name}"
        trail.append(f"team_prefix: {params['name']} -> {name}")
    tpl_res = await read_port_template(source)
    if tpl_res.get("error"):
        return cerr(f"source '{source}': {tpl_res['error']}", trail + ["read_port_template: error"])
    tmpl = tpl_res["template"]
    ptype = tmpl["port_type"]
    trail.append(f"read_port_template: {tpl_res['source']} -> type={ptype}, "
                 f"aep={tmpl['aep'] or '(none)'}")

    if ptype == "vpc" and len(nodes) != 2:
        return cerr("source is a vPC; the new port needs exactly 2 nodes", trail)
    if ptype == "pc" and len(nodes) != 1:
        return cerr("source is a pc; the new port needs exactly 1 node", trail)

    # Resolve switch-name fragments to numeric node IDs
    nodes, nerr = await _resolve_node_list(nodes, trail, convention)
    if nerr:
        return cerr(f"node name resolution: {nerr}", trail)

    sw_name = params.get("switch_profile")
    if not sw_name:
        if ptype == "access" and len(nodes) > 1:
            # Multi-node access (e.g. VCF hosts): each node has its own switch
            # profile — resolve them individually and pass a list.
            sw_names = []
            for n in nodes:
                sn, err = await resolve_switch_profile([n])
                if err:
                    return cerr(f"node {n}: {err}", trail + ["resolve_switch_profile: failed"])
                sw_names.append(sn)
                trail.append(f"resolve_switch_profile: node {n} -> {sn}")
            sw_name = sw_names
        else:
            sw_name, err = await resolve_switch_profile(nodes)
            if err:
                return cerr(err, trail + ["resolve_switch_profile: failed"])
            trail.append(f"resolve_switch_profile: node(s) {'-'.join(nodes)} -> {sw_name}")

    # New name + user location + copied type/AEP/policies (pure copy, no overrides).
    bp_params = {
        "name": name, "port_type": ptype, "nodes": nodes, "ports": ports,
        "aep": tmpl["aep"],
        "cdp": tmpl["cdp"], "lldp": tmpl["lldp"], "link_level": tmpl["link_level"],
        "storm": tmpl["storm"], "stp": tmpl["stp"], "lacp": tmpl["lacp"],
    }
    prep = build_port_definition(bp_params, user_input, sw_name)
    prep["intent"] = intent
    prep["trail"] = trail + prep.get("trail", [])
    return prep

def _copy_destinations(params):
    """Destination port references for copy_epg_bindings: the "destinations"
    list, plus a single "destination" (older intent shape). Each entry is a
    dict; a bare string is treated as a serial / interface-profile name."""
    raw = params.get("destinations")
    if isinstance(raw, (dict, str)):
        raw = [raw]
    refs = list(raw or [])
    single = params.get("destination")
    if isinstance(single, list):
        refs.extend(single)
    elif single:
        refs.append(single)
    out = []
    for r in refs:
        if isinstance(r, str) and r.strip():
            r = {"serial": r.strip()}
        if isinstance(r, dict) and r and r not in out:
            out.append(r)
    return out


def _ref_label(ref):
    if ref.get("serial") or ref.get("name"):
        scope = str(ref.get("scope") or "").lower()
        suffix = f" ({scope})" if scope and scope != "data" else ""
        return str(ref.get("serial") or ref.get("name")) + suffix
    if ref.get("node2"):
        return f"leaf{ref.get('node')}-{ref.get('node2')} {ref.get('policy_group') or ref.get('vpc') or ''}".strip()
    return f"leaf{ref.get('node', '?')} {ref.get('port', '')}".strip()


async def _build_copy_bindings(params, user_input, convention=None):
    """Copy ALL EPG static bindings of one source port onto one or MANY
    destination ports in a single approval-gated POST.

    A destination that cannot be resolved (typo, ambiguous serial, same as the
    source) is skipped and listed in the plan instead of failing the whole
    copy; the copy fails only when NO destination resolves.
    """
    def cerr(msg, t=None):
        return {"flow": "create", "intent": "copy_epg_bindings", "user_input": user_input,
                "errors": [f"Copy bindings: {msg}"], "trail": t or ["_build_copy_bindings: error"]}

    source = params.get("source")
    if isinstance(source, str) and source.strip():
        source = {"serial": source.strip()}
    destinations = _copy_destinations(params)
    if not isinstance(source, dict) or not source:
        return cerr("missing source port reference")
    if not destinations:
        return cerr("missing destination port reference")

    trail = []
    # -- Resolve switch-name fragments in source/destination "node" fields --
    # The source must resolve; a destination that fails is skipped below.
    skipped = []
    dest_refs = []
    for idx, ref_dict in enumerate([source] + destinations):
        failed = None
        for key in ("node", "node2"):
            if key in ref_dict and not re.match(r"^\d{1,4}$", str(ref_dict[key]).strip()):
                nres = await resolve_node_name(ref_dict[key], convention)
                if nres.get("error"):
                    failed = f"{key} name resolution failed for '{ref_dict[key]}': {nres['error']}"
                    break
                trail.append(f"resolve_node_name: {ref_dict[key]} -> {nres['node_id']} ({nres.get('node_name', '')})")
                ref_dict[key] = nres["node_id"]
        if idx == 0:
            if failed:
                return cerr(f"source {failed}", trail + ["resolve_node_name: error"])
        elif failed:
            skipped.append({"destination": _ref_label(ref_dict), "reason": failed})
        else:
            dest_refs.append(ref_dict)

    src = await resolve_port_ref(source)
    if src.get("error"):
        return cerr(f"source: {src['error']}", trail + ["resolve source: error"])
    src_tdns = src.get("tdns") or ([src["tdn"]] if src.get("tdn") else [])
    trail.append(f"resolve source: {src['label']} -> {len(src_tdns)} path(s)")

    # Read bindings from EVERY source path and union them (dual-homed access
    # hosts expose the same EPGs on each port; dedup keeps one copy).
    specs = []
    seen = set()
    for t in src_tdns:
        read = await read_bindings_on_path(t)
        if read.get("error"):
            return cerr(f"reading source bindings: {read['error']}", trail + ["read source: error"])
        for s in read.get("specs", []):
            key = (s["tenant"], s["ap"], s["epg"], s["encap"], s["mode"])
            if key not in seen:
                seen.add(key)
                specs.append(s)
    if not specs:
        return cerr("source has no EPG bindings", trail + ["read source: empty"])
    trail.append(f"read source: {len(specs)} unique binding(s)")

    # Resolve every destination; each may expand to several paths.
    src_set = set(src_tdns)
    dest_groups = []
    dst_tdns = []
    for ref in dest_refs:
        label = _ref_label(ref)
        explicit = _path_tdn(ref) if (ref.get("tdn") or ref.get("node2")) else None
        if explicit:
            tdns = [explicit]
        else:
            dst = await resolve_port_ref(ref)
            if dst.get("error"):
                skipped.append({"destination": label, "reason": dst["error"]})
                trail.append(f"resolve dest: {label} -> skipped ({dst['error']})")
                continue
            tdns = dst.get("tdns") or ([dst["tdn"]] if dst.get("tdn") else [])
            label = dst.get("label", label)
        if src_set & set(tdns):
            skipped.append({"destination": label, "reason": "same port as the source"})
            trail.append(f"resolve dest: {label} -> skipped (overlaps source)")
            continue
        new = [t for t in tdns if t not in dst_tdns]
        if not new:
            skipped.append({"destination": label, "reason": "duplicate of another destination"})
            continue
        dst_tdns.extend(new)
        dest_groups.append({"destination": label, "paths": new})
        trail.append(f"resolve dest: {label} -> {len(new)} path(s)")

    if not dst_tdns:
        reasons = "; ".join(f"{x['destination']}: {x['reason']}" for x in skipped)
        return cerr(f"no destination could be resolved ({reasons})", trail + ["resolve dest: none"])

    prep = build_copy_bindings(specs, dst_tdns, user_input,
                               source_label=src.get("label", ""),
                               dest_groups=dest_groups, skipped=skipped)
    prep["trail"] = trail + prep.get("trail", [])
    return prep

async def run(user_input, fabric, team="", profile="", chat_history=None):
    await _load_tools(fabric)
    convention = detect_switch_convention(profile)
    initial = _new_state(user_input, chat_history)
    intent_state = await recognize_intent(
        {"user_input": user_input, "errors": [], "trail": [], "convention": convention,
         "chat_history": initial["chat_history"]})
    flow = intent_state.get("flow", "unknown")
    intent = intent_state.get("intent", "")
    params = intent_state.get("params", {})

    if flow == "create":
        if intent == "bind_epg":
            prep = await _build_binding(params, user_input)
            if prep.get("errors"):
                return prep
            return await build_execute_graph().ainvoke(prep)
        if intent == "bind_vmm_domain":
            prep = await _build_vmm_binding(params, user_input)
            if prep.get("errors"):
                return prep
            return await build_execute_graph().ainvoke(prep)
        if intent == "define_port":
            prep = await _build_port(params, user_input, team=team, convention=convention)
            if prep.get("errors"):
                return prep
            return await build_execute_graph().ainvoke(prep)
        if intent == "copy_port_definition":
            prep = await _build_copy_port(params, user_input, team=team, convention=convention)
            if prep.get("errors"):
                return prep
            return await build_execute_graph().ainvoke(prep)
        if intent == "copy_epg_bindings":
            prep = await _build_copy_bindings(params, user_input, convention=convention)
            if prep.get("errors"):
                return prep
            return await build_execute_graph().ainvoke(prep)
        return await build_create_graph().ainvoke(initial)
    else:
        # Everything that is not a deterministic create is handled agentically.
        initial.update(intent_state)
        return await build_agentic_graph().ainvoke(
            initial,
            config={"recursion_limit": AGENTIC_RECURSION_LIMIT},
        )

#this welcones the prompt and route according to intent
async def prepare(user_input, fabric, team="", profile="", chat_history=None):
    await _load_tools(fabric)
    convention = detect_switch_convention(profile)
    initial = _new_state(user_input, chat_history)
    intent_state = await recognize_intent(
        {"user_input": user_input, "errors": [], "trail": [], "convention": convention,
         "chat_history": initial["chat_history"]})
    flow = intent_state.get("flow", "unknown")
    intent = intent_state.get("intent", "")
    params = intent_state.get("params", {})

    if flow == "create":
        if intent == "bind_epg":
            return await _build_binding(params, user_input)
        if intent == "bind_vmm_domain":
            return await _build_vmm_binding(params, user_input)
        if intent == "define_port":
            return await _build_port(params, user_input, team=team, convention=convention)
        if intent == "copy_port_definition":
            return await _build_copy_port(params, user_input, team=team, convention=convention)
        if intent == "copy_epg_bindings":
            return await _build_copy_bindings(params, user_input, convention=convention)
        result = await build_prepare_graph().ainvoke(initial)
        result["flow"] = "create"
        return result
    else:
        # Everything that is not a deterministic create is handled agentically.
        initial.update(intent_state)
        result = await build_agentic_graph().ainvoke(
            initial,
            config={"recursion_limit": AGENTIC_RECURSION_LIMIT},
        )
        result["flow"] = "agentic"
        return result

def _summarize_create_result(state) -> str:
    dn = state.get("dn", "")
    target = state.get("target_class", "")
    fr = state.get("final_result", {})
    err = None
    if isinstance(fr, dict):
        if "error" in fr:
            err = fr["error"]
        for item in fr.get("imdata", []) if isinstance(fr.get("imdata"), list) else []:
            if isinstance(item, dict) and "error" in item:
                err = item["error"].get("attributes", {}).get("text", "APIC error")
    if err:
        return f"FAILED. Tried {target}. APIC error: {err}"
    return f"SUCCESS. Applied {target} ({dn})."

async def execute_create(state, fabric):
    await _load_tools(fabric)
    result = await build_execute_graph().ainvoke(state)
    try:
        summary = _summarize_create_result(result)
        result["interpretation"] = await interpret_operation(state.get("user_input", ""), summary)
    except Exception:
        result["interpretation"] = ""
    return result

