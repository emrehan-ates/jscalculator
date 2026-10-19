# ACI Object Map — identify what a name is and which class to query

Type → class/DN lookup table: maps "EPG, BD, policy group, AEP, VMM domain, VLAN pool, interface profile, switch…" to the exact ACI class, DN shape and relation objects.

## Core rule

If the user names the TYPE of object, query that type's class DIRECTLY. "the XXX bridge domain" means query fvBD for XXX — do NOT look at interface profiles, policy groups, or other classes first. Probing wrong classes one by one is the main cause of slow, failed lookups. Map the stated type to its class with the table below, then query only that class.

If the user gives only a name with no type, classify it with aci_naming_conventions.

Use aci_inventory_validation for "how many". Use aci_object_summary to
summarize one named object, aci_access_policy_chain for port/PG/AEP/domain
relations, and aci_epg_bindings for EPG static bindings.

## Type -> class map

| User says | ACI class | DN shape |
|---|---|---|
| tenant | fvTenant | uni/tn-NAME |
| VRF / context / private network | fvCtx | uni/tn-T/ctx-NAME |
| bridge domain / BD | fvBD | uni/tn-T/BD-NAME |
| application profile / AP | fvAP | uni/tn-T/ap-NAME |
| EPG | fvAEPg | uni/tn-T/ap-AP/epg-NAME |
| subnet / gateway | fvSubnet | under a BD or EPG |
| endpoint / host / MAC | fvCEp | learned endpoint (query by MAC) |
| static binding (EPG-to-port) | fvRsPathAtt | under an EPG (tDn, encap, mode) |
| static leaf binding | fvRsNodeAtt | under an EPG |
| AEP-level EPG binding | infraRsFuncToEpg | uni/infra/attentp-A/gen-default |
| VMM dynamic deployment (operational) | fvIfConn | uni/epp/fv-[EPG dn]/node-N/dyatt-[path]/... |
| fabric path endpoint (port / PC / vPC path) | fabricPathEp | topology/pod-1/(prot)paths-.../pathep-[...] |
| L3Out | l3extOut | uni/tn-T/out-NAME |
| external EPG | l3extInstP | under an L3Out |
| L3Out logical node profile (border leaves) | l3extLNodeP | under an L3Out (see aci_l3out_troubleshooting) |
| L3Out logical interface profile | l3extLIfP | under a logical node profile |
| external subnet (route classification) | l3extSubnet | under an external EPG |
| BGP peer (operational adjacency) | bgpPeerEntry | under a border node; operSt=established (see aci_l3out_troubleshooting) |
| OSPF adjacency (operational) | ospfAdjEp | under a border node; operSt=full |
| EIGRP adjacency (operational) | eigrpAdjEp | under a border node |
| routed prefix / route table (IPv4) | uribv4Route | under a node; query by prefix + node |
| physical interface (config) | l1PhysIf | topology/pod-P/node-N/sys/phys-[eth1/X] |
| physical interface (operational) | ethpmPhysIf | child of l1PhysIf |
| interface profile (leaf access) | infraAccPortP | uni/infra/accportprof-NAME |
| port selector | infraHPortS | under an interface profile |
| switch profile (leaf) | infraNodeP | uni/infra/nprof-NAME |
| access port policy group | infraAccPortGrp | uni/infra/funcprof/accportgrp-NAME |
| PC / vPC policy group | infraAccBndlGrp | uni/infra/funcprof/accbundle-NAME |
| vPC domain (peer state) | vpcDom | node-N/sys/vpc/inst/dom (see aci_vpc_health) |
| vPC interface member (consistency) | vpcIf | under the node vpc instance (see aci_vpc_health) |
| port-channel / vPC aggregate interface | pcAggrIf | under a node; operSt + members |
| vPC protection group (leaf pair) | fabricExplicitGEp | uni/fabric/protpol/expgep-NAME |
| AEP | infraAttEntityP | uni/infra/attentp-NAME |
| VLAN pool | fvnsVlanInstP | uni/infra/vlanns-[NAME]-static / -dynamic |
| VLAN range (encap block) | fvnsEncapBlk | under a VLAN pool (from, to) |
| link level policy | fabricHIfPol | uni/infra/hintfpol-NAME |
| CDP / LLDP interface policy | cdpIfPol / lldpIfPol | uni/infra/cdpIfP-NAME / lldpIfP-NAME |
| port-channel (LACP) policy | lacpLagPol | uni/infra/lacplagp-NAME |
| MCP / STP / L2 interface policy | mcpIfPol / stpIfPol / l2IfPol | uni/infra/mcpIfP- / ifPol- / l2IfP-NAME |
| switch (leaf) policy group | infraAccNodePGrp | uni/infra/funcprof/accnodepgrp-NAME |
| vCenter of a VMM domain | vmmCtrlrP | uni/vmmp-VMware/dom-D/ctrlr-NAME |
| physical domain | physDomP | uni/phys-NAME |
| L3 domain | l3extDomP | uni/l3dom-NAME |
| VMM domain | vmmDomP | uni/vmmp-VENDOR/dom-NAME |
| node / leaf / spine / switch | fabricNode | topology/pod-P/node-N |
| APIC controller (cluster member) | topSystem | role=controller; state=in-service (see aci_apic_cluster_health) |
| APIC cluster member health | infraWiNode | health=fully-fit (see aci_apic_cluster_health) |
| APIC cluster target size / quorum | infraClusterPol | uni/controller/clusterpol |
| running firmware (switch node) | firmwareRunning | per node; version (see aci_firmware_maintenance) |
| running firmware (APIC controller) | firmwareCtrlrRunning | per controller; version |
| upgrade / maintenance job | maintUpgJob | per node; upgradeStatus + desiredVersion |
| LLDP neighbor (attached device) | lldpAdjEp | under node-N/sys/lldp (query by chassisIdV / sysName) |
| CDP neighbor | cdpAdjEp | under node-N/sys/cdp |
| audit log / config change / "who changed" | aaaModLR | append-only log (see aci_change_audit) |
| operational event (link up/down etc.) | eventRecord | append-only log (see aci_change_audit) |
| virtual machine (VMware) | compVm | under comp/prov-VMware (see aci_vmm_visibility) |
| ESXi host / hypervisor | compHv | under comp/prov-VMware (see aci_vmm_visibility) |
| VM vNIC | compVNic | child of compVm |

To query a class: /api/node/class/<CLASS>.json (optionally with a name filter).
To read one object plus its children: /api/node/mo/<DN>.json?rsp-subtree=full

## Relationship pointers (how objects link)
- EPG -> BD: fvRsBd (tnFvBDName)
- BD -> VRF: fvRsCtx (tnFvCtxName)
- BD -> L3Out: fvRsBDToOut
- EPG -> domain: fvRsDomAtt
- policy group -> AEP: infraRsAttEntP
- AEP -> domain: infraRsDomP
- AEP -> EPG (deployed on all AEP ports): infraRsFuncToEpg
- EPG -> port/bundle: fvRsPathAtt (tDn = fabric path)
- policy group -> interface policies: infraRsHIfPol, infraRsCdpIfPol,
  infraRsLldpIfPol, infraRsLacpPol, infraRsMcpIfPol, infraRsStpIfPol, infraRsL2IfPol
- domain -> VLAN pool: infraRsVlanNs
- interface profile -> policy group: infraRsAccBaseGrp (under the port selector)
- switch profile -> interface profile: infraRsAccPortP
- **physical leaf port (node + eth) -> policy group: NOT a direct class query.**
  Reverse walk only: node → infraNodeBlk → switch profile → infraRsAccPortP →
  interface profile subtree → matching infraPortBlk → infraRsAccBaseGrp.tDn
  (full procedure: aci_access_policy_chain Path PG). Never filter
  infraAccPortGrp/infraAccBndlGrp by eth port or MAC.
- Every relation can be walked in reverse with
  eq(<RelClass>.tDn,"<target dn>") — see the cheat sheet in aci_access_policy_chain.
- L3Out -> VRF: l3extRsEctx
- L3Out border node -> node + router ID: l3extRsNodeL3OutAtt (rtrId)
- L3Out interface -> physical path: l3extRsPathL3OutAtt

## Naming conventions

Site naming (MSY_/CSY_/SG_ prefixes, serial-named profiles and bundles, pod
and node ID ranges, switch hostnames) lives in **aci_naming_conventions**.

Contracts, subjects and filters are out of scope (vzAny permitAny) — they are
intentionally not mapped here.
