# ACI Endpoint Troubleshooting — locate a host by MAC and explain where/whether it is learned

Use this skill when the user asks about an endpoint, host, server, or MAC address: where it is connected, whether the fabric has learned it, or whether it is moving/flapping.

## Key facts for this environment

- Identify endpoints by MAC address ONLY. Do not search endpoints by IP.
- Endpoint connectivity problems here are physical/operational, not policy. Contracts are vzAny permitAny (filtering is on the firewall, OUT OF SCOPE). Do not analyze contracts for reachability.

## Find the endpoint

/api/node/class/fvCEp.json?query-target-filter=eq(fvCEp.mac,"AA:BB:CC:DD:EE:FF")

Key fields:
- mac : the endpoint MAC
- encap : VLAN it was learned on (e.g. vlan-354)
- fabricPathDn : WHERE it is learned (leaf and port, or vPC). The most important field.
- bdDn : the bridge domain
- dn : contains tenant / app profile / EPG (parse uni/tn-T/ap-AP/epg-EPG/...)

## Interpret

- Found with a fabricPathDn -> learned. Report leaf/port, VLAN, BD, EPG.
- Not found (0 results) -> the fabric is not learning this MAC. Likely causes, in order:
  1. Port down or host not sending traffic -> aci_interface_troubleshooting.
  2. EPG not deployed on the port, or wrong VLAN/encap -> aci_epg_deployment.
  3. Access policy chain incomplete so the VLAN never deployed -> aci_access_policy_chain.
  Tell the user it is not learned and which of these to check. Do not just say "not found".
- Found in MORE THAN ONE fabricPathDn, or endpoint-move faults exist -> possible flapping. Flag it, check faults (aci_fault_investigation) and interface errors on both paths.

## Find the local port from a NEIGHBOR MAC (LLDP/CDP)

If the MAC is not a learned host but the chassis/port MAC of a directly attached device, it will not appear in fvCEp. Find the local leaf/port from the adjacency instead:

- LLDP: /api/node/class/lldpAdjEp.json?query-target-filter=wcard(lldpAdjEp.chassisIdV,"<mac>")  (also try portIdV)
- CDP fallback: /api/node/class/cdpAdjEp.json?query-target-filter=wcard(cdpAdjEp.chassisIdV,"<mac>")

The adjacency dn is topology/pod-1/node-<N>/sys/lldp/inst/if-[eth1/x]/adj-... -> the local port is node-<N> + eth1/x. sysName / portIdV / mgmtIp describe the neighbor. Use this node/port to look for faults (aci_fault_investigation) or interface errors (aci_interface_troubleshooting).

When given a MAC, try fvCEp first; if 0 results, try lldpAdjEp, then cdpAdjEp.

## Going deeper

For where-learned-now detail on a specific leaf, the endpoint manager database holds epmMacEp entries. Use only if fvCEp is ambiguous; for most questions fvCEp is enough.


