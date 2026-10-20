"""Management-port resolution for ESXi / server hosts.

Site convention: a host's MANAGEMENT ports are not in the host's own
interface profile. Every leaf has a shared infra interface profile named
ESXI-Infra-<NODE> or ESX-Infra-<NODE> ("-" or "_" between the parts, so 8
spellings in total), holding
team selectors such as ESXI-Infra-MSY-<NODE> / ESXI-Infra-CSY-<NODE>. The
port block (infraPortBlk) whose DESCRIPTION names the host is that host's
management port.

Resolution:
  1. leaves to check = nodes given by the caller, else the leaves of the
     host's data interface profile(s) (e.g. a host on 109+110 checks both);
  2. per leaf: find ESX(I)-Infra-<NODE> trying all 8 spellings in ONE
     query (case-insensitive fallback in a second query);
  3. read the profile subtree, keep every port block whose descr contains the
     host token -> leaf N (from the profile name) + eth port = management port.
If no leaf is known, fall back to one fabric-wide infraPortBlk description
search limited to ESXI-Infra profiles.

Used by resolve_port_ref (create flows, port refs with scope=management|all)
and by the agent tool find_management_ports.
"""
import json
import re

from langchain_core.tools import tool

INFRA_PREFIXES = ("ESXI", "ESX")
INFRA_WORD = "Infra"
_TEAM_PREFIX_RE = re.compile(r"^(?:MSY|CSY|SG)[_-]", re.I)
_DATA_SUFFIX_RE = re.compile(r"[_-]DATA$", re.I)
_INFRA_NAME_RE = re.compile(r"^esxi?[-_]infra[-_]0*(\d+)$", re.I)


def infra_profile_names(node):
    """All 8 spellings of the infra interface profile for a leaf: ESXI or ESX
    prefix, each with any mix of '-'/'_'."""
    n = str(node).strip()
    return [f"{p}{a}{INFRA_WORD}{b}{n}"
            for p in INFRA_PREFIXES for a in "-_" for b in "-_"]


def host_tokens(host):
    """Strings to look for in port-block descriptions: the host as given plus
    the bare serial (team prefix / _DATA suffix stripped)."""
    h = str(host or "").strip()
    if not h:
        return []
    bare = _DATA_SUFFIX_RE.sub("", _TEAM_PREFIX_RE.sub("", h))
    return list(dict.fromkeys(t for t in (h, bare) if t))


def _descr_matches(descr, tokens):
    d = (descr or "").lower()
    return bool(d) and any(t.lower() in d for t in tokens)


def _is_infra_profile(name, node=None):
    m = _INFRA_NAME_RE.match(name or "")
    if not m:
        return False
    return node is None or int(m.group(1)) == int(node)


async def _get(path, params=None):
    from .nodes_common import get_tool, unwrap_mcp_result
    args = {"path": path}
    if params:
        args["params"] = params
    raw = await get_tool("apic_get").ainvoke(args)
    res = unwrap_mcp_result(raw)
    if isinstance(res, dict) and res.get("error"):
        raise RuntimeError(res["error"])
    return res.get("imdata", []) if isinstance(res, dict) else []


def _attrs(item, cls):
    return item.get(cls, {}).get("attributes", {}) if isinstance(item, dict) else {}


async def find_infra_profiles(node):
    """Return [{"name","dn"}] of the ESXI-Infra-<node> / ESX-Infra-<node>
    interface profile(s)."""
    names = infra_profile_names(node)
    flt = "or(" + ",".join(f'eq(infraAccPortP.name,"{n}")' for n in names) + ")"
    found = [_attrs(i, "infraAccPortP") for i in await _get(
        "/api/node/class/infraAccPortP.json", {"query-target-filter": flt})]
    if not found:
        # Case / zero-padding differences: substring on "nfra" + node, then
        # keep only names that really are ESX(I)-Infra-<node>.
        n = str(node).strip()
        terms = [f'wcard(infraAccPortP.name,"{w}{s}{n}")'
                 for w in ("nfra", "NFRA") for s in "-_"]
        found = [_attrs(i, "infraAccPortP") for i in await _get(
            "/api/node/class/infraAccPortP.json",
            {"query-target-filter": "or(" + ",".join(terms) + ")"})]
    out = [{"name": a["name"], "dn": a.get("dn") or f"uni/infra/accportprof-{a['name']}"}
           for a in found if a.get("name") and _is_infra_profile(a["name"], node)]
    return list({p["dn"]: p for p in out}.values())


async def _profile_nodes(prof_dn):
    """Leaves the interface profile is applied to (via its switch profiles)."""
    from .nodes_port import _nodes_of_switch_profile
    rel = await _get("/api/node/class/infraRsAccPortP.json",
                     {"query-target-filter": f'eq(infraRsAccPortP.tDn,"{prof_dn}")'})
    nodes = []
    for item in rel:
        m = re.match(r"uni/infra/nprof-([^/]+)/", _attrs(item, "infraRsAccPortP").get("dn", ""))
        if m:
            sw_nodes, err = await _nodes_of_switch_profile(m.group(1))
            if not err:
                nodes.extend(sw_nodes)
    return list(dict.fromkeys(nodes))


def _walk_selectors(imdata):
    """Yield (selector_name, pg_tdn, [port-block attrs]) for every infraHPortS."""
    def walk(obj):
        if isinstance(obj, list):
            for it in obj:
                yield from walk(it)
            return
        if not isinstance(obj, dict):
            return
        for cls, body in obj.items():
            if not isinstance(body, dict):
                continue
            children = body.get("children", [])
            if cls == "infraHPortS":
                blocks, pg = [], None
                for ch in children:
                    if "infraPortBlk" in ch:
                        blocks.append(ch["infraPortBlk"].get("attributes", {}))
                    elif "infraRsAccBaseGrp" in ch:
                        pg = ch["infraRsAccBaseGrp"].get("attributes", {}).get("tDn")
                yield body.get("attributes", {}).get("name", ""), pg, blocks
            else:
                yield from walk(children)
    yield from walk(imdata)


def _port_path(node, eth, pg_tdn):
    m = re.search(r"accbundle-(.+)$", pg_tdn or "")
    if m:  # a bundle selector: the binding goes on the bundle path
        return f"topology/pod-1/paths-{node}/pathep-[{m.group(1)}]"
    return f"topology/pod-1/paths-{node}/pathep-[{eth}]"


async def _ports_in_profile(prof, tokens):
    from .nodes_port import _expand_port_block
    imdata = await _get(f"/api/node/mo/{prof['dn']}.json", {"rsp-subtree": "full"})
    hits = []
    for sel, pg, blocks in _walk_selectors(imdata):
        for blk in blocks:
            if _descr_matches(blk.get("descr"), tokens):
                hits.append({"selector": sel, "pg": pg, "block": blk.get("name", ""),
                             "descr": blk.get("descr", ""), "eths": _expand_port_block(blk)})
    if not hits:
        return []
    # The profile is per leaf: ESXI-Infra-<N> -> leaf N. Taken from the name,
    # not from the switch profile, which may cover a leaf pair.
    node = str(int(_INFRA_NAME_RE.match(prof["name"]).group(1)))
    ports = []
    for h in hits:
        for eth in h["eths"]:
            ports.append({"node": node, "port": eth, "profile": prof["name"],
                          "selector": h["selector"], "port_block": h["block"],
                          "descr": h["descr"], "policy_group": h["pg"],
                          "tdn": _port_path(node, eth, h["pg"])})
    return ports


async def host_data_nodes(host):
    """Leaves of the host's DATA interface profile(s): profiles whose name
    contains the host token (ESXI-Infra profiles excluded)."""
    nodes, profiles = [], []
    for tok in host_tokens(host)[::-1]:  # bare serial first: matches every team prefix
        items = await _get("/api/node/class/infraAccPortP.json",
                           {"query-target-filter": f'wcard(infraAccPortP.name,"{tok}")'})
        profiles = [_attrs(i, "infraAccPortP") for i in items]
        profiles = [p for p in profiles if p.get("name") and not _is_infra_profile(p["name"])]
        if profiles:
            break
    for p in profiles:
        dn = p.get("dn") or f"uni/infra/accportprof-{p['name']}"
        nodes.extend(await _profile_nodes(dn))
    return list(dict.fromkeys(nodes)), [p["name"] for p in profiles]


async def _fabric_wide_descr_search(tokens):
    """No leaf known: one infraPortBlk description search, ESXI-Infra only."""
    terms = [f'wcard(infraPortBlk.descr,"{t}")' for t in tokens]
    flt = terms[0] if len(terms) == 1 else "or(" + ",".join(terms) + ")"
    items = await _get("/api/node/class/infraPortBlk.json", {"query-target-filter": flt})
    profs = {}
    for item in items:
        m = re.match(r"(uni/infra/accportprof-([^/]+))/", _attrs(item, "infraPortBlk").get("dn", ""))
        if m and _is_infra_profile(m.group(2)):
            profs[m.group(1)] = {"name": m.group(2), "dn": m.group(1)}
    return list(profs.values())


async def resolve_management_ports(host, nodes=None):
    """Find a host's management ports in the ESXI-Infra-<NODE> profiles.

    Returns {"host","nodes_checked","data_profiles","profiles_checked",
             "ports":[{node,port,profile,selector,port_block,descr,policy_group,tdn}],
             "tdns","label","notes":[...],"error":None|str}.
    "error" is set only when nothing could be searched or nothing was found.
    """
    from .nodes_common import get_tool
    tokens = host_tokens(host)
    out = {"host": host, "nodes_checked": [], "data_profiles": [], "profiles_checked": [],
           "ports": [], "tdns": [], "label": "", "notes": [], "error": None}
    if not tokens:
        out["error"] = "no host given"
        return out
    try:
        await get_tool("apic_login").ainvoke({})
        nodes = [str(int(str(n).strip())) for n in (nodes or []) if str(n).strip().isdigit()]
        if not nodes:
            nodes, out["data_profiles"] = await host_data_nodes(host)
            if not nodes:
                out["notes"].append(f"no data interface profile with leaves found for '{host}'")

        profiles = []
        if nodes:
            for node in nodes:
                found = await find_infra_profiles(node)
                if not found:
                    out["notes"].append(
                        f"leaf {node}: no interface profile named "
                        + " / ".join(infra_profile_names(node)))
                profiles.extend(found)
        else:
            profiles = await _fabric_wide_descr_search(tokens)
        out["nodes_checked"] = nodes

        for prof in profiles:
            out["profiles_checked"].append(prof["name"])
            ports = await _ports_in_profile(prof, tokens)
            if not ports:
                out["notes"].append(f"{prof['name']}: no port block description contains "
                                    + " / ".join(tokens))
            out["ports"].extend(ports)
    except Exception as e:
        out["error"] = f"management port lookup failed: {e}"
        return out

    seen, uniq = set(), []
    for p in out["ports"]:
        if p["tdn"] not in seen:
            seen.add(p["tdn"])
            uniq.append(p)
    out["ports"] = uniq
    out["tdns"] = [p["tdn"] for p in uniq]
    if not uniq:
        out["error"] = (f"no management port found for '{host}' "
                        f"(checked: {', '.join(out['profiles_checked']) or 'no ESXI-Infra profile'}; "
                        + "; ".join(out["notes"]) + ")")
        return out
    out["label"] = (f"{host} management ("
                    + ", ".join(f"leaf{p['node']} {p['port']}" for p in uniq) + ")")
    return out


@tool
async def find_management_ports(host: str, nodes: list[str] | None = None) -> str:
    """Find a server/ESXi host's MANAGEMENT ports. Site convention: management
    ports live in the shared leaf interface profile ESXI-Infra-<leaf> or
    ESX-Infra-<leaf> (any mix of '-'/'_'), under selectors like ESXI-Infra-MSY-<leaf>; the port block
    whose description names the host is its management port. Pass the host
    serial or name (e.g. 'CX124553KT' or 'MSY_CX124553KT'); optionally the leaf
    IDs to check (default: the leaves of the host's data ports). Returns leaf +
    port, profile, selector, port-block description and binding path for each
    management port, plus what was searched."""
    res = await resolve_management_ports(host, nodes)
    return json.dumps(res, ensure_ascii=False)
