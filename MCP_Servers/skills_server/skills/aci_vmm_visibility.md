# ACI VMM Visibility — VMs, ESXi hosts, and VMM domains (for the VMware team)

Use this skill for questions about virtual machines, ESXi hosts, vCenter/VCF integration, or VMM domains: "where is VM X", "which EPGs are in VMM domain Y", "which hosts are in the VMM inventory", "is the vCenter controller connected". APIC learns the vCenter inventory through the VMM integration and exposes it under the comp* classes (comp/prov-VMware/...).

## Key classes

- **compVm** — a virtual machine. name = the VM name in vCenter.
- **compVNic** — a VM's vNIC (child of compVm). mac = the vNIC MAC, operSt = up/down.
- **compHv** — a hypervisor (ESXi host). name = the host name as vCenter knows it.
- **compCtrlr** — the vCenter controller itself. operSt = online means the APIC↔vCenter connection is healthy.
- **vmmDomP** — the VMM domain (uni/vmmp-VMware/dom-NAME).
- **fvIfConn** — where an EPG's VLAN is dynamically deployed via VMM (see aci_epg_bindings section D).

## "Where is VM X" (the most common question)

1. Find the VM and its vNIC MACs in one query:
```
/api/node/class/compVm.json?query-target-filter=wcard(compVm.name,"VMNAME")&rsp-subtree=children&rsp-subtree-class=compVNic
```
   wcard because vCenter names often include suffixes. Record each compVNic.mac.
2. Locate each MAC in the fabric: fvCEp by MAC (aci_endpoint_troubleshooting). fabricPathDn = the leaf/port or vPC where the host's uplink is; the dn gives the EPG; encap gives the VLAN.
3. If port health matters, continue with aci_interface_troubleshooting on that path.

Report: VM name, vNIC MAC(s), EPG, VLAN, and the physical leaf/port (usually a vPC bundle named after the host serial, e.g. CSY_<SERIAL>).

## Other question patterns

- "Which EPGs are in VMM domain D": exact reverse lookup on the domain DN:
  /api/node/class/fvRsDomAtt.json?query-target-filter=eq(fvRsDomAtt.tDn,"uni/vmmp-VMware/dom-D")
  Parse each result dn for tenant/AP/EPG. (wcard "dom-D" would also match dom-D2, dom-DX…)
- "Which AEPs / policy groups / ports carry VMM domain D": aci_access_policy_chain Path DOWN
  (infraRsDomP reverse → AEPs → infraRsAttEntP reverse → PGs → ports).
- "Summarize VMM domain D" (vCenter, VLAN pool, AEPs, EPG count): aci_object_summary (Domain section).
- "Which ESXi hosts does APIC see": /api/node/class/compHv.json (filter by name with wcard if needed). A host missing here but present in vCenter suggests a VMM inventory sync problem.
- "Is the vCenter connection healthy": /api/node/class/compCtrlr.json — check operSt (online) and report the controller name/address. If offline, VMM-deployed VLANs may be stale; check faults on the vmmDomP dn (aci_fault_investigation).
- "Which physical port does host H uplink through": the host advertises itself via LLDP/CDP — use aci_resolve_identifier Path C (lldpAdjEp.sysName = the ESXi hostname). Cross-check with the vPC bundle naming convention (CSY_/MSY_ + serial).

## Migration validation (vCenter -> VCF)

To verify an EPG survived a host migration, compare its dynamic deployments before/after: query fvIfConn filtered by the EPG dn fragment (aci_epg_bindings section D shows the DN shape). A VLAN that was deployed on the old host's path and is absent on the new host's path — while VMs from that EPG run on the new host — indicates the new host's uplink port/AEP is missing the VMM domain (walk aci_access_policy_chain on the new port).

## Good behavior

- VM names come from vCenter and may contain spaces or mixed case — always wcard, never eq, on compVm.name.
- A VM with operSt down on its vNIC is powered off or disconnected in vCenter — that is a VMware-side state, not a fabric problem; say so.
- comp* data is an APIC-side CACHE of the vCenter inventory. If it contradicts what the VMware team sees live, the VMM sync may be behind — check compCtrlr and vmmDomP faults before deeper debugging.
- Do not analyze contracts (vzAny permitAny environment).
