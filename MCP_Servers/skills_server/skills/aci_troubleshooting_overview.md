# ACI Troubleshooting Overview — choose the right skill

Router for broad or unclear requests: maps the question (summary, relations, bindings, location, faults, ports, counts) to the one skill that answers it.

## Routing rules

Names, relations and configuration (most common):

- Server serial, MSY_/CSY_/SG_ name, switch hostname or EPG/VLAN name and you are unsure what it is → [aci_naming_conventions.md](aci_naming_conventions.md).
- "What is X / X'i özetle / properties of X" for any tenant object (tenant, VRF, BD, AP, EPG) or access/fabric policy object (policy group, interface policy, AEP, phys/VMM domain, VLAN pool, interface/switch profile) → [aci_object_summary.md](aci_object_summary.md).
- Relations between port ↔ interface profile ↔ policy group ↔ AEP ↔ domain (phys/VMM) ↔ VLAN pool, in either direction ("policy group of this port", "ports of this PG", "AEPs of this VMM domain"), or a VLAN not deploying → [aci_access_policy_chain.md](aci_access_policy_chain.md).
- EPG static bindings, EPGs/VLANs on a port/server/policy group, EPGs of a VLAN, VMM dynamic deployment, "is EPG X on port Y" → [aci_epg_bindings.md](aci_epg_bindings.md).
- Where is a server/host (MAC, serial, interface-profile name, hostname) → [aci_resolve_identifier.md](aci_resolve_identifier.md).
- Switch named by hostname (e.g. "DR1-C6-8") → [aci_switch_name_resolution.md](aci_switch_name_resolution.md) first.
- Which class / DN for an object type → [aci_object_map.md](aci_object_map.md); how to write the query → [aci_query_syntax.md](aci_query_syntax.md).
- "How many / list all" → [aci_inventory_validation.md](aci_inventory_validation.md).

Operations and troubleshooting:

- Single endpoint / MAC learning → [aci_endpoint_troubleshooting.md](aci_endpoint_troubleshooting.md).
- Reachability between two MACs → [aci_connectivity_between_endpoints.md](aci_connectivity_between_endpoints.md).
- Port up/down, speed, CRC/FCS → [aci_interface_troubleshooting.md](aci_interface_troubleshooting.md).
- vPC / port-channel state → [aci_vpc_health.md](aci_vpc_health.md).
- L3Out / external routing → [aci_l3out_troubleshooting.md](aci_l3out_troubleshooting.md).
- APIC cluster → [aci_apic_cluster_health.md](aci_apic_cluster_health.md); firmware/upgrades → [aci_firmware_maintenance.md](aci_firmware_maintenance.md).
- Faults / "what is wrong" / fault history → [aci_fault_investigation.md](aci_fault_investigation.md).
- "What changed / who changed" → [aci_change_audit.md](aci_change_audit.md).
- VMs, ESXi hosts, vCenter connection → [aci_vmm_visibility.md](aci_vmm_visibility.md).
- "Is the network the problem for server X" ticket verdict → [aci_network_health_verdict.md](aci_network_health_verdict.md).
- Nothing fits → [generic_investigation.md](generic_investigation.md).

## Answering rules (all skills)

- The final answer interprets EVERY result gathered in the turn, including
  partial chains. An empty query means "nothing matched that exact filter" —
  say which one, and keep the findings from the other queries.
- Structure: findings (with the values APIC returned) → what was empty or not
  checked → optional next step.
- Only when every query was empty or failed: say clearly that you could not
  find it ("bulamadım / bilmiyorum"), list what you searched, and suggest how
  to narrow it. Never fill gaps with guesses or naming assumptions.

## Fabric policy note

Contracts are out of scope (vzAny permitAny; filtering on the firewall). Do not
analyze contracts, subjects, or filters. Users ask in Turkish or English —
route the same way and answer in the user's language.
