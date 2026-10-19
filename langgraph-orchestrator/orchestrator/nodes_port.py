"""Port definition (access policy chain) — access, PC, and vPC.

Two env-driven knobs so the SAME code works in prod and a test fabric where
the named policies/AEPs don't exist:

  PORT_DEF_NAMING   = prod (default) | test
      prod: profile = selector = policy group = <name>
      test: profile = <name>-PROFILE, selector = <name>-PORT-SELECTOR, pg = <name>

  PORT_DEF_POLICIES = standard (default) | empty
      standard: send bank-standard policy names (Disable_CDP, 1_Rate, ...)
      empty:    omit policy refs entirely -> APIC applies its own defaults
                (use in test fabrics that don't have those named policies)

  Optional per-policy env overrides (win over the mode): PORTDEF_CDP,
  PORTDEF_LLDP, PORTDEF_LINK_LEVEL, PORTDEF_STORM, PORTDEF_STP, PORTDEF_LACP.
  A value given in the user's prompt always wins over everything.

Switch profile is resolved from the fabric by node ID (name-convention agnostic).
An empty policy/AEP value means: do not send that ref (APIC default applies).
"""
import os
import re

STANDARD = {
    "cdp": "Disable_CDP",
    "lldp": "Enable_LLDP",
    "link_level": "10G_LLP",
    "storm": "1_Rate",
    "stp": "BPDU_Filter_Guard",
    "lacp": "Active_LACP",
}
_ENV_KEYS = {
    "cdp": "PORTDEF_CDP", "lldp": "PORTDEF_LLDP", "link_level": "PORTDEF_LINK_LEVEL",
    "storm": "PORTDEF_STORM", "stp": "PORTDEF_STP", "lacp": "PORTDEF_LACP",
}


def _naming_mode():
    return os.environ.get("PORT_DEF_NAMING", "prod").strip().lower()


def _names(base):
    if _naming_mode() == "test":
        return f"{base}-PROFILE", f"{base}-PORT-SELECTOR", base
    return base, base, base


def _policy_defaults():
    """Resolve default policy names: mode (standard|empty) + per-policy env overrides."""
    if os.environ.get("PORT_DEF_POLICIES", "standard").strip().lower() == "empty":
        base = {k: "" for k in STANDARD}
    else:
        base = dict(STANDARD)
    for k, env in _ENV_KEYS.items():
        if env in os.environ:
            base[k] = os.environ[env].strip()
    return base


def _err(user_input, msg):
    return {"flow": "create", "intent": "define_port", "user_input": user_input,
            "errors": [f"Port definition: {msg}"],
            "trail": ["build_port_definition: error"]}


def _parse_port(p):
    m = re.match(r"(?:eth(?:ernet)?)?\s*(\d+)/(\d+)(?:/(\d+))?$", str(p).strip(), re.I)
    if not m:
        return None
    return {"card": m.group(1), "port": m.group(2), "sub": m.group(3)}


async def resolve_switch_profile(node_ids):
    """Find the infraNodeP name covering EXACTLY the given node(s) via infraNodeBlk."""
    from .nodes_common import get_tool, unwrap_mcp_result
    try:
        await get_tool("apic_login").ainvoke({})
        raw = await get_tool("apic_get").ainvoke({"path": "/api/node/class/infraNodeBlk.json"})
    except Exception as e:
        return None, f"switch profile lookup failed: {e}"
    res = unwrap_mcp_result(raw)
    imdata = res.get("imdata", []) if isinstance(res, dict) else []

    nodes = sorted(int(n) for n in node_ids)
    want_from, want_to = str(nodes[0]), str(nodes[-1])

    matches = []
    for item in imdata:
        if not isinstance(item, dict):
            continue
        attrs = item.get("infraNodeBlk", {}).get("attributes", {})
        if attrs.get("from_") == want_from and attrs.get("to_") == want_to:
            m = re.match(r"uni/infra/nprof-([^/]+)/", attrs.get("dn", ""))
            if m:
                matches.append(m.group(1))

    matches = list(dict.fromkeys(matches))
    label = "-".join(str(n) for n in nodes)
    if not matches:
        return None, f"No switch profile found covering node(s) {label}."
    if len(matches) > 1:
        return None, f"Multiple switch profiles match node(s) {label}: {matches}. Specify one explicitly."
    return matches[0], None


async def _nodes_of_switch_profile(sw_name):
    """Inverse of resolve_switch_profile: given a switch profile (infraNodeP)
    name, return the list of node IDs it covers via its infraNodeBlk children."""
    from .nodes_common import get_tool, unwrap_mcp_result
    try:
        await get_tool("apic_login").ainvoke({})
        raw = await get_tool("apic_get").ainvoke({
            "path": f"/api/node/mo/uni/infra/nprof-{sw_name}.json",
            "params": {"rsp-subtree": "full", "rsp-subtree-class": "infraNodeBlk"},
        })
    except Exception as e:
        return None, f"switch profile node lookup failed: {e}"
    res = unwrap_mcp_result(raw)
    imdata = res.get("imdata", []) if isinstance(res, dict) else []

    nodes = []

    def _walk(obj):
        if isinstance(obj, dict):
            for cls, body in obj.items():
                attrs = body.get("attributes", {}) if isinstance(body, dict) else {}
                if cls == "infraNodeBlk":
                    f, t = attrs.get("from_"), attrs.get("to_")
                    if f and t:
                        nodes.extend(str(n) for n in range(int(f), int(t) + 1))
                for ch in (body.get("children", []) if isinstance(body, dict) else []):
                    _walk(ch)
        elif isinstance(obj, list):
            for it in obj:
                _walk(it)

    for item in imdata:
        _walk(item)

    nodes = list(dict.fromkeys(nodes))
    if not nodes:
        return None, f"No nodes found for switch profile '{sw_name}'."
    return nodes, None


def parse_leaf_port(text):
    """Defensive parser: turn 'leaf101' / 'leaf 101' / 'Leaf-101' / '101' into
    the node id '101'. Returns None if no leaf id is present. The intent
    classifier normally pre-splits node/port, but this keeps a fallback."""
    m = re.search(r"(?:leaf[\s\-_]*)?(\d{3,4})\b", str(text).strip(), re.I)
    return m.group(1) if m else None


def _expand_port_block(attrs):
    """Expand an infraPortBlk into a list of 'eth<card>/<port>' strings,
    handling single ports and ranges (fromPort..toPort on the same card,
    e.g. a port channel spanning eth1/11-eth1/17)."""
    fc = attrs.get("fromCard"); fp = attrs.get("fromPort")
    tc = attrs.get("toCard") or fc; tp = attrs.get("toPort") or fp
    if not (fc and fp):
        return []
    try:
        fpN, tpN = int(fp), int(tp)
    except (ValueError, TypeError):
        return [f"eth{fc}/{fp}"]
    if str(tc) != str(fc) or tpN < fpN:
        # Unusual multi-card or inverted block: take the two endpoints.
        return [f"eth{fc}/{fp}", f"eth{tc}/{tp}"]
    return [f"eth{fc}/{p}" for p in range(fpN, tpN + 1)]


MGMT_SCOPES = {"management", "mgmt", "all", "both"}


async def resolve_port_ref(ref):
    """Resolve a port reference to its static-binding tDn(s).

    A {"serial"} reference may carry "scope":
      - "data" (default): the host's own interface profile (below);
      - "management": the host's management ports, found in the shared
        ESXI-Infra-<leaf> interface profiles by port-block description
        (see mgmt_ports.py); optional "nodes" limits the leaves checked;
      - "all": data + management paths together.
    """
    scope = str((ref or {}).get("scope") or "data").strip().lower() if isinstance(ref, dict) else "data"
    if scope not in MGMT_SCOPES or not isinstance(ref, dict) or not (ref.get("serial") or ref.get("name")):
        return await _resolve_data_port_ref(ref)

    from .mgmt_ports import resolve_management_ports
    host = (ref.get("serial") or ref.get("name")).strip()
    mg = await resolve_management_ports(host, ref.get("nodes"))
    if scope in ("management", "mgmt"):
        if mg.get("error"):
            return {"error": mg["error"]}
        return {"tdns": mg["tdns"], "tdn": mg["tdns"][0], "kind": "management",
                "nodes": list(dict.fromkeys(p["node"] for p in mg["ports"])),
                "label": mg["label"], "error": None}

    data = await _resolve_data_port_ref({"serial": host})
    if data.get("error") and mg.get("error"):
        return {"error": f"data ports: {data['error']}; management ports: {mg['error']}"}
    tdns = list(dict.fromkeys((data.get("tdns") or []) + (mg.get("tdns") or [])))
    labels = [x for x in (data.get("label") if not data.get("error") else None,
                          mg.get("label") if not mg.get("error") else None) if x]
    missing = ("data" if data.get("error") else "management" if mg.get("error") else None)
    label = " + ".join(labels) + (f" [no {missing} ports found]" if missing else "")
    return {"tdns": tdns, "tdn": tdns[0], "kind": "all",
            "nodes": list(dict.fromkeys((data.get("nodes") or [])
                                        + [p["node"] for p in mg.get("ports", [])])),
            "label": label, "error": None}


async def _resolve_data_port_ref(ref):
    """Resolve a data-port reference to its static-binding tDn(s).

    ref is either {"node","port"} (explicit leaf+port) or
    {"serial": "..."} (a device serial OR a full interface-profile name).
    Returns {"tdns","tdn","kind","nodes","label","error"} (error None on success):
      - "tdns": ALL binding paths the reference covers.
          * vPC  -> one protpaths path (covers both leaves).
          * PC   -> one bundle path (covers all member ports).
          * access -> one path per (node x port): every physical port on every
                      leaf the interface profile is applied to (dual-homed
                      access hosts get all their ports).
      - "tdn": the first path (kept for backward compatibility).
    [DOLDUR: multi-pod] pod-1 is hardcoded, matching existing _path_tdn.
    """
    from .nodes_common import get_tool, unwrap_mcp_result

    if not isinstance(ref, dict):
        return {"error": f"invalid port reference: {ref!r}"}

    # ---- Explicit leaf + port ----
    node = ref.get("node")
    port = ref.get("port")
    if node and port:
        parsed = _parse_port(port)
        if not parsed:
            return {"error": f"invalid port '{port}'"}
        eth = f"eth{parsed['card']}/{parsed['port']}" + (f"/{parsed['sub']}" if parsed["sub"] else "")
        tdn = f"topology/pod-1/paths-{node}/pathep-[{eth}]"
        return {"tdns": [tdn], "tdn": tdn, "kind": "access", "nodes": [str(node)],
                "label": f"leaf{node} {eth}", "error": None}

    # ---- Serial / full name -> interface profile (infraAccPortP) ----
    serial = (ref.get("serial") or "").strip()
    if not serial:
        return {"error": "port reference needs either {node,port} or {serial}"}

    try:
        await get_tool("apic_login").ainvoke({})
        raw = await get_tool("apic_get").ainvoke({
            "path": "/api/node/class/infraAccPortP.json",
            "params": {"query-target-filter": f'wcard(infraAccPortP.name,"{serial}")'},
        })
    except Exception as e:
        return {"error": f"interface profile lookup failed: {e}"}
    res = unwrap_mcp_result(raw)
    imdata = res.get("imdata", []) if isinstance(res, dict) else []
    profs = []
    for item in imdata:
        if isinstance(item, dict):
            a = item.get("infraAccPortP", {}).get("attributes", {})
            if a.get("name"):
                profs.append({"name": a["name"], "dn": a.get("dn", "")})
    if not profs:
        return {"error": f"no port (interface profile) found for serial '{serial}'"}
    if len(profs) > 1:
        names = ", ".join(p["name"] for p in profs)
        return {"error": f"serial '{serial}' is ambiguous, matches: {names}. Be more specific."}

    prof_name, prof_dn = profs[0]["name"], profs[0]["dn"]

    # Read the interface-profile subtree: port block + policy group reference
    try:
        raw2 = await get_tool("apic_get").ainvoke({
            "path": f"/api/node/mo/{prof_dn}.json",
            "params": {"rsp-subtree": "full"},
        })
    except Exception as e:
        return {"error": f"interface profile subtree read failed: {e}"}
    res2 = unwrap_mcp_result(raw2)
    imdata2 = res2.get("imdata", []) if isinstance(res2, dict) else []

    found = {"eths": [], "pg_tdn": None}

    def _walk2(obj):
        if isinstance(obj, dict):
            for cls, body in obj.items():
                attrs = body.get("attributes", {}) if isinstance(body, dict) else {}
                if cls == "infraPortBlk":
                    found["eths"].extend(_expand_port_block(attrs))
                elif cls == "infraRsAccBaseGrp" and found["pg_tdn"] is None:
                    found["pg_tdn"] = attrs.get("tDn")
                for ch in (body.get("children", []) if isinstance(body, dict) else []):
                    _walk2(ch)
        elif isinstance(obj, list):
            for it in obj:
                _walk2(it)

    for item in imdata2:
        _walk2(item)

    # accbundle- = PC/vPC (bundle name); accportgrp- = access
    bundle = None
    if found["pg_tdn"]:
        m_bnd = re.search(r"accbundle-(.+)$", found["pg_tdn"])
        if m_bnd:
            bundle = m_bnd.group(1)

    # Reverse-resolve node(s): which switch profile binds this interface profile
    try:
        raw3 = await get_tool("apic_get").ainvoke({
            "path": "/api/node/class/infraRsAccPortP.json",
            "params": {"query-target-filter": f'eq(infraRsAccPortP.tDn,"{prof_dn}")'},
        })
    except Exception as e:
        return {"error": f"switch profile reverse lookup failed: {e}"}
    res3 = unwrap_mcp_result(raw3)
    imdata3 = res3.get("imdata", []) if isinstance(res3, dict) else []
    sw_names = []
    for item in imdata3:
        if isinstance(item, dict):
            a = item.get("infraRsAccPortP", {}).get("attributes", {})
            m = re.match(r"uni/infra/nprof-([^/]+)/", a.get("dn", ""))
            if m:
                sw_names.append(m.group(1))
    sw_names = list(dict.fromkeys(sw_names))
    if not sw_names:
        return {"error": f"port '{prof_name}' is not bound to any switch profile."}
    # For access ports (no bundle), the same interface profile being attached to
    # multiple switch profiles is normal for multi-node VCF hosts: each leaf has
    # its own switch profile. Collect nodes from all of them.
    # For PC/vPC bundles, multiple switch profiles is unexpected — keep the error.
    if len(sw_names) > 1 and bundle:
        return {"error": f"port '{prof_name}' is bound to multiple switch profiles: {sw_names}."}

    nodes = []
    for sw in sw_names:
        sw_nodes, err = await _nodes_of_switch_profile(sw)
        if err:
            return {"error": err}
        nodes.extend(sw_nodes)
    nodes = list(dict.fromkeys(nodes))

    # Build the binding tDn(s) by type.
    #   vPC    -> one protpaths path on the protected pair (covers both leaves).
    #   PC     -> one bundle path on its leaf (covers all member ports).
    #   access -> one path per (node x port): every physical port on every leaf
    #             the interface profile is applied to (dual-homed access hosts
    #             get all of their ports).
    if bundle and len(nodes) >= 2:
        n = sorted(int(x) for x in nodes)
        tdns = [f"topology/pod-1/protpaths-{n[0]}-{n[-1]}/pathep-[{bundle}]"]
        kind = "vpc"
    elif bundle:
        tdns = [f"topology/pod-1/paths-{nodes[0]}/pathep-[{bundle}]"]
        kind = "pc"
    else:
        eths = list(dict.fromkeys(found["eths"]))
        if not eths:
            return {"error": f"could not determine any port from interface profile '{prof_name}'."}
        tdns = [f"topology/pod-1/paths-{node}/pathep-[{eth}]"
                for node in nodes for eth in eths]
        kind = "access"

    tdns = list(dict.fromkeys(tdns))
    label = (f"{prof_name} ({kind}, node(s) {'-'.join(str(x) for x in nodes)}, "
             f"{len(tdns)} path(s))")
    return {"tdns": tdns, "tdn": tdns[0], "kind": kind,
            "nodes": [str(x) for x in nodes], "label": label, "error": None}


async def read_port_template(serial):
    """Read an existing port's access-policy config so it can be cloned onto a
    new port. Resolves the source by serial / interface-profile name (substring
    match), then reads its policy group: the type (access/pc/vpc), the AEP, and
    every policy reference (CDP/LLDP/link level/storm/STP/LACP).

    Returns {"template": {port_type, aep, cdp, lldp, link_level, storm, stp,
    lacp}, "source": <profile name>, "error": None|str}.
    """
    from .nodes_common import get_tool, unwrap_mcp_result
    serial = (serial or "").strip()
    if not serial:
        return {"template": None, "source": None, "error": "no source port specified"}

    # ---- Find the source interface profile (infraAccPortP) by name ----
    try:
        await get_tool("apic_login").ainvoke({})
        raw = await get_tool("apic_get").ainvoke({
            "path": "/api/node/class/infraAccPortP.json",
            "params": {"query-target-filter": f'wcard(infraAccPortP.name,"{serial}")'},
        })
    except Exception as e:
        return {"template": None, "source": None, "error": f"interface profile lookup failed: {e}"}
    res = unwrap_mcp_result(raw)
    imdata = res.get("imdata", []) if isinstance(res, dict) else []
    profs = []
    for item in imdata:
        if isinstance(item, dict):
            a = item.get("infraAccPortP", {}).get("attributes", {})
            if a.get("name"):
                profs.append({"name": a["name"], "dn": a.get("dn", "")})
    if not profs:
        return {"template": None, "source": None, "error": f"no port (interface profile) found for '{serial}'"}
    if len(profs) > 1:
        names = ", ".join(p["name"] for p in profs)
        return {"template": None, "source": None,
                "error": f"'{serial}' is ambiguous, matches: {names}. Be more specific."}
    prof = profs[0]

    # ---- Find the policy group tDn from the interface-profile subtree ----
    try:
        raw2 = await get_tool("apic_get").ainvoke({
            "path": f"/api/node/mo/{prof['dn']}.json",
            "params": {"rsp-subtree": "full"},
        })
    except Exception as e:
        return {"template": None, "source": None, "error": f"interface profile subtree read failed: {e}"}
    res2 = unwrap_mcp_result(raw2)

    pg_tdn = {"v": None}

    def _walk(obj):
        if isinstance(obj, dict):
            for cls, body in obj.items():
                attrs = body.get("attributes", {}) if isinstance(body, dict) else {}
                if cls == "infraRsAccBaseGrp" and pg_tdn["v"] is None:
                    pg_tdn["v"] = attrs.get("tDn")
                for ch in (body.get("children", []) if isinstance(body, dict) else []):
                    _walk(ch)
        elif isinstance(obj, list):
            for it in obj:
                _walk(it)

    for item in (res2.get("imdata", []) if isinstance(res2, dict) else []):
        _walk(item)
    if not pg_tdn["v"]:
        return {"template": None, "source": None,
                "error": f"source port '{prof['name']}' has no policy group."}

    # ---- Read the policy group subtree: type + AEP + every policy ref ----
    try:
        raw3 = await get_tool("apic_get").ainvoke({
            "path": f"/api/node/mo/{pg_tdn['v']}.json",
            "params": {"rsp-subtree": "full"},
        })
    except Exception as e:
        return {"template": None, "source": None, "error": f"policy group read failed: {e}"}
    res3 = unwrap_mcp_result(raw3)
    pg_imdata = res3.get("imdata", []) if isinstance(res3, dict) else []
    if not pg_imdata or not isinstance(pg_imdata[0], dict):
        return {"template": None, "source": None, "error": f"policy group '{pg_tdn['v']}' not found."}

    tmpl = {"cdp": "", "lldp": "", "link_level": "", "storm": "", "stp": "", "lacp": "", "aep": ""}
    port_type = None
    for cls, body in pg_imdata[0].items():
        attrs = body.get("attributes", {}) if isinstance(body, dict) else {}
        if cls == "infraAccPortGrp":
            port_type = "access"
        elif cls == "infraAccBndlGrp":
            port_type = "vpc" if attrs.get("lagT") == "node" else "pc"
        for ch in (body.get("children", []) if isinstance(body, dict) else []):
            if not isinstance(ch, dict):
                continue
            for cch, cbody in ch.items():
                cattrs = cbody.get("attributes", {}) if isinstance(cbody, dict) else {}
                if cch == "infraRsCdpIfPol":
                    tmpl["cdp"] = cattrs.get("tnCdpIfPolName", "")
                elif cch == "infraRsLldpIfPol":
                    tmpl["lldp"] = cattrs.get("tnLldpIfPolName", "")
                elif cch == "infraRsHIfPol":
                    tmpl["link_level"] = cattrs.get("tnFabricHIfPolName", "")
                elif cch == "infraRsStormctrlIfPol":
                    tmpl["storm"] = cattrs.get("tnStormctrlIfPolName", "")
                elif cch == "infraRsStpIfPol":
                    tmpl["stp"] = cattrs.get("tnStpIfPolName", "")
                elif cch == "infraRsLacpPol":
                    tmpl["lacp"] = cattrs.get("tnLacpLagPolName", "")
                elif cch == "infraRsAttEntP":
                    m = re.search(r"attentp-(.+)$", cattrs.get("tDn", ""))
                    tmpl["aep"] = m.group(1) if m else ""
    if not port_type:
        return {"template": None, "source": None,
                "error": f"could not determine the port type of '{prof['name']}'."}
    tmpl["port_type"] = port_type
    return {"template": tmpl, "source": prof["name"], "error": None}


def build_port_definition(params, user_input, sw_name):
    """Build the atomic port-definition POST. sw_name is already resolved."""
    name = params.get("name")
    ptype = (params.get("port_type") or params.get("type") or "").lower()
    ports = params.get("ports") or []
    if isinstance(ports, str):
        ports = [ports]

    defs = _policy_defaults()
    # prompt override > env/mode default; empty string => omit the ref
    cdp = params.get("cdp", defs["cdp"]) or ""
    lldp = params.get("lldp", defs["lldp"]) or ""
    link_level = params.get("link_level", defs["link_level"]) or ""
    storm = params.get("storm", defs["storm"]) or ""
    stp = params.get("stp", defs["stp"]) or ""
    lacp = params.get("lacp", defs["lacp"]) or ""
    aep = (params.get("aep") or "").strip()

    missing = []
    if not name: missing.append("name")
    if ptype not in ("access", "pc", "vpc"): missing.append("port_type (access|pc|vpc)")
    if not ports: missing.append("port(s)")
    if not sw_name: missing.append("switch_profile")
    if missing:
        return {"flow": "create", "intent": "define_port", "user_input": user_input,
                "errors": [f"Port definition missing/invalid: {', '.join(missing)}"],
                "trail": ["build_port_definition: missing params"]}

    profile_name, selector_name, pg_base = _names(name)

    if ptype == "access":
        pg_class, pg_seg, lagT = "infraAccPortGrp", f"accportgrp-{pg_base}", None
    else:
        pg_class, pg_seg = "infraAccBndlGrp", f"accbundle-{pg_base}"
        lagT = "node" if ptype == "vpc" else "link"
    pg_tdn = f"uni/infra/funcprof/{pg_seg}"

    # Only include refs with a non-empty value; empty -> APIC default.
    pg_children = []
    if cdp: pg_children.append({"infraRsCdpIfPol": {"attributes": {"tnCdpIfPolName": cdp}}})
    if lldp: pg_children.append({"infraRsLldpIfPol": {"attributes": {"tnLldpIfPolName": lldp}}})
    if link_level: pg_children.append({"infraRsHIfPol": {"attributes": {"tnFabricHIfPolName": link_level}}})
    if storm: pg_children.append({"infraRsStormctrlIfPol": {"attributes": {"tnStormctrlIfPolName": storm}}})
    if stp: pg_children.append({"infraRsStpIfPol": {"attributes": {"tnStpIfPolName": stp}}})
    if aep: pg_children.append({"infraRsAttEntP": {"attributes": {"tDn": f"uni/infra/attentp-{aep}"}}})
    if ptype in ("pc", "vpc") and lacp:
        pg_children.append({"infraRsLacpPol": {"attributes": {"tnLacpLagPolName": lacp}}})

    pg_attrs = {"name": pg_base, "descr": name}
    if lagT:
        pg_attrs["lagT"] = lagT
    policy_group = {pg_class: {"attributes": pg_attrs, "children": pg_children}}

    port_blocks = []
    bad_ports = []
    for i, p in enumerate(ports, 1):
        parsed = _parse_port(p)
        if not parsed:
            bad_ports.append(str(p)); continue
        attrs = {"fromCard": parsed["card"], "toCard": parsed["card"],
                 "fromPort": parsed["port"], "toPort": parsed["port"],
                 "name": f"block{i}", "descr": name}
        if parsed["sub"]:
            attrs["fromSubPort"] = parsed["sub"]
            attrs["toSubPort"] = parsed["sub"]
        port_blocks.append({"infraPortBlk": {"attributes": attrs}})
    if bad_ports:
        return _err(user_input, f"invalid port(s): {bad_ports}")

    selector = {"infraHPortS": {
        "attributes": {"name": selector_name, "descr": name, "type": "range"},
        "children": port_blocks + [
            {"infraRsAccBaseGrp": {"attributes": {"tDn": pg_tdn, "fexId": "101"}}}
        ],
    }}
    interface_profile = {"infraAccPortP": {
        "attributes": {"name": profile_name, "descr": name},
        "children": [selector],
    }}

    # sw_name may be a list when the same access port is being defined on
    # multiple independent switch profiles (e.g. multi-node VCF access ports).
    sw_names_list = sw_name if isinstance(sw_name, list) else [sw_name]
    sw_display = "+".join(sw_names_list)

    switch_profiles = [
        {"infraNodeP": {
            "attributes": {"name": sn, "status": "modified"},
            "children": [
                {"infraRsAccPortP": {"attributes": {"tDn": f"uni/infra/accportprof-{profile_name}"}}}
            ],
        }}
        for sn in sw_names_list
    ]

    post_body = {"infraInfra": {"attributes": {}, "children": [
        {"infraFuncP": {"attributes": {}, "children": [policy_group]}},
        interface_profile,
        *switch_profiles,
    ]}}
    post_path = "/api/node/mo/uni/infra.json"

    sent = {k: v for k, v in {"cdp": cdp, "lldp": lldp, "link_level": link_level,
                              "storm": storm, "stp": stp,
                              **({"lacp": lacp} if ptype in ("pc", "vpc") else {})}.items() if v}
    omitted = [k for k, v in {"cdp": cdp, "lldp": lldp, "link_level": link_level,
                              "storm": storm, "stp": stp,
                              **({"lacp": lacp} if ptype in ("pc", "vpc") else {})}.items() if not v]

    display = {
        "type": ptype,
        "naming_mode": _naming_mode(),
        "interface_profile": f"accportprof-{profile_name}",
        "selector": selector_name,
        "policy_group": f"{pg_seg}" + (f" lagT={lagT}" if lagT else ""),
        "ports": list(ports),
        "switch_profile": sw_display,
        "aep": aep or "(none — APIC default)",
        "policies_sent": sent or "(none)",
        "policies_omitted_apic_default": omitted or "(none)",
    }

    return {
        "flow": "create", "intent": "define_port", "user_input": user_input,
        "target_class": f"port-definition ({ptype}, naming={_naming_mode()})",
        "dn": f"uni/infra (accportprof-{profile_name}, {pg_seg}, {sw_display})",
        "payload": display,
        "post_path": post_path, "post_body": post_body,
        "errors": [],
        "trail": [f"build_port_definition: {ptype} '{name}' [{_naming_mode()}] on {sw_display}, "
                  f"{len(port_blocks)} port(s), {len(sent)} policy ref(s), "
                  f"{len(sw_names_list)} switch profile(s)"],
    }
