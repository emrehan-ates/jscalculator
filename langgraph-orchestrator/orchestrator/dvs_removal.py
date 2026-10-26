"""remove_from_dvs — take servers out of the DVS by moving their policy groups
to the team's baremetal AEP.

For every server the user names:
  1. find its DATA interface profile(s) (name contains the serial; the shared
     ESX(I)-Infra-<leaf> management profiles are never touched);
  2. read each selector's policy group (infraRsAccBaseGrp.tDn);
  3. point that policy group's AEP relation (infraRsAttEntP) at the team's
     baremetal AEP.

The baremetal AEP name differs between fabrics; each team has a fixed list of
possible names (BAREMETAL_AEPS) and at most one of them exists per fabric, so
the AEPs are listed with a GET first and the real name is used. Team = the
logged-in user's team, else the server's own MSY_/CSY_ name prefix.

Safety: a policy group that is also used by interface profiles of OTHER
servers is skipped (changing it would move those servers too). Policy groups
already on the baremetal AEP are reported as unchanged. All changes go out in
one approval-gated POST.
"""
import re

from .mgmt_ports import _get, _attrs, _is_infra_profile, _walk_selectors, host_tokens

INTENT = "remove_from_dvs"
_TEAM_RE = re.compile(r"^(MSY|CSY)[_-]", re.I)


# Possible baremetal AEP names per team; at most one exists on a fabric.
BAREMETAL_AEPS = {
    "MSY": ("MSY_BAREMETAL", "BARE_METAL"),
    "CSY": ("BARE_METAL", "CSY_BAREMETAL", "CSY_BARE_METAL"),
}


async def find_baremetal_aep(team):
    """Return (aep_name, None) or (None, error) for the team's baremetal AEP."""
    candidates = BAREMETAL_AEPS.get(team.upper())
    if not candidates:
        return None, (f"no baremetal AEP names known for team {team} "
                      f"(known teams: {', '.join(BAREMETAL_AEPS)})")
    items = await _get("/api/node/class/infraAttEntityP.json",
                       {"rsp-prop-include": "naming-only"})
    names = [_attrs(i, "infraAttEntityP").get("name", "") for i in items]
    wanted = {c.upper() for c in candidates}
    hits = sorted({n for n in names if n.upper() in wanted})
    if not hits:
        return None, (f"no baremetal AEP found for team {team} "
                      f"(looked for {' / '.join(candidates)})")
    if len(hits) > 1:
        return None, f"several baremetal AEPs match team {team}: {', '.join(hits)}"
    return hits[0], None


def _pg_class(pg_tdn):
    if "/accbundle-" in pg_tdn:
        return "infraAccBndlGrp", pg_tdn.split("/accbundle-", 1)[1]
    if "/accportgrp-" in pg_tdn:
        return "infraAccPortGrp", pg_tdn.split("/accportgrp-", 1)[1]
    return None, None


async def _server_profiles(server):
    """Data interface profiles of a server: names containing its serial."""
    for tok in host_tokens(server)[::-1]:  # bare serial first
        items = await _get("/api/node/class/infraAccPortP.json",
                           {"query-target-filter": f'wcard(infraAccPortP.name,"{tok}")'})
        profs = [_attrs(i, "infraAccPortP") for i in items]
        profs = [{"name": p["name"], "dn": p.get("dn") or f"uni/infra/accportprof-{p['name']}"}
                 for p in profs if p.get("name") and not _is_infra_profile(p["name"])]
        if profs:
            return profs
    return []


async def _current_aep(pg_tdn):
    items = await _get(f"/api/node/mo/{pg_tdn}.json",
                       {"rsp-subtree": "children", "rsp-subtree-class": "infraRsAttEntP"})
    for item in items:
        for body in item.values():
            for ch in body.get("children", []) if isinstance(body, dict) else []:
                tdn = ch.get("infraRsAttEntP", {}).get("attributes", {}).get("tDn", "")
                if tdn:
                    return tdn.split("attentp-", 1)[-1]
    return ""


async def _pg_users(pg_tdn):
    """Interface-profile names whose selectors use this policy group."""
    items = await _get("/api/node/class/infraRsAccBaseGrp.json",
                       {"query-target-filter": f'eq(infraRsAccBaseGrp.tDn,"{pg_tdn}")'})
    users = set()
    for item in items:
        m = re.match(r"uni/infra/accportprof-([^/]+)/", _attrs(item, "infraRsAccBaseGrp").get("dn", ""))
        if m:
            users.add(m.group(1))
    return users


def _err(user_input, msg, trail):
    return {"flow": "create", "intent": INTENT, "user_input": user_input,
            "errors": [f"Remove from DVS: {msg}"], "trail": trail or [f"{INTENT}: error"]}


def _server_refs(params):
    raw = params.get("servers") or params.get("bindings") or []
    if isinstance(raw, (str, dict)):
        raw = [raw]
    if params.get("serial"):
        raw = list(raw) + [params["serial"]]
    out = []
    for r in raw:
        name = r if isinstance(r, str) else (r.get("serial") or r.get("name")) if isinstance(r, dict) else None
        if name and str(name).strip() and str(name).strip() not in out:
            out.append(str(name).strip())
    return out


def build_aep_change(changes, user_input):
    """One polUni POST that re-points each policy group's infraRsAttEntP."""
    pgs = []
    for c in changes:
        pgs.append({c["class"]: {
            "attributes": {"name": c["policy_group"]},
            "children": [{"infraRsAttEntP": {"attributes": {
                "tDn": f"uni/infra/attentp-{c['to_aep']}"}}}],
        }})
    return {"polUni": {"attributes": {}, "children": [{"infraInfra": {
        "attributes": {}, "children": [{"infraFuncP": {
            "attributes": {}, "children": pgs}}]}}]}}


async def build_remove_from_dvs(params, user_input, team=""):
    from .nodes_common import get_tool
    servers = _server_refs(params)
    if not servers:
        return _err(user_input, "no server specified", [f"{INTENT}: no server"])

    trail, changes, unchanged, skipped = [], [], [], []
    aep_cache = {}
    try:
        await get_tool("apic_login").ainvoke({})

        # Pass 1: each server's data profiles and their policy groups.
        per_server = []
        all_profiles = set()
        for server in servers:
            profs = await _server_profiles(server)
            if not profs:
                skipped.append({"server": server, "reason": "no interface profile found for this server"})
                trail.append(f"{INTENT}: {server} -> no interface profile")
                continue
            all_profiles.update(p["name"] for p in profs)
            pg_tdns = []
            for prof in profs:
                imdata = await _get(f"/api/node/mo/{prof['dn']}.json", {"rsp-subtree": "full"})
                for _, pg, _ in _walk_selectors(imdata):
                    if pg and pg not in pg_tdns:
                        pg_tdns.append(pg)
            m = _TEAM_RE.match(profs[0]["name"])
            srv_team = (team or (m.group(1) if m else "")).upper()
            per_server.append((server, profs, pg_tdns, srv_team))
            trail.append(f"{INTENT}: {server} -> profiles {[p['name'] for p in profs]}, "
                         f"{len(pg_tdns)} policy group(s), team {srv_team or '?'}")

        # Pass 2: target AEP per team, safety checks, current AEP.
        seen_pgs = set()
        for server, profs, pg_tdns, srv_team in per_server:
            if not pg_tdns:
                skipped.append({"server": server, "reason": "interface profile has no policy group"})
                continue
            if not srv_team:
                skipped.append({"server": server, "reason": "team unknown (no user team and no MSY_/CSY_ prefix)"})
                continue
            if srv_team not in aep_cache:
                aep_cache[srv_team] = await find_baremetal_aep(srv_team)
                if aep_cache[srv_team][0]:
                    trail.append(f"{INTENT}: team {srv_team} baremetal AEP -> {aep_cache[srv_team][0]}")
            target, aerr = aep_cache[srv_team]
            if aerr:
                skipped.append({"server": server, "reason": aerr})
                continue
            for pg_tdn in pg_tdns:
                if pg_tdn in seen_pgs:
                    continue
                seen_pgs.add(pg_tdn)
                cls, pg_name = _pg_class(pg_tdn)
                if not cls:
                    skipped.append({"server": server, "policy_group": pg_tdn,
                                    "reason": "unrecognised policy group type"})
                    continue
                others = sorted(await _pg_users(pg_tdn) - all_profiles)
                if others:
                    skipped.append({"server": server, "policy_group": pg_name,
                                    "reason": "shared with other interface profiles: "
                                              + ", ".join(others[:5])
                                              + (" ..." if len(others) > 5 else "")})
                    continue
                current = await _current_aep(pg_tdn)
                row = {"server": server, "policy_group": pg_name,
                       "type": "PC/vPC" if cls == "infraAccBndlGrp" else "access",
                       "from_aep": current or "(none)", "to_aep": target}
                if current == target:
                    unchanged.append(row)
                else:
                    changes.append(dict(row, **{"class": cls}))
    except Exception as e:
        return _err(user_input, f"APIC lookup failed: {e}", trail + [f"{INTENT}: error"])

    if not changes:
        why = []
        if unchanged:
            why.append(f"{len(unchanged)} policy group(s) already on the baremetal AEP")
        why += [f"{s['server']}: {s['reason']}" for s in skipped]
        return _err(user_input, "nothing to change (" + "; ".join(why or ["no policy group found"]) + ")",
                    trail + [f"{INTENT}: no changes"])

    payload = {"changes": [{k: v for k, v in c.items() if k != "class"} for c in changes]}
    if unchanged:
        payload["already_baremetal"] = unchanged
    if skipped:
        payload["skipped"] = skipped
    targets = sorted({c["to_aep"] for c in changes})
    return {
        "flow": "create", "intent": INTENT, "user_input": user_input,
        "target_class": f"infraRsAttEntP ({len(changes)} policy group(s) -> {', '.join(targets)})",
        "dn": "uni/infra/funcprof",
        "payload": payload,
        "post_path": "/api/node/mo/uni.json",
        "post_body": build_aep_change(changes, user_input),
        "errors": [],
        "trail": trail + [f"{INTENT}: {len(changes)} change(s), {len(unchanged)} unchanged, "
                          f"{len(skipped)} skipped"],
    }
