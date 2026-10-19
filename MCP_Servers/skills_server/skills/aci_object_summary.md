# ACI Object Summary — "X nedir / özetle": summarize any tenant or access/fabric policy object and its relations

Use for "what is X", "summarize EPG/BD/tenant/policy group/AEP/domain/VLAN pool/interface profile", "X'i özetle", properties or config of one named object, and what it is attached to.

Goal: a short, readable summary built from 2–4 targeted reads — the object's
own attributes plus its first-hop relations. Contracts are out of scope
(vzAny permitAny): never read or report `fvRsCons`, `fvRsProv`, `vzBrCP`,
`vzSubj`, `vzFilter`.

## Method (always)

1. **Identify the type.** If the user said it ("X EPG'si", "policy group X")
   use that class (table below / **aci_object_map**). If not, classify the name
   with **aci_naming_conventions** and try the likely classes in order.
2. **Locate + read in ONE call** with the relations you need:
   `/api/node/class/<CLASS>.json?query-target-filter=eq(<CLASS>.name,"<NAME>")&rsp-subtree=children`
   (or `rsp-subtree=full` when relations are nested, e.g. interface profiles).
   0 results → retry once with `wcard(<CLASS>.name,"<NAME>")`. Multiple hits
   (same name in several tenants) → summarize each briefly or ask which one.
3. **One-hop enrichment** — only the reverse lookups listed for that type.
   Use `or(eq(...),eq(...))` to batch several targets into one call.
4. **Write the summary** in the template at the bottom.

## Tenant objects

### EPG — `fvAEPg` (`uni/tn-T/ap-AP/epg-E`)
- Read: class query with `rsp-subtree=children&rsp-subtree-class=fvRsBd,fvRsDomAtt,fvRsPathAtt,fvRsNodeAtt,fvSubnet`.
- Own attributes worth reporting: `descr`, `pcEnfPref`, `prefGrMemb`, `floodOnEncap`, `shutdown`, `isAttrBasedEPg`.
- Relations: BD (`fvRsBd.tnFvBDName`), domains (`fvRsDomAtt.tDn` → phys vs VMM, `resImedcy`/`instrImedcy`), static bindings (count + short list; full list via **aci_epg_bindings** A), EPG subnets.
- Enrich: BD → VRF + gateway in one read: `/api/node/mo/uni/tn-T/BD-<BD>.json?rsp-subtree=children&rsp-subtree-class=fvRsCtx,fvSubnet`; AEP-level bindings: `eq(infraRsFuncToEpg.tDn,"<EPG dn>")`.

### Bridge domain — `fvBD` (`uni/tn-T/BD-B`)
- Attributes: `unicastRoute`, `arpFlood`, `unkMacUcastAct`, `multiDstPktAct`, `limitIpLearnToSubnets`, `mac`, `descr`.
- Children: `fvSubnet` (`ip`, `scope`), `fvRsCtx.tnFvCtxName` (VRF), `fvRsBDToOut` (L3Outs).
- Reverse: EPGs in this BD → `/api/node/class/fvRsBd.json?query-target-filter=and(eq(fvRsBd.tnFvBDName,"B"),wcard(fvRsBd.dn,"uni/tn-T/"))`.

### VRF — `fvCtx` (`uni/tn-T/ctx-V`)
- Attributes: `pcEnfPref`, `pcEnfDir`, `descr`.
- Reverse: BDs → `and(eq(fvRsCtx.tnFvCtxName,"V"),wcard(fvRsCtx.dn,"uni/tn-T/"))`; L3Outs → `l3extRsEctx` with `tnFvCtxName`.

### Application profile — `fvAP` (`uni/tn-T/ap-A`)
- `rsp-subtree=children&rsp-subtree-class=fvAEPg` → EPG list (+ count).

### Tenant — `fvTenant` (`uni/tn-T`)
- `rsp-subtree=children&rsp-subtree-class=fvCtx,fvBD,fvAP,l3extOut` → counts and names per type. For EPG count use `/api/node/class/fvAEPg.json?query-target-filter=wcard(fvAEPg.dn,"uni/tn-T/")&page-size=1` (`totalCount`).

### L3Out — `l3extOut`: brief summary only (VRF, L3 domain, node profiles, external EPGs); routing detail → **aci_l3out_troubleshooting**.

## Access (fabric) policy objects

### Policy group — `infraAccPortGrp` (access) / `infraAccBndlGrp` (PC/vPC)
- Unknown type → query BOTH classes by name (two calls) or read both DNs:
  `uni/infra/funcprof/accportgrp-<PG>` and `uni/infra/funcprof/accbundle-<PG>`.
- Read with `rsp-subtree=full`. Report:
  - Type: access, or `lagT` = `link` (PC) / `node` (vPC).
  - AEP: `infraRsAttEntP.tDn`.
  - Interface policies (relation → attribute with the policy name; empty name = default policy):
    link level `infraRsHIfPol.tnFabricHIfPolName`, CDP `infraRsCdpIfPol.tnCdpIfPolName`,
    LLDP `infraRsLldpIfPol.tnLldpIfPolName`, port-channel `infraRsLacpPol.tnLacpLagPolName`,
    MCP `infraRsMcpIfPol.tnMcpIfPolName`, STP `infraRsStpIfPol.tnStpIfPolName`,
    L2 `infraRsL2IfPol.tnL2IfPolName`, storm control `infraRsStormctrlIfPol.tnStormctrlIfPolName`.
- Enrich (one call each): where it is used → `eq(infraRsAccBaseGrp.tDn,"<PG dn>")`
  (interface profiles + selectors; ports/leaves via **aci_access_policy_chain** Path PORTS);
  AEP → domains (Path UP). EPGs on it only if asked (**aci_epg_bindings** B).
- Naming: `<TEAM>_<SERIAL>` → name the owning team/server (aci_naming_conventions).

### Interface policy (link level / CDP / LLDP / LACP / MCP / STP / L2 / storm control)
- Classes + DN: `fabricHIfPol` `uni/infra/hintfpol-N` (`speed`, `autoNeg`, `fecMode`);
  `cdpIfPol` `uni/infra/cdpIfP-N` (`adminSt`); `lldpIfPol` `uni/infra/lldpIfP-N` (`adminRxSt`, `adminTxSt`);
  `lacpLagPol` `uni/infra/lacplagp-N` (`mode`: active/passive/off/mac-pin, `ctrl`);
  `mcpIfPol` `uni/infra/mcpIfP-N` (`adminSt`); `stpIfPol` `uni/infra/ifPol-N` (`ctrl`);
  `l2IfPol` `uni/infra/l2IfP-N` (`vlanScope`, `qinq`); `stormctrlIfPol` `uni/infra/stormctrlifp-N`.
- Reverse (which PGs use it): the matching relation class with its name
  attribute, e.g. `/api/node/class/infraRsCdpIfPol.json?query-target-filter=eq(infraRsCdpIfPol.tnCdpIfPolName,"N")&page-size=50` → `totalCount` + sample PGs.

### AEP — `infraAttEntityP` (`uni/infra/attentp-A`)
- Read `rsp-subtree=full`: domains (`infraRsDomP.tDn`), AEP-level EPGs
  (`infraRsFuncToEpg`: EPG, `encap`, `mode`), infra VLAN (`infraProvAcc` present).
- Reverse: PGs using it → `eq(infraRsAttEntP.tDn,"uni/infra/attentp-A")` (`totalCount` + names).

### Domain — `physDomP` `uni/phys-D` | `vmmDomP` `uni/vmmp-VMware/dom-D` | `l3extDomP` `uni/l3dom-D`
- Read `rsp-subtree=children`: VLAN pool (`infraRsVlanNs.tDn`); VMM also
  `vmmCtrlrP` (`hostOrIp`, `rootContName` = vCenter + datacenter), `encapMode`, `enfPref`.
- Reverse: AEPs → `eq(infraRsDomP.tDn,"<dom dn>")`; EPGs → `eq(fvRsDomAtt.tDn,"<dom dn>")` (`totalCount` + sample).
- VMM health/inventory (controller online, hosts, VMs) → **aci_vmm_visibility**.

### VLAN pool — `fvnsVlanInstP` (`uni/infra/vlanns-[P]-static|dynamic`)
- Read `rsp-subtree=children`: `allocMode`, `fvnsEncapBlk` ranges (`from`, `to`, `allocMode`, `role`).
- Reverse: domains using it → `eq(infraRsVlanNs.tDn,"<pool dn>")`.

### Interface profile — `infraAccPortP` (`uni/infra/accportprof-I`)
- Read `rsp-subtree=full`: per selector (`infraHPortS`) its port blocks
  (`infraPortBlk` → `eth<fromCard>/<fromPort>`–`<toPort>`) and its PG
  (`infraRsAccBaseGrp.tDn`). Never mix one selector's ports with another's PG.
- Reverse: switch profiles → `eq(infraRsAccPortP.tDn,"<ifprof dn>")`, then node IDs.

### Switch profile — `infraNodeP` (`uni/infra/nprof-S`)
- Read `rsp-subtree=full`: node blocks (`infraNodeBlk` `from_`/`to_`), bound
  interface profiles (`infraRsAccPortP.tDn`), switch policy group (`infraRsAccNodePGrp`).
- Optionally map node IDs to hostnames with one `fabricNode` query
  (`or(eq(fabricNode.id,"101"),eq(fabricNode.id,"102"))`).

### Fabric-wide policies (Fabric > Fabric Policies) — summarize only if asked
- Pod policy group `fabricPodPGrp` (`uni/fabric/funcprof/podpgrp-N`): relations to
  date/time (`fabricRsTimePol`), SNMP (`fabricRsSnmpPol`), BGP RR (`fabricRsPodPGrpBGPRRP`), COOP, ISIS.
- NTP `datetimePol` + `datetimeNtpProv`; DNS `dnsProfile` + `dnsProv`; SNMP `snmpPol`;
  BGP route reflector `bgpInstPol` / `bgpRRNodePEp`; vPC pairs `fabricExplicitGEp`.
- Unknown class → `aci_lookup_class` with a plain-English description.

## Summary template

```
<Type> <name>  (<dn>)
- What it is: one sentence in plain language (+ owner team/server from naming, if applicable)
- Key settings: only non-default / meaningful attributes
- Relations: up (what it uses) and down (who uses it), with counts
- Notes: anything missing or inconsistent (e.g. PG without AEP, EPG without domain)
- Not checked / not found: queries that were empty or skipped
```

Answer in the user's language (Turkish or English). Report real attribute
values, not just DNs. Keep it short; offer details (full binding list, ports,
VLAN ranges) as a follow-up instead of dumping everything.

## Answering with partial or empty data

- If the object was found but some enrichment queries were empty, still write
  the summary with what you have and list the empty relations under
  "Not checked / not found".
- Only when the object itself could not be located in any likely class, say
  you could not find it and list the classes/filters tried. Never invent values.
