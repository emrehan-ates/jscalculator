"""Nodes used only in the CREATE flow (Solution A) + static binding builders."""
import json, re
from langchain_core.messages import HumanMessage, SystemMessage
from .nodes_common import get_llm, get_tool, unwrap_mcp_result, extract_json_from_llm


async def resolve_parent(state):
    intent = state.get("intent", "")
    params = state.get("params", {})
    if intent == "create_bd":
        tenant = params.get("tenant")
        if not tenant:
            return {"errors": ["create_bd requires 'tenant'"], "trail": ["resolve_parent: missing tenant"]}
        return {"parent_dn": f"uni/tn-{tenant}", "trail": [f"resolve_parent: uni/tn-{tenant}"]}
    return {"parent_dn": "", "trail": ["resolve_parent: no parent needed"]}


async def get_schema(state):
    if not state.get("target_class"):
        return {"errors": ["No target_class"], "trail": ["get_schema: SKIPPED"]}
    tool = get_tool("aci_get_mo_spec")
    raw = await tool.ainvoke({"class_name": state["target_class"]})
    result = unwrap_mcp_result(raw)
    if not isinstance(result, dict):
        return {"errors": [f"Schema bad format: {type(result).__name__}"], "trail": ["get_schema: bad format"]}
    if "error" in result:
        return {"errors": [f"Schema lookup failed: {result['error']}"], "trail": ["get_schema: ERROR"]}
    return {"schema": result, "trail": [f"get_schema: {state['target_class']}"]}


PAYLOAD_SYSTEM_PROMPT = """You are constructing an ACI MO payload.
Given the MO class schema and user parameters, construct a flat JSON dict of attributes.
Rules:
1. Include all naming properties and mandatory properties.
2. Use exact enum values. Use "yes"/"no" for booleans (NOT true/false).
3. Include optional descriptive properties only if the user provided them.
Respond with ONLY a flat JSON dict, no className wrapper, no "attributes" key.
"""


async def build_payload(state):
    if not state.get("schema"):
        return {"errors": ["No schema"], "trail": ["build_payload: SKIPPED"]}
    schema = state["schema"]
    llm = get_llm()
    context = f"""
MO Class: {schema.get('class_name')}
Description: {schema.get('description', '')[:300]}
Naming properties: {json.dumps(schema.get('naming_properties', []), indent=2)}
Mandatory properties: {json.dumps(schema.get('mandatory_properties', []), indent=2)}
User parameters: {json.dumps(state.get('params', {}), indent=2)}
"""
    response = await llm.ainvoke([SystemMessage(content=PAYLOAD_SYSTEM_PROMPT), HumanMessage(content=context)])
    try:
        payload = extract_json_from_llm(response.content)
    except (ValueError, json.JSONDecodeError) as e:
        return {"errors": [f"Payload parse failed: {e}"], "trail": ["build_payload: FAILED"]}
    return {"payload": payload, "trail": [f"build_payload: {len(payload)} attributes"]}


async def validate_payload(state):
    if not state.get("payload"):
        return {"validation_result": {"valid": False, "errors": [{"message": "no payload"}]},
                "trail": ["validate_payload: SKIPPED"]}
    tool = get_tool("aci_validate_payload")
    raw = await tool.ainvoke({"class_name": state["target_class"], "payload": state["payload"]})
    result = unwrap_mcp_result(raw)
    if not isinstance(result, dict):
        result = {"valid": False, "errors": [{"message": str(result)}]}
    attempts = state.get("validation_attempts", 0) + 1
    status = "PASSED" if result.get("valid") else f"FAILED ({len(result.get('errors', []))} errors)"
    return {"validation_result": result, "validation_attempts": attempts,
            "trail": [f"validate attempt #{attempts}: {status}"]}


FIX_SYSTEM_PROMPT = """You are correcting an invalid ACI MO payload.
Fix: replace true/false with "yes"/"no"; use exact enum values; add missing required
properties; remove unknown properties. Respond with ONLY the corrected flat JSON payload.
"""


async def fix_payload(state):
    llm = get_llm()
    errors = state.get("validation_result", {}).get("errors", [])
    context = f"""
Schema (truncated): {json.dumps(state.get('schema', {}), indent=2)[:1500]}
Current payload: {json.dumps(state.get('payload', {}), indent=2)}
Validation errors: {json.dumps(errors, indent=2)}
Return the corrected payload.
"""
    response = await llm.ainvoke([SystemMessage(content=FIX_SYSTEM_PROMPT), HumanMessage(content=context)])
    try:
        fixed = extract_json_from_llm(response.content)
    except (ValueError, json.JSONDecodeError) as e:
        return {"errors": [f"Fix parse failed: {e}"], "trail": ["fix_payload: FAILED"]}
    return {"payload": fixed, "trail": ["fix_payload: corrected"]}


async def build_dn(state):
    tool = get_tool("aci_build_dn")
    schema = state.get("schema", {})
    naming_props = [p.get("name") for p in schema.get("naming_properties", []) if isinstance(p, dict)]
    naming_values = {k: v for k, v in state.get("payload", {}).items() if k in naming_props}
    parent_dn = state.get("parent_dn", "")
    tool_args = {"class_name": state["target_class"], "naming_values": naming_values}
    if parent_dn:
        tool_args["parent_dn"] = parent_dn
    raw = await tool.ainvoke(tool_args)
    result = unwrap_mcp_result(raw)
    if not isinstance(result, dict):
        return {"errors": [f"DN build bad format: {type(result).__name__}"], "trail": ["build_dn: bad format"]}
    if result.get("errors") or not result.get("dn"):
        return {"errors": [f"DN build failed: {result.get('errors')}"], "trail": ["build_dn: FAILED"]}
    return {"dn": result["dn"], "trail": [f"build_dn: {result['dn']}"]}


async def post_to_apic(state):
    """POST to APIC. Uses explicit post_path/post_body if present (static binding),
    otherwise builds a single-MO body from dn/target_class/payload."""
    tool = get_tool("apic_post")
    if state.get("post_path") and state.get("post_body"):
        path = state["post_path"]
        body = state["post_body"]
    else:
        path = f"/api/node/mo/{state['dn']}.json"
        body = {state["target_class"]: {"attributes": state["payload"]}}
    raw = await tool.ainvoke({"path": path, "payload": body})
    result = unwrap_mcp_result(raw)
    return {"final_result": result, "trail": [f"post_to_apic: sent to {path}"]}


# ============================================================
# Static binding (fvRsPathAtt) — N ports x M EPGs (cross product)
# Single atomic POST to /api/node/mo/uni.json via polUni tree.
# ============================================================

def _path_tdn(b: dict):
    """Build a fabricPath tDn from a binding entry, or None if invalid."""
    if b.get("tdn"):
        return b["tdn"]
    node = b.get("node"); port = b.get("port")
    node2 = b.get("node2"); pg = b.get("policy_group") or b.get("vpc")
    if pg and node and node2:
        return f"topology/pod-1/protpaths-{node}-{node2}/pathep-[{pg}]"
    if node and port:
        return f"topology/pod-1/paths-{node}/pathep-[{port}]"
    return None


def build_static_binding(epg_specs: list, bindings: list, mode_in: str, user_input: str) -> dict:
    """
    Cross product: bind EVERY EPG to EVERY port.

    epg_specs: list of {"tenant","ap","epg","encap"} (already resolved)
    bindings:  list of port entries ({node,port} or {node,node2,policy_group})
    Each EPG uses its OWN encap; all EPGs bind to all ports.

    Builds a polUni tree:
      polUni -> fvTenant(name) -> fvAp(name) -> fvAEPg(name) -> [fvRsPathAtt...]
    EPGs in the same tenant/ap are grouped under one fvTenant/fvAp node.
    """
    mode_map = {
        "trunk": "regular", "tagged": "regular", "regular": "regular",
        "access": "untagged", "untagged": "untagged", "access untagged": "untagged",
        "native": "native", "trunk native": "native", "access 802.1p": "native",
        "802.1p": "native",
    }
    mode = mode_map.get(str(mode_in).strip().lower(), "regular")

    # Build tDn list from bindings
    tdns = []
    bad = []
    for b in bindings:
        if not isinstance(b, dict):
            bad.append(str(b)); continue
        t = _path_tdn(b)
        if t:
            tdns.append(t)
        else:
            bad.append(str(b))

    errors = []
    if not epg_specs:
        errors.append("no EPGs resolved")
    if not tdns:
        errors.append("no valid ports")
    if bad:
        errors.append(f"invalid port entries: {bad}")
    if errors:
        return {"flow": "create", "intent": "bind_epg", "user_input": user_input,
                "errors": [f"Static binding: {', '.join(errors)}"],
                "trail": ["build_static_binding: invalid input"]}

    # Group EPGs into a tenant -> ap -> [epg...] tree
    # tree[tenant][ap] = list of epg_spec
    tree = {}
    for spec in epg_specs:
        tree.setdefault(spec["tenant"], {}).setdefault(spec["ap"], []).append(spec)

    tenant_children = []
    display = []
    for tenant, aps in tree.items():
        ap_children = []
        for ap, specs in aps.items():
            epg_children = []
            for spec in specs:
                encap = spec["encap"]
                encap_norm = encap if str(encap).startswith("vlan-") else f"vlan-{encap}"
                rspath_children = []
                for tdn in tdns:
                    rspath_children.append({"fvRsPathAtt": {"attributes": {
                        "tDn": tdn,
                        "encap": encap_norm,
                        "mode": mode,
                        "instrImedcy": "lazy",
                        "status": "created,modified",
                    }}})
                    display.append({"epg": spec["epg"], "tDn": tdn,
                                    "encap": encap_norm, "mode": mode})
                epg_children.append({"fvAEPg": {
                    "attributes": {"name": spec["epg"]},
                    "children": rspath_children,
                }})
            ap_children.append({"fvAp": {
                "attributes": {"name": ap},
                "children": epg_children,
            }})
        tenant_children.append({"fvTenant": {
            "attributes": {"name": tenant},
            "children": ap_children,
        }})

    post_body = {"polUni": {"attributes": {}, "children": tenant_children}}
    post_path = "/api/node/mo/uni.json"

    n_epg = len(epg_specs)
    n_port = len(tdns)
    return {
        "flow": "create", "intent": "bind_epg", "user_input": user_input,
        "target_class": f"fvRsPathAtt ({n_epg} EPG x {n_port} port)",
        "dn": "uni",
        "payload": display,
        "post_path": post_path, "post_body": post_body,
        "errors": [],
        "trail": [f"build_static_binding: {n_epg} EPG x {n_port} port = {len(display)} bindings"],
    }


async def resolve_epg_location(epg_name: str) -> dict:
    """Find an EPG by name across the fabric; return tenant+ap parsed from its DN."""
    from .nodes_common import get_tool, unwrap_mcp_result
    try:
        await get_tool("apic_login").ainvoke({})
        raw = await get_tool("apic_get").ainvoke({
            "path": "/api/node/class/fvAEPg.json",
            "params": {"query-target-filter": f'eq(fvAEPg.name,"{epg_name}")'},
        })
    except Exception as e:
        return {"matches": [], "error": str(e)}
    result = unwrap_mcp_result(raw)
    imdata = result.get("imdata", []) if isinstance(result, dict) else []
    matches = []
    for item in imdata:
        if not isinstance(item, dict):
            continue
        attrs = item.get("fvAEPg", {}).get("attributes", {})
        dn = attrs.get("dn", "")
        m = re.match(r"uni/tn-([^/]+)/ap-([^/]+)/epg-(.+)", dn)
        if m:
            matches.append({"tenant": m.group(1), "ap": m.group(2),
                            "epg": m.group(3), "dn": dn})
    return {"matches": matches}


def extract_vlan_from_name(epg_name):
    """Encap VLAN from the EPG name per the team convention: the last
    underscore-separated segment, when it is a 1-4094 number (e.g. 'NAME_123'->123,
    'MSY_TEST_354'->354). Returns (vlan_str, None) on success, or (None, error)
    when the last '_' segment is not such a number (~15% of EPGs)."""
    last = str(epg_name or "").rsplit("_", 1)[-1]
    if last.isdigit() and 1 <= int(last) <= 4094:
        return last, None
    return None, f"EPG name '{epg_name}' has no numeric VLAN suffix after '_'."


async def _resolve_epg_static_encap(tenant: str, ap: str, epg: str):
    """Read the EPG's existing static path bindings (fvRsPathAtt) and return the
    encap VLAN they use. All static bindings of an EPG share one encap, so we
    return that single value to reuse for the new binding.

    Returns (encap_str|None, error|None) where encap_str is like 'vlan-123'.
    """
    from .nodes_common import get_tool, unwrap_mcp_result
    epg_dn = f"uni/tn-{tenant}/ap-{ap}/epg-{epg}"
    try:
        raw = await get_tool("apic_get").ainvoke({
            "path": f"/api/node/mo/{epg_dn}.json",
            "params": {"query-target": "subtree", "target-subtree-class": "fvRsPathAtt"},
        })
    except Exception as e:
        return None, str(e)
    result = unwrap_mcp_result(raw)
    imdata = result.get("imdata", []) if isinstance(result, dict) else []
    encaps = set()
    for item in imdata:
        if not isinstance(item, dict):
            continue
        attrs = item.get("fvRsPathAtt", {}).get("attributes", {})
        enc = attrs.get("encap", "")
        if enc and str(enc).startswith("vlan-"):
            encaps.add(enc)
    if len(encaps) == 1:
        return next(iter(encaps)), None
    if len(encaps) > 1:
        return None, f"multiple distinct static-binding VLANs {sorted(encaps)}"
    return None, "no existing static binding to copy the encap from"


async def _resolve_epg_vlan_pool_encap(tenant: str, ap: str, epg: str, dom_tdn: str = None):
    """Walk EPG → fvRsDomAtt → VMM domain → infraRsVlanNs → VLAN pool →
    fvnsEncapBlk. If the pool has exactly one VLAN block (single-from == to),
    return it as the encap. Multi-VLAN pools return None (ambiguous).

    This is the 3rd fallback after static-binding and EPG-name extraction,
    used for VMM domain EPGs whose name has no VLAN suffix and have no
    existing static bindings.

    Returns (encap_str|None, error|None) where encap_str is like 'vlan-123'.
    """
    from .nodes_common import get_tool, unwrap_mcp_result

    # If a domain tDn is given, use it directly; otherwise discover all
    # VMM domain associations on this EPG.
    epg_dn = f"uni/tn-{tenant}/ap-{ap}/epg-{epg}"
    dom_tdns = []
    if dom_tdn:
        dom_tdns = [dom_tdn]
    else:
        # Find VMM domain associations for this EPG
        try:
            raw = await get_tool("apic_get").ainvoke({
                "path": f"/api/node/mo/{epg_dn}.json",
                "params": {"query-target": "subtree", "target-subtree-class": "fvRsDomAtt"},
            })
        except Exception:
            return None, "vlan-pool lookup failed"
        result = unwrap_mcp_result(raw)
        imdata = result.get("imdata", []) if isinstance(result, dict) else []
        for item in imdata:
            if not isinstance(item, dict):
                continue
            attrs = item.get("fvRsDomAtt", {}).get("attributes", {})
            tdn = attrs.get("tDn", "")
            # Only VMM domains (uni/vmmp-.../dom-...)
            if tdn.startswith("uni/vmmp-"):
                dom_tdns.append(tdn)

    if not dom_tdns:
        return None, "no VMM domain association found"

    # Walk each VMM domain → infraRsVlanNs → VLAN pool → fvnsEncapBlk
    for dtdn in dom_tdns:
        try:
            raw = await get_tool("apic_get").ainvoke({
                "path": f"/api/node/mo/{dtdn}.json",
                "params": {"query-target": "subtree", "target-subtree-class": "infraRsVlanNs"},
            })
        except Exception:
            continue
        result = unwrap_mcp_result(raw)
        imdata = result.get("imdata", []) if isinstance(result, dict) else []
        for item in imdata:
            if not isinstance(item, dict):
                continue
            attrs = item.get("infraRsVlanNs", {}).get("attributes", {})
            vlan_pool_tdn = attrs.get("tDn", "")
            if not vlan_pool_tdn:
                continue
            # Read the VLAN pool's encap blocks
            try:
                vp_raw = await get_tool("apic_get").ainvoke({
                    "path": f"/api/node/mo/{vlan_pool_tdn}.json",
                    "params": {"query-target": "subtree", "target-subtree-class": "fvnsEncapBlk"},
                })
            except Exception:
                continue
            vp_result = unwrap_mcp_result(vp_raw)
            vp_imdata = vp_result.get("imdata", []) if isinstance(vp_result, dict) else []
            blocks = []
            for blk_item in vp_imdata:
                if not isinstance(blk_item, dict):
                    continue
                blk_attrs = blk_item.get("fvnsEncapBlk", {}).get("attributes", {})
                blk_from = blk_attrs.get("from", "")
                blk_to = blk_attrs.get("to", "")
                if blk_from and blk_to:
                    blocks.append((blk_from, blk_to))
            # Single-VLAN pool: exactly one block where from == to
            if len(blocks) == 1 and blocks[0][0] == blocks[0][1]:
                vlan_id = blocks[0][0].replace("vlan-", "")
                return f"vlan-{vlan_id}", None
            # Multi-VLAN pool is ambiguous — skip
    return None, "VLAN pool has multiple VLANs or no blocks"


async def resolve_vmm_domain_epgs(dom_name: str, vmmp: str = "VMware") -> dict:
    """Find every EPG associated with a VMM (VDS) domain and the encap VLAN to
    use for static path binding.

    Discovers the EPGs from fvRsDomAtt (the domain tDn uni/vmmp-<vmmp>/dom-<dom>).
    The encap for a STATIC PATH binding is NOT the VMM-side fvRsDomAtt.encap;
    instead it is read from the EPG's existing fvRsPathAtt static bindings (an
    EPG uses one encap across all its static bindings). If the EPG has no
    existing static binding, the VLAN embedded in the EPG name is used as a
    last resort.

    Returns {"specs": [{tenant, ap, epg, encap, encap_source}],
             "unresolved": [{tenant, ap, epg, reason}], "error": None|str}.
    """
    from .nodes_common import get_tool, unwrap_mcp_result
    dom_tdn = f"uni/vmmp-{vmmp}/dom-{dom_name}"
    try:
        await get_tool("apic_login").ainvoke({})
        raw = await get_tool("apic_get").ainvoke({
            "path": "/api/node/class/fvRsDomAtt.json",
            "params": {"query-target-filter": f'eq(fvRsDomAtt.tDn,"{dom_tdn}")'},
        })
    except Exception as e:
        return {"specs": [], "unresolved": [], "error": str(e)}
    result = unwrap_mcp_result(raw)
    imdata = result.get("imdata", []) if isinstance(result, dict) else []

    specs = []
    unresolved = []
    for item in imdata:
        if not isinstance(item, dict):
            continue
        attrs = item.get("fvRsDomAtt", {}).get("attributes", {})
        dn = attrs.get("dn", "")
        m = re.match(r"uni/tn-([^/]+)/ap-([^/]+)/epg-([^/]+)/rsdomAtt-", dn)
        if not m:
            continue  # not a regular fvAEPg association (e.g. l3extInstP) — skip
        tenant, ap, epg = m.group(1), m.group(2), m.group(3)

        # Encap for the static path binding comes from the EPG's existing static
        # bindings, NOT from the VMM-side fvRsDomAtt.encap.
        encap, serr = await _resolve_epg_static_encap(tenant, ap, epg)
        encap_source = "static-binding"
        if not encap:
            name_vlan, nerr = extract_vlan_from_name(epg)
            if name_vlan:
                encap, encap_source = f"vlan-{name_vlan}", "epg-name"
            else:
                # 3rd fallback: walk the VMM domain's VLAN pool
                pool_encap, _ = await _resolve_epg_vlan_pool_encap(
                    tenant, ap, epg, dom_tdn=dom_tdn)
                if pool_encap:
                    encap, encap_source = pool_encap, "vlan-pool"
                else:
                    unresolved.append({"tenant": tenant, "ap": ap, "epg": epg,
                                       "reason": serr or nerr or "no encap"})
                    continue

        specs.append({"tenant": tenant, "ap": ap, "epg": epg,
                      "encap": encap, "encap_source": encap_source})
    return {"specs": specs, "unresolved": unresolved, "error": None}


async def resolve_epg_encap(tenant: str, ap: str, epg: str, user_encap=None):
    """Resolve the encap VLAN for an EPG static binding.

    Order: explicit user_encap -> the VLAN suffix in the EPG name (team
    convention NAME_123 -> 123) -> the EPG's existing static bindings
    (fvRsPathAtt) -> VMM domain VLAN pool (single-VLAN pool only).

    Returns (encap_str, source, None) on success, or (None, None, error).
    """
    if user_encap:
        return str(user_encap), "user", None
    vlan, _ = extract_vlan_from_name(epg)
    if vlan:
        return vlan, "epg-name", None
    enc, serr = await _resolve_epg_static_encap(tenant, ap, epg)
    if enc:
        return enc, "static-binding", None
    # 4th fallback: walk VMM domain VLAN pool
    pool_encap, _ = await _resolve_epg_vlan_pool_encap(tenant, ap, epg)
    if pool_encap:
        return pool_encap, "vlan-pool", None
    return None, None, (f"Could not determine encap for EPG '{epg}': name has no "
                        f"numeric VLAN suffix, {serr}, and no single-VLAN pool found.")


async def read_bindings_on_path(tdn: str) -> dict:
    """Read every EPG static binding (fvRsPathAtt) currently on a path tDn.

    Returns {"specs": [{"tenant","ap","epg","encap","mode"}], "error": None|str}.
    """
    from .nodes_common import get_tool, unwrap_mcp_result
    try:
        await get_tool("apic_login").ainvoke({})
        raw = await get_tool("apic_get").ainvoke({
            "path": "/api/node/class/fvRsPathAtt.json",
            "params": {"query-target-filter": f'eq(fvRsPathAtt.tDn,"{tdn}")'},
        })
    except Exception as e:
        return {"specs": [], "error": str(e)}
    result = unwrap_mcp_result(raw)
    imdata = result.get("imdata", []) if isinstance(result, dict) else []
    specs = []
    for item in imdata:
        if not isinstance(item, dict):
            continue
        attrs = item.get("fvRsPathAtt", {}).get("attributes", {})
        dn = attrs.get("dn", "")
        m = re.match(r"uni/tn-([^/]+)/ap-([^/]+)/epg-([^/]+)/", dn)
        if not m:
            continue
        specs.append({
            "tenant": m.group(1), "ap": m.group(2), "epg": m.group(3),
            "encap": attrs.get("encap", ""), "mode": attrs.get("mode", "regular"),
        })
    return {"specs": specs, "error": None}


def build_copy_bindings(specs, dest_tdns, user_input):
    """Re-create each source EPG static binding on every dest path (additive merge).

    specs: list of {"tenant","ap","epg","encap","mode"} read from the source path.
    dest_tdns: one tDn or a list of tDns (a destination may expand to several
      access ports). Each source binding is created on EVERY destination path.
    Each binding keeps its OWN encap and mode (copy preserves per-binding mode).
    status 'created' merges into the destination; existing bindings are untouched.

    Builds the same polUni tree shape as build_static_binding:
      polUni -> fvTenant -> fvAp -> fvAEPg -> fvRsPathAtt
    """
    if isinstance(dest_tdns, str):
        dest_tdns = [dest_tdns]
    dest_tdns = [t for t in (dest_tdns or []) if t]
    if not specs:
        return {"flow": "create", "intent": "copy_epg_bindings", "user_input": user_input,
                "errors": ["Copy bindings: source has no EPG bindings"],
                "trail": ["build_copy_bindings: no specs"]}
    if not dest_tdns:
        return {"flow": "create", "intent": "copy_epg_bindings", "user_input": user_input,
                "errors": ["Copy bindings: no destination path"],
                "trail": ["build_copy_bindings: no dest tDn"]}

    # Group source bindings into a tenant -> ap -> [spec...] tree
    tree = {}
    for spec in specs:
        tree.setdefault(spec["tenant"], {}).setdefault(spec["ap"], []).append(spec)

    tenant_children = []
    display = []
    for tenant, aps in tree.items():
        ap_children = []
        for ap, ap_specs in aps.items():
            epg_children = []
            for spec in ap_specs:
                encap = spec["encap"]
                encap_norm = encap if str(encap).startswith("vlan-") else f"vlan-{encap}"
                mode = spec.get("mode") or "regular"
                rspath_children = []
                for dest_tdn in dest_tdns:
                    rspath_children.append({"fvRsPathAtt": {"attributes": {
                        "tDn": dest_tdn,
                        "encap": encap_norm,
                        "mode": mode,
                        "instrImedcy": "lazy",
                        "status": "created,modified",
                    }}})
                    display.append({"epg": spec["epg"], "tDn": dest_tdn,
                                    "encap": encap_norm, "mode": mode})
                epg_children.append({"fvAEPg": {
                    "attributes": {"name": spec["epg"]},
                    "children": rspath_children,
                }})
            ap_children.append({"fvAp": {
                "attributes": {"name": ap},
                "children": epg_children,
            }})
        tenant_children.append({"fvTenant": {
            "attributes": {"name": tenant},
            "children": ap_children,
        }})

    post_body = {"polUni": {"attributes": {}, "children": tenant_children}}
    post_path = "/api/node/mo/uni.json"

    n = len(display)
    return {
        "flow": "create", "intent": "copy_epg_bindings", "user_input": user_input,
        "target_class": f"fvRsPathAtt (copy {n} binding(s))",
        "dn": "uni",
        "payload": display,
        "post_path": post_path, "post_body": post_body,
        "errors": [],
        "trail": [f"build_copy_bindings: {n} binding(s) -> {len(dest_tdns)} dest path(s)"],
    }
