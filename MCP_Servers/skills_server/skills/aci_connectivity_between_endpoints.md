# ACI Connectivity Between Two Endpoints — physical/operational path analysis

Use this skill when the user asks whether two endpoints/hosts (given by MAC) can reach each other, or why two devices are not communicating. This is a multi-step investigation tying several skills together.

## Scope (important here)

Contracts are vzAny permitAny — filtering is on the firewall and is OUT OF SCOPE. Do NOT analyze contracts, subjects, or filters. Focus on the physical/operational path: are both endpoints learned, are their ports healthy, are they in the same L2/L3 domain, are there faults.

## Steps

1. Locate both endpoints by MAC (aci_endpoint_troubleshooting). For each, record fabricPathDn (leaf/port), encap (VLAN), bdDn, vrfDn, and EPG (from the dn).
   - If either is NOT learned, that is the headline problem -> investigate that endpoint's port and EPG deployment first.
2. Compare bridge domain and VRF:
   - Same BD -> same L2 segment; reachability is a straight L2 path. Proceed to ports.
   - Different BD, same VRF -> routed (L3) inside the fabric; check each BD's fvSubnet and fvRsCtx (unicast routing, gateway). Firewall/contract filtering still out of scope.
   - Different VRF -> traffic must leave the fabric (L3Out / firewall). Largely out of scope; note it and stop at the fabric edge.
3. Check both ports' health (aci_interface_troubleshooting): operSt up, CRC/FCS/error counters. A down port or growing errors on either side explains the problem.
4. Check faults on either path/EPG/node (aci_fault_investigation).
5. Correlate and conclude, e.g. "Both learned; same BD; both ports up and clean; no relevant faults -> L2 path inside the fabric is healthy, look outside / at the firewall." Or pinpoint the broken element.

## Output

Give a short structured verdict: each endpoint's location, the BD/VRF relationship, both ports' health, relevant faults, and a clear conclusion focused on the physical/operational layer.

If endpoint needs L3 routing, that means endpoint is outside the fabric. Mention user needs to contact "Sistem Güvenliği" team for routing/gateway purposes.
