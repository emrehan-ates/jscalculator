# ACI Troubleshooting Overview — choose the right skill

Use this skill when the user asks a broad troubleshooting question, an inventory/count question, or when you need to route to the correct ACI troubleshooting playbook.

## Routing rules

- To turn a MAC, serial, or device/interface name into a leaf node + physical port(s) before any other check, use [aci_resolve_identifier.md](aci_resolve_identifier.md). Most port/fault/binding tasks start here.
- For "how many", "how much", "list all", or count-style questions, use [aci_inventory_validation.md](aci_inventory_validation.md).
- For questions about ONE named object's properties or what is attached to it (e.g. "properties of X EPG", "static bindings of X EPG", "ports in X interface profile"), use [aci_object_inspection.md](aci_object_inspection.md).
- For a single endpoint or MAC lookup, use [aci_endpoint_troubleshooting.md](aci_endpoint_troubleshooting.md).
- For locating a SERVER or HOST by NAME/hostname (not a MAC), use [aci_server_locator.md](aci_server_locator.md).
- If the user names a SWITCH by hostname (e.g. "DR1-C6-8") instead of a numeric node ID, first resolve it with [aci_switch_name_resolution.md](aci_switch_name_resolution.md), then continue with the relevant skill.
- For reachability between two MACs, use [aci_connectivity_between_endpoints.md](aci_connectivity_between_endpoints.md).
- For a port up/down, speed, or CRC/FCS problem, use [aci_interface_troubleshooting.md](aci_interface_troubleshooting.md).
- For a vPC/port-channel peer state, member consistency, or bundle-leg problem, use [aci_vpc_health.md](aci_vpc_health.md).
- For an EPG not working on a port, use [aci_epg_deployment.md](aci_epg_deployment.md).
- For a VLAN or port that is not deploying, use [aci_access_policy_chain.md](aci_access_policy_chain.md).
- For an L3Out / external routing problem (BGP/OSPF adjacency, learned routes, external reachability), use [aci_l3out_troubleshooting.md](aci_l3out_troubleshooting.md).
- For APIC controller cluster health, quorum, or "is the APIC itself the problem", use [aci_apic_cluster_health.md](aci_apic_cluster_health.md).
- For firmware versions, version consistency, or in-progress upgrades/maintenance, use [aci_firmware_maintenance.md](aci_firmware_maintenance.md).
- For faults, alarms, or "what is wrong", use [aci_fault_investigation.md](aci_fault_investigation.md). This also covers faults scoped to a specific port, device name/serial, endpoint MAC, or LLDP neighbor, and fault history / records over a time window (e.g. last 48 hours).
- For "what changed", "who changed", audit questions, or correlating a problem with a recent configuration change, use [aci_change_audit.md](aci_change_audit.md).
- For virtual machines, ESXi hosts, VMM domains, or "where is VM X", use [aci_vmm_visibility.md](aci_vmm_visibility.md).
- When another team (server / VMware / database) asks "is the network the problem?" or a formal evidence report is needed for a ticket, use [aci_network_health_verdict.md](aci_network_health_verdict.md).
- For filtering object trees with rsp-subtree, use [aci_subtree_analysis.md](aci_subtree_analysis.md).
- For HOW to write an APIC query (filter operators, query-target vs rsp-subtree, paging, escaping), use [aci_query_syntax.md](aci_query_syntax.md).

## Fabric policy note

Contracts are out of scope in this environment. The fabric uses vzAny permitAny and filtering happens on the firewall, so do not analyze contracts, subjects, or filters for troubleshooting or counting questions.

## Good behavior

- Pick the most specific skill first.
- Do not mix count questions with troubleshooting guesses.
- If the scope is unclear, resolve the object type first with aci_object_map and then apply the relevant skill.
- Users may ask in Turkish or English. Route the same way regardless of language, and answer in the language the user used.
