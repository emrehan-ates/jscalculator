# ACI EPG Bindings — static bindings of an EPG, EPGs on a port/server/policy group, VMM dynamic deployment

Use for static bindings of an EPG (static binding'leri), which EPGs/VLANs are on a port, server serial, interface profile or policy group, EPGs of a VLAN, or is EPG X deployed on port Y.

Tenant-side counterpart of **aci_access_policy_chain**. Read-only. Contracts
are out of scope (vzAny permitAny) — never query `fvRsCons`/`fvRsProv`/`vz*`.

## How an EPG reaches a port (four mechanisms — check the ones that apply)

| Mechanism | Object | Where | Key attributes |
|---|---|---|---|
| Static port binding | `fvRsPathAtt` | child of the EPG | `tDn` (path), `encap`, `mode`, `instrImedcy` |
| Static leaf binding | `fvRsNodeAtt` | child of the EPG | `tDn` (`topology/pod-1/node-N`), `encap`, `mode` |
| AEP-level binding | `infraRsFuncToEpg` | `uni/infra/attentp-<AEP>/gen-default` | `tDn` (EPG dn), `encap`, `mode` — applies to EVERY port whose PG uses that AEP |
| VMM dynamic | `fvIfConn` (operational) | `uni/epp/fv-[<EPG dn>]/node-N/dyatt-[<path>]/...` | `encap`; EPG needs `fvRsDomAtt` → `uni/vmmp-VMware/dom-...` |

`mode`: `regular` = trunk/tagged, `untagged` = access, `native` = 802.1p/native.

### Path tDn shapes

- Access port: `topology/pod-1/paths-<N>/pathep-[eth1/X]`
- PC: `topology/pod-1/paths-<N>/pathep-[<BUNDLE>]`
- vPC: `topology/pod-1/protpaths-<N1>-<N2>/pathep-[<BUNDLE>]`
- FEX: `topology/pod-1/paths-<N>/extpaths-<FEX>/pathep-[eth1/X]`

`<BUNDLE>` is the PC/vPC policy-group name, usually `<TEAM>_<SERIAL>` — so a
bundle path already tells you the server (see **aci_naming_conventions**).
An eth port that is a member of a PC/vPC carries its bindings on the BUNDLE
path, never on its own eth path.

## A. Static bindings of a named EPG ("X EPG'sinin static binding'leri")

1. One call returns the EPG and its bindings:
   `/api/node/class/fvAEPg.json?query-target-filter=eq(fvAEPg.name,"<EPG>")&rsp-subtree=children&rsp-subtree-class=fvRsPathAtt,fvRsNodeAtt,fvRsDomAtt`
   0 results → retry once with `wcard(fvAEPg.name,"<EPG>")`. Several EPGs with
   the same name in different tenants/APs → report each separately (or ask).
2. AEP-level bindings (cheap, do it): `/api/node/class/infraRsFuncToEpg.json?query-target-filter=eq(infraRsFuncToEpg.tDn,"<EPG dn>")`
3. Translate each path for the reader:
   - bundle → bundle name (= server/PG), leaf pair; expand to member ports only
     if asked (**aci_access_policy_chain** Path PORTS).
   - eth path → node + port; name the server only if you resolved its
     interface profile (Path PG) — do not guess.
4. If the EPG has a VMM domain in `fvRsDomAtt`, say so: dynamic ports are not
   static bindings; offer/do step D.

Report: tenant / AP / EPG, binding count, table `leaf(s) | port or bundle | encap | mode | immediacy`, domains.

## B. EPGs on a port, server, interface profile or policy group

1. Resolve the target to the exact path tDn(s):
   - leaf + eth port → first find its PG (**aci_access_policy_chain** Path PG).
     Access PG → path `paths-N/pathep-[ethX/Y]`. PC/vPC PG → bundle path.
   - server serial / interface profile → **aci_resolve_identifier** Path B,
     keeping each selector's PG; then same rule per selector.
   - management ports of a host → `find_management_ports(host=...)`; use the
     returned `tdns` (access paths on the ESXI-Infra ports).
   - policy group name → access: its ports (Path PORTS) → eth paths;
     bundle: `/api/node/class/fabricPathEp.json?query-target-filter=eq(fabricPathEp.name,"<BUNDLE>")`
     → keep paths whose nodes belong to the resolved switch profiles.
2. ONE query for all paths:
   `/api/node/class/fvRsPathAtt.json?query-target-filter=or(eq(fvRsPathAtt.tDn,"<path1>"),eq(fvRsPathAtt.tDn,"<path2>"))`
   Never add an EPG-name filter here — discovering the EPGs is the task.
   Follow `totalCount`: if larger than returned objects, fetch the next page.
3. Add AEP-level EPGs: PG → AEP (Path UP step 1) →
   `/api/node/mo/uni/infra/attentp-<AEP>.json?rsp-subtree=full&rsp-subtree-class=infraRsFuncToEpg`.
4. Add static leaf bindings for the node(s) if relevant:
   `eq(fvRsNodeAtt.tDn,"topology/pod-1/node-N")`.
5. VMM (only if asked or if static results are empty and the AEP carries a VMM
   domain): step D.
6. Parent EPG of each binding = the part of `dn` before `/rspathAtt-[`.
   Deduplicate by full EPG dn (same name can exist in several tenants).

Report: per target, distinct EPG count + table
`tenant | AP | EPG | encap | mode | via (static path / AEP / VMM)`.
If both data and management ports were asked, report them in separate sections.

## C. EPGs of a VLAN

`/api/node/class/fvRsPathAtt.json?query-target-filter=eq(fvRsPathAtt.encap,"vlan-<V>")&page-size=50`
→ parent EPG dns. Also check `infraRsFuncToEpg` and `fvRsNodeAtt` with the
same encap filter. Report distinct EPGs; `totalCount` = binding count.

## D. VMM dynamic deployment of an EPG

1. `fvRsDomAtt` children of the EPG: a `uni/vmmp-VMware/dom-<D>` tDn means VMM.
   None → the EPG has no VMM deployment; stop this step.
2. Where it is deployed:
   `/api/node/class/fvIfConn.json?query-target-filter=wcard(fvIfConn.dn,"epg-<EPG>]")&page-size=50`
   Narrow with an extra `wcard(fvIfConn.dn,"<BUNDLE or node-N>")` term when the
   question is about one port/server.
3. Results → the path inside `dyatt-[...]` and `encap`. No results with a VMM
   domain → no VM of that EPG is on those hosts now (or on-demand immediacy).

`fvIfConn` is operational state; label it separately from static config.

## E. Is EPG X deployed on port/server Y (yes/no)

Run A for EPG X, resolve Y's path(s) as in B.1, and compare: match on path tDn
→ yes (report encap/mode). No static match → check AEP-level (B.3) → then VMM
(D, filtered to Y). Still nothing → "not bound". If it is bound but the user
says traffic does not pass, run the consistency check in
**aci_access_policy_chain** (domain/VLAN pool) and
**aci_interface_troubleshooting** (port state).

## Answering

- Use everything collected: e.g. "3 static bindings found; AEP-level query
  empty; VMM not checked".
- Say "no static EPG bindings" only when every resolved path was queried and
  returned 0. If path resolution failed, the answer is "could not resolve the
  port/path", not "no EPGs".
- If ALL queries were empty, say you could not find it and what you searched.
- Static config ≠ live traffic; do not query faults/counters for this task
  unless asked.
