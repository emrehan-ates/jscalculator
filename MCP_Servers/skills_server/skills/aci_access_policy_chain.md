# ACI Access Policy Chain — port ↔ interface profile ↔ policy group ↔ AEP ↔ domain (phys/VMM) ↔ VLAN pool

Use for policy group of a port (policy grubu), ports of a policy group, AEP of a PG, PGs/domains of an AEP, AEPs/pools of a phys/VMM domain, or why a VLAN is not deployed on a port.

This skill is the single source of truth for walking the fabric access-policy
relations in BOTH directions. For the tenant side (EPG static bindings, EPGs on
a port/server, VMM dynamic deployment) use **aci_epg_bindings**. For a full
"summarize X" answer use **aci_object_summary**. Site naming →
**aci_naming_conventions**.

## The relation graph

```
fabricNode (leaf N)
   ▲ infraNodeBlk (from_/to_)        under uni/infra/nprof-<SWPROF>/leaves-.../nodeblk-...
Switch profile  infraNodeP           uni/infra/nprof-<SWPROF>
   │ infraRsAccPortP.tDn
   ▼
Interface profile  infraAccPortP     uni/infra/accportprof-<IFPROF>
   │ infraHPortS (port selector)
   │   ├─ infraPortBlk  fromCard/fromPort … toCard/toPort  (= eth ports)
   │   └─ infraRsAccBaseGrp.tDn  ──►  policy group
   ▼
Policy group  infraAccPortGrp  uni/infra/funcprof/accportgrp-<PG>   (access)
              infraAccBndlGrp  uni/infra/funcprof/accbundle-<PG>    (lagT=link PC, lagT=node vPC)
   │ infraRsAttEntP.tDn
   ▼
AEP  infraAttEntityP                 uni/infra/attentp-<AEP>
   │ infraRsDomP.tDn                 (also gen-default/infraRsFuncToEpg = EPGs deployed on the whole AEP)
   ▼
Domain  physDomP uni/phys-<D> | vmmDomP uni/vmmp-VMware/dom-<D> | l3extDomP uni/l3dom-<D>
   │ infraRsVlanNs.tDn
   ▼
VLAN pool  fvnsVlanInstP             uni/infra/vlanns-[<POOL>]-static|dynamic
   └─ fvnsEncapBlk  from / to        (e.g. vlan-100 … vlan-199)

Tenant side: EPG ─fvRsDomAtt─► Domain   and   EPG ─fvRsPathAtt─► fabric path   (aci_epg_bindings)
```

Every arrow is a relation object you can query **forward** (read the parent
with `rsp-subtree`) or **reverse** (query the relation class with
`eq(<RelClass>.tDn,"<target dn>")` and take the parent part of each result `dn`).

## Reverse-lookup cheat sheet (one call each)

| Question | Query | Answer is in |
|---|---|---|
| Which switch profiles contain node N | `infraNodeBlk` `and(le(infraNodeBlk.from_,"N"),ge(infraNodeBlk.to_,"N"))` | `nprof-<SWPROF>` segment of each `dn` |
| Which switch profiles bind interface profile X | `infraRsAccPortP` `eq(infraRsAccPortP.tDn,"uni/infra/accportprof-X")` | `nprof-...` in `dn` |
| Which selectors/interface profiles use PG X | `infraRsAccBaseGrp` `eq(infraRsAccBaseGrp.tDn,"<PG dn>")` | `accportprof-<IFPROF>/hports-<SEL>-typ-range` in `dn` |
| Which PGs use AEP X | `infraRsAttEntP` `eq(infraRsAttEntP.tDn,"uni/infra/attentp-X")` | `accportgrp-...` / `accbundle-...` in `dn` |
| Which AEPs carry domain D | `infraRsDomP` `eq(infraRsDomP.tDn,"<domain dn>")` | `attentp-<AEP>` in `dn` |
| Which domains use VLAN pool P | `infraRsVlanNs` `eq(infraRsVlanNs.tDn,"<pool dn>")` | domain DN prefix of `dn` |
| Which EPGs use domain D | `fvRsDomAtt` `eq(fvRsDomAtt.tDn,"<domain dn>")` | `uni/tn-T/ap-AP/epg-E` prefix of `dn` |
| Which pool contains VLAN V | `/api/node/class/fvnsEncapBlk.json` (small class) and compare `from`/`to` NUMERICALLY yourself — `le`/`ge` on `vlan-N` strings compare as text and give wrong answers | `vlanns-[...]` in `dn` |

Combine several targets in ONE call with `or(eq(...),eq(...))` instead of one
call per target.

## Path PG — known leaf + port → policy group (most asked)

Input: `node-N` + `ethX/Y` (from the user, conversation history,
`fvCEp.fabricPathDn`, LLDP, or **aci_resolve_identifier**).

**PG0 – bundle shortcut.** If the known path is `.../protpaths-N1-N2/pathep-[B]`
or `.../paths-N/pathep-[B]` and `B` is not `eth...`, then `B` IS the PC/vPC
policy-group name:
`/api/node/mo/uni/infra/funcprof/accbundle-B.json?rsp-subtree=full`. Done.

**PG1 – switch profiles of the node:**
`/api/node/class/infraNodeBlk.json?query-target-filter=and(le(infraNodeBlk.from_,"N"),ge(infraNodeBlk.to_,"N"))`
→ unique `uni/infra/nprof-<SWPROF>`. 0 results → node is in no switch profile; report it.

**PG2 – interface profiles of those switch profiles:**
`/api/node/mo/uni/infra/nprof-<SWPROF>.json?rsp-subtree=children&rsp-subtree-class=infraRsAccPortP`
→ each `infraRsAccPortP.tDn`.

**PG3 – find the selector that covers the port:** for each interface profile
`/api/node/mo/<ifprof dn>.json?rsp-subtree=full`. A `infraPortBlk` covers
`ethC/P` when `fromCard ≤ C ≤ toCard` and `fromPort ≤ P ≤ toPort`. The
`infraRsAccBaseGrp.tDn` on the SAME `infraHPortS` is the policy group.
Several matching selectors → report all.

**PG4 – enrich only if asked** (AEP, domains, interface policies):
read the PG with `rsp-subtree=full`, then continue forward below.

Stop as soon as you have the PG. Never wcard `infraAccPortGrp` /
`infraAccBndlGrp` with `eth1/X` or a MAC — those classes have no such attribute.

## Path PORTS — policy group → interface profiles, ports, leaves

1. `infraRsAccBaseGrp` with `eq(infraRsAccBaseGrp.tDn,"<PG dn>")` → every
   selector using the PG (`.../accportprof-<IFPROF>/hports-<SEL>-typ-range/rsaccBaseGrp`).
2. For each interface profile: `/api/node/mo/uni/infra/accportprof-<IFPROF>.json?rsp-subtree=full`
   → the `infraPortBlk` ranges of THOSE selectors only (never mix selectors).
3. `infraRsAccPortP` with `eq(infraRsAccPortP.tDn,"uni/infra/accportprof-<IFPROF>")`
   → switch profiles → read each with
   `rsp-subtree=full&rsp-subtree-class=infraNodeBlk` → node IDs.
4. Result: (node × port) list. For a vPC PG the members are spread over the two
   leaves; for a PC all members are usually on one leaf.

## Path UP — policy group → AEP → domains → VLAN pools

1. `/api/node/mo/<PG dn>.json?rsp-subtree=full` → `infraRsAttEntP.tDn` (AEP).
   No `infraRsAttEntP` = broken chain; report it.
2. `/api/node/mo/uni/infra/attentp-<AEP>.json?rsp-subtree=full` →
   `infraRsDomP.tDn` (domains) and `infraRsFuncToEpg` (EPGs deployed on the whole
   AEP: `tDn`, `encap`, `mode`).
3. For each domain `/api/node/mo/<domain dn>.json?rsp-subtree=full` →
   `infraRsVlanNs.tDn` (pool); for a VMM domain also `vmmCtrlrP` (vCenter).
4. `/api/node/mo/<pool dn>.json?rsp-subtree=full` → `fvnsEncapBlk` ranges and
   `allocMode`.

## Path DOWN — domain / AEP → who uses it

- VMM or phys domain → AEPs: `infraRsDomP` reverse (cheat sheet), then AEPs →
  PGs: `infraRsAttEntP` reverse, then PGs → ports: Path PORTS.
- Domain → EPGs: `fvRsDomAtt` reverse.
- Large fan-out (an AEP used by hundreds of PGs): report `totalCount` and a
  sample; do not walk every PG to its ports unless the user asked.

## Why is VLAN V not working on port P (consistency check)

All of these must hold:

1. Port is covered by a selector → PG (Path PG).
2. PG has an AEP; AEP has ≥1 domain.
3. The EPG's `fvRsDomAtt` contains one of those SAME domains.
4. That domain's VLAN pool contains V.
5. The EPG has a binding for the port/bundle with encap `vlan-V`
   (**aci_epg_bindings**) — or the AEP has `infraRsFuncToEpg` for it, or a VMM
   dynamic `fvIfConn` exists.

Report the first link that breaks. Most common breaks: PG without AEP, AEP
missing the EPG's domain, VLAN outside pool range, binding on the eth path
while the port is actually a vPC member (binding must be on the bundle path).

## Answering

- Build the answer from EVERY result you collected, including partial chains:
  e.g. "port → PG = X, PG → AEP = Y; AEP domains query returned nothing".
- Empty result = "not found with this exact filter", not proof the object
  does not exist — say which filter was empty.
- Only if every query returned nothing/errors: say plainly that you could not
  find it and list what you searched. Never fill gaps with naming guesses.

## Hand-off

- Need leaf+port from MAC / serial / hostname → **aci_resolve_identifier**, then return here.
- EPGs on the port / static bindings / VMM deployment → **aci_epg_bindings**.
- Port up/down, CRC → **aci_interface_troubleshooting**.
