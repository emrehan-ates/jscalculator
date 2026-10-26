"""define_epg — new EPG(s), each with its own new bridge domain.

Request (user): EPG base name(s), optionally a VLAN pool and domains.
  -> free VLANs are taken from the VLAN Desk API: free on EVERY environment it
     knows (all APICs / Core L3 / DCI), inside the pool if one was given;
     final name = <BASE>_<VLAN> for both the EPG and its BD. Nothing is
     reserved yet.
Approval (admin, chainlit wizard): VLAN pool (keep / change / none — a change
  picks new VLANs and names), tenant, AP, VRF, per-EPG subnet + scope, domain
  additions/removals -> plan["epg_admin"].
Execution (batch.execute_batch -> allocate_and_build):
  each VLAN is reserved on the VLAN API first (a 409 means someone took it:
  the next free VLAN is used and the EPG/BD renamed), then the EPG/BD tree is
  pushed together with the rest of the batch; if the push fails the
  reservations are released again.

BD/EPG settings follow the team template (BD-/epg-ZT_PROD_APP_110): a subnet
turns unicast routing on, no subnet keeps it off. No static bindings.
"""
import ipaddress
import re

from . import vlan_api

INTENT = "define_epg"
NAME_RE = re.compile(r"^[A-Za-z0-9_.-]{1,60}$")
SUBNET_SCOPES = ("private", "public", "private,shared", "public,shared")

# Team template, from BD-ZT_PROD_APP_110 (unicastRoute/subnet set per EPG).
BD_TEMPLATE = {
    "arpFlood": "yes",
    "ipLearning": "yes",
    "limitIpLearnToSubnets": "yes",
    "unkMacUcastAct": "proxy",
    "unkMcastAct": "flood",
    "v6unkMcastAct": "flood",
    "multiDstPktAct": "bd-flood",
    "type": "regular",
}
# From epg-ZT_PROD_APP_110.
EPG_TEMPLATE = {
    "floodOnEncap": "disabled",
    "pcEnfPref": "unenforced",
    "prefGrMemb": "exclude",
    "prio": "level3",
}
# fvRsDomAtt immediacy per domain kind, as in the example EPG.
DOMAIN_ATTACH = {
    "phys": {"resImedcy": "immediate", "instrImedcy": "lazy"},
    "vmm": {"resImedcy": "pre-provision", "instrImedcy": "lazy"},
}


async def _apic_get(path, params=None):
    from .nodes_common import get_tool, unwrap_mcp_result
    args = {"path": path}
    if params:
        args["params"] = params
    res = unwrap_mcp_result(await get_tool("apic_get").ainvoke(args))
    if isinstance(res, dict) and res.get("error"):
        raise RuntimeError(res["error"])
    return res.get("imdata", []) if isinstance(res, dict) else []


def _attr(item, cls):
    return item.get(cls, {}).get("attributes", {}) if isinstance(item, dict) else {}


async def find_domain(name):
    """Physical or VMM domain by name -> {"name","dn","kind"} or None."""
    flt = f'eq(physDomP.name,"{name}")'
    for i in await _apic_get("/api/node/class/physDomP.json", {"query-target-filter": flt}):
        a = _attr(i, "physDomP")
        return {"name": a["name"], "dn": a.get("dn") or f"uni/phys-{a['name']}", "kind": "phys"}
    flt = f'eq(vmmDomP.name,"{name}")'
    for i in await _apic_get("/api/node/class/vmmDomP.json", {"query-target-filter": flt}):
        a = _attr(i, "vmmDomP")
        return {"name": a["name"], "dn": a["dn"], "kind": "vmm"}
    return None


async def list_domains():
    """All physical + VMM domains (for the admin wizard)."""
    out = []
    for i in await _apic_get("/api/node/class/physDomP.json", {"rsp-prop-include": "naming-only"}):
        a = _attr(i, "physDomP")
        out.append({"name": a["name"], "dn": a.get("dn") or f"uni/phys-{a['name']}", "kind": "phys"})
    for i in await _apic_get("/api/node/class/vmmDomP.json", {"rsp-prop-include": "naming-only"}):
        a = _attr(i, "vmmDomP")
        out.append({"name": a["name"], "dn": a["dn"], "kind": "vmm"})
    return sorted(out, key=lambda d: (d["kind"], d["name"].lower()))


async def list_tenants():
    items = await _apic_get("/api/node/class/fvTenant.json", {"rsp-prop-include": "naming-only"})
    return sorted((_attr(i, "fvTenant").get("name") for i in items), key=str.lower)


async def _children(tenant, cls):
    items = await _apic_get(f"/api/node/mo/uni/tn-{tenant}.json",
                            {"query-target": "children", "target-subtree-class": cls})
    return sorted((_attr(i, cls).get("name") for i in items if _attr(i, cls).get("name")),
                  key=str.lower)


async def list_aps(tenant):
    return await _children(tenant, "fvAP")


async def list_vrfs(tenant):
    """VRFs a BD in `tenant` can use by name: its own, then common's (a common
    VRF shadowed by a same-named tenant VRF is left out)."""
    own = await _children(tenant, "fvCtx")
    common = [] if tenant == "common" else await _children("common", "fvCtx")
    return own + [f"common/{v}" for v in common if v not in own]


async def _vmm_lag_policy(dom_dn):
    """The VMM domain's enhanced LAG policy DN if it has exactly one."""
    try:
        items = await _apic_get(f"/api/node/mo/{dom_dn}/vswitchpolcont.json",
                                {"query-target": "children",
                                 "target-subtree-class": "lacpEnhancedLagPol"})
    except RuntimeError:
        return None
    dns = [_attr(i, "lacpEnhancedLagPol").get("dn") for i in items]
    dns = [d for d in dns if d]
    return dns[0] if len(dns) == 1 else None


def _names(params):
    raw = params.get("epgs") or params.get("epg") or params.get("names") or []
    if isinstance(raw, str):
        raw = [raw]
    out = []
    for n in raw:
        n = str(n).strip()
        if n and n not in out:
            out.append(n)
    return out


def _err(user_input, msg, trail=None):
    return {"flow": "create", "intent": INTENT, "user_input": user_input,
            "errors": [f"EPG definition: {msg}"], "trail": trail or [f"{INTENT}: error"]}


async def prepare_define_epg(params, user_input, fabric=None):
    names = _names(params)
    if not names:
        return _err(user_input, "no EPG name given")
    bad = [n for n in names if not NAME_RE.match(n)]
    if bad:
        return _err(user_input, f"invalid EPG name(s): {', '.join(bad)}")
    trail = []
    try:
        sources, pool = await vlan_api.resolve_scope(params.get("pool") or params.get("vlan_pool"))
        epgs = await pick_vlans(names, sources, pool)
    except vlan_api.VlanApiError as e:
        return _err(user_input, str(e), trail)
    trail.append(f"{INTENT}: VLAN API pool={pool or '(none)'} on {len(sources)} environment(s) "
                 f"-> {[e['vlan'] for e in epgs]}")
    try:
        flt = "or(" + ",".join(f'eq(fvAEPg.name,"{e["name"]}")' for e in epgs) + ")"
        taken = [_attr(i, "fvAEPg").get("name") for i in
                 await _apic_get("/api/node/class/fvAEPg.json", {"query-target-filter": flt})]
        domains = []
        for d in params.get("domains") or []:
            dom = await find_domain(str(d).strip())
            if dom is None:
                return _err(user_input, f"domain '{d}' not found (physical or VMM)", trail)
            domains.append(dom)
    except RuntimeError as e:
        return _err(user_input, f"APIC lookup failed: {e}", trail)
    if taken:
        return _err(user_input, f"EPG(s) already exist on APIC: {', '.join(taken)} — the VLAN API "
                                "may be out of date for this fabric", trail)

    request = {"epgs": epgs, "pool": pool, "sources": sources, "domains": domains}
    return {
        "flow": "create", "intent": INTENT, "user_input": user_input,
        "target_class": f"EPG + BD ({len(epgs)})",
        "dn": "uni (tenant / AP / VRF chosen by the admin at approval)",
        "payload": {
            "epgs": [f"{e['name']} (vlan-{e['vlan']}, BD {e['name']})" for e in epgs],
            "vlan_pool": (f"{pool}" if pool else "(no pool — any VLAN)")
                         + " — free on every VLAN API environment; re-checked and reserved "
                           "when applied",
            "domains": [f"{d['name']} ({d['kind']})" for d in domains] or "(admin decides)",
            "admin_decides": ["tenant", "application profile", "VRF", "subnet + scope per EPG",
                              "domains"],
        },
        "epg_request": request,
        "errors": [],
        "trail": trail + [f"{INTENT}: {', '.join(e['name'] for e in epgs)}"],
    }


async def pick_vlans(names, sources, pool):
    """[{base, vlan, name}] with one free VLAN per base name."""
    vlans = await vlan_api.free_vlans(sources, pool, limit=len(names))
    if len(vlans) < len(names):
        raise vlan_api.VlanApiError(
            f"only {len(vlans)} VLAN(s) free on every environment"
            + (f" in {pool}" if pool else "") + f", {len(names)} needed")
    return [{"base": b, "vlan": v, "name": f"{b}_{v}"} for b, v in zip(names, vlans)]


async def change_pool(request, pool_text):
    """Admin picked another pool (or none): new VLANs and names, in place."""
    sources, pool = await vlan_api.resolve_scope(pool_text)
    request["epgs"] = await pick_vlans([e["base"] for e in request["epgs"]], sources, pool)
    request["sources"], request["pool"] = sources, pool
    return request


def validate_subnet(text):
    """'10.1.1.1/24' -> normalized string; raises ValueError."""
    iface = ipaddress.ip_interface(str(text).strip())
    if "/" not in str(text) or iface.network.prefixlen in (0, iface.max_prefixlen):
        raise ValueError("give the gateway with its mask, e.g. 10.1.1.1/24")
    if iface.ip == iface.network.network_address:
        raise ValueError("that is the network address; give the gateway IP, e.g. 10.1.1.1/24")
    return str(iface)


async def build_tree(epgs, admin):
    """polUni children for the given EPGs with the admin's decisions."""
    tenant, ap, vrf = admin["tenant"], admin["ap"], admin["vrf"]
    subnets = admin.get("subnets") or {}
    domain_children = []
    for d in admin.get("domains") or []:
        attrs = {"tDn": d["dn"], **DOMAIN_ATTACH.get(d["kind"], {})}
        node = {"fvRsDomAtt": {"attributes": attrs}}
        if d["kind"] == "vmm":
            lag = await _vmm_lag_policy(d["dn"])
            if lag:
                node["fvRsDomAtt"]["children"] = [{"fvAEPgLagPolAtt": {"attributes": {}, "children": [
                    {"fvRsVmmVSwitchEnhancedLagPol": {"attributes": {"tDn": lag}}}]}}]
        domain_children.append(node)

    bds, epg_nodes = [], []
    for e in epgs:
        sub = subnets.get(e["base"]) or subnets.get(e["name"])
        bd_children = [{"fvRsCtx": {"attributes": {"tnFvCtxName": vrf}}}]
        if sub:
            bd_children.append({"fvSubnet": {"attributes": {"ip": sub["ip"], "scope": sub["scope"]}}})
        bds.append({"fvBD": {
            "attributes": {"name": e["name"], "descr": e["name"], **BD_TEMPLATE,
                           "unicastRoute": "yes" if sub else "no"},
            "children": bd_children}})
        epg_nodes.append({"fvAEPg": {
            "attributes": {"name": e["name"], "descr": e["name"], **EPG_TEMPLATE},
            "children": [{"fvRsBd": {"attributes": {"tnFvBDName": e["name"]}}}]
                        + [dict(c) for c in domain_children]}})
    return [{"fvTenant": {"attributes": {"name": tenant}, "children": bds + [
        {"fvAp": {"attributes": {"name": ap}, "children": epg_nodes}}]}}]


async def allocate(request, note):
    """Reserve every VLAN of the request on the VLAN API, re-picking any that
    were taken since the request. Returns (epgs, renames, reserved, error)."""
    sources, pool = request["sources"], request.get("pool")
    used = {e["vlan"] for e in request["epgs"]}
    epgs, renames, reserved = [], {}, []
    for e in request["epgs"]:
        cur = dict(e)
        for _ in range(5):
            try:
                await vlan_api.reserve(cur["vlan"], sources, pool, f"{note} {cur['name']}")
                break
            except vlan_api.VlanConflict:
                free = [v for v in await vlan_api.free_vlans(sources, pool) if v not in used]
                if not free:
                    return epgs, renames, reserved, (f"no free VLAN left" + (f" in {pool}" if pool else "")
                                                     + f" for {e['base']}")
                cur = {"base": e["base"], "vlan": free[0], "name": f"{e['base']}_{free[0]}"}
                used.add(free[0])
            except vlan_api.VlanApiError as ex:
                return epgs, renames, reserved, str(ex)
        else:
            return epgs, renames, reserved, f"could not reserve a VLAN for {e['base']}"
        reserved.append(cur["vlan"])
        if cur["name"] != e["name"]:
            renames[e["name"]] = cur["name"]
        renames.setdefault(e["base"], cur["name"])
        epgs.append(cur)
    return epgs, renames, reserved, None


async def release_all(vlans):
    """Best effort; returns the VLANs that could not be released."""
    failed = []
    for v in vlans:
        try:
            await vlan_api.release(v)
        except vlan_api.VlanApiError:
            failed.append(v)
    return failed
