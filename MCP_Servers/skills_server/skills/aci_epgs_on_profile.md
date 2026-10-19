# ACI EPGs on a Profile - list every EPG bound to a server/interface profile

Use for "what are epgs on CZ24160G9N", "EPGs on profile/server/port", "profile'a bind edilmis EPGler", or "hangi EPGler bagli". Resolve the interface profile, then list ALL static EPG bindings.

## Scope

This is a read-only inventory task, not a create/bind or port-health task.
An interface profile (`infraAccPortP`) does not contain EPGs directly. Follow:

```
infraAccPortP -> infraHPortS -> infraPortBlk + infraRsAccBaseGrp
             -> infraRsAccPortP -> infraNodeP -> infraNodeBlk
             -> fabric path tDn -> fvRsPathAtt -> tenant / AP / EPG
```

Do not treat a serial/profile name as an endpoint name or query `fvCEp` for it.
Do not infer bound EPGs from VLAN suffixes, VLAN pools, AEP domains, or naming
similarity. Binding evidence is an actual `fvRsPathAtt` whose `tDn` matches.

## 1. Resolve the requested profile and retain selector ownership

Read **aci_resolve_identifier Path B** for the profile-to-node/port queries.
For the example, start with:

```
GET /api/node/class/infraAccPortP.json
    query-target-filter=wcard(infraAccPortP.name,"CZ24160G9N")
```

For a serial token, collect ALL matching profiles, including returned team
prefixes and DATA variants; do not invent profile names. For an explicitly
named full profile or profile DN, scope the primary answer to that profile.
If there are no matches, use the case/shorter-fragment retry from the resolver;
if still empty, report profile not found, not "no EPG bindings".

Read each profile with `rsp-subtree=full`. For EACH `infraHPortS`, retain its
own `infraPortBlk` ranges and `infraRsAccBaseGrp.tDn`. Never combine one
selector's ports with another selector's policy group. Reverse-resolve ALL
attached switch profiles via `infraRsAccPortP.tDn`, then expand their
`infraNodeBlk.from_` / `to_` ranges to node IDs.

The resolver/locator also discovers management-candidate profiles on the same
switch profiles. Keep these separate from the requested/matching profiles.
If included, label them as related management candidates; sharing a switch
profile alone is not proof that they belong to this server. Do not silently
merge their EPGs into the requested profile's EPG list.

Retain profile DN/name, selector, node IDs, ports, and policy-group DN for
every attachment. Reuse already resolved conversation evidence when available.

## 2. Resolve the actual binding paths (access versus PC/vPC)

Use the selector's `infraRsAccBaseGrp.tDn` to choose the path type. An interface
profile name and a policy-group name are NOT interchangeable.

- `.../accportgrp-<NAME>`: access. Each resolved node/physical port has a path
  like `topology/pod-<POD>/paths-<NODE>/pathep-[eth1/47]`.
- `.../accbundle-<NAME>`: PC or vPC. Read that `infraAccBndlGrp` MO; `lagT=link`
  means PC and `lagT=node` means vPC. The binding is on the bundle path, NOT
  on each member `eth` path. Keep member ports only as supporting location.

For bundle paths, discover APIC's actual path endpoints using the exact
policy-group name captured above:

```
GET /api/node/class/fabricPathEp.json
    query-target-filter=eq(fabricPathEp.name,"<BUNDLE_NAME>")
```

Retain only returned DNs matching the resolved nodes and bundle type:
`.../paths-<NODE>/pathep-[<BUNDLE_NAME>]` for PC;
`.../protpaths-<N1>-<N2>/pathep-[<BUNDLE_NAME>]` for vPC. Both vPC nodes must
belong to the resolved attachment set. Keep ALL matching paths, not just the
first one. Do not pair arbitrary nodes from a multi-node switch profile.

Use real pod IDs (resolve from `fabricNode` for the selected nodes if needed);
do not assume pod-1. For FEX/breakout or other nonstandard attachments, use
APIC-returned `fabricPathEp.dn` rather than fabricating a simple eth path.
Missing policy-group/path evidence is an unresolved attachment, not proof
that the profile has no EPGs. Report the gap and any partial results.

## 3. Enumerate ALL static EPG bindings for EVERY resolved path

For each unique path tDn:

```
GET /api/node/class/fvRsPathAtt.json
    query-target-filter=eq(fvRsPathAtt.tDn,"<EXACT_PATH_TDN>")
```

Do NOT add an EPG-name filter: discovering those names is the task. Do NOT
search `fvRsPathAtt.tDn` for the serial as the only check: access-port paths
contain node/eth, and bundle names can differ from the interface-profile name.

Follow pagination until all matches are collected (`page`, `page-size`,
`totalCount`); the first page or first matching path is not the complete list.
Continue across all profiles/selectors/paths even after finding EPGs.

From each `fvRsPathAtt` record retain:
- Parent EPG DN: the portion of `dn` before `/rspathAtt-[`.
- Tenant, application profile, and EPG from that parent DN.
- `tDn`, `encap`, `mode`, and `instrImedcy` as returned by APIC.
- Source interface profile/selector and resolved leaf/member ports.

Deduplicate binding records by full EPG DN + path tDn, retaining all profile
origins. Count distinct EPGs by full EPG DN, not just EPG name: different
tenants/APs may contain identically named EPGs. Keep per-path VLAN/mode values
even when the same EPG is present on multiple paths.

## 4. Report and stop

Report matched profile(s), distinct EPG count, and binding count. Show a compact
table: profile | tenant | AP | EPG | VLAN/encap | mode | leaf/port or bundle.
An EPG on two uplinks is one EPG with two bindings. Put related management
candidates in a separate section if queried.

Only say "no static EPG bindings found" when every requested attachment was
successfully resolved and every binding query completed with zero results.
For errors, missing paths, or incomplete pagination, label the answer partial
and identify the unresolved profiles/paths. Never turn a failed query into
an empty binding result.

Static configuration is not evidence of active endpoints or operational VLAN
deployment. Do not query health, faults, counters, or learned endpoints for
this request. If the user also asks about dynamic/VMM deployment, continue
with **aci_epg_deployment** using the resolved paths and label operational
`fvIfConn` evidence separately; a shared VMM domain alone does not prove an
EPG is deployed on this server.