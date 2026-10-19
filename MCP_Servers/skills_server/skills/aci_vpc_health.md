# ACI vPC / Port-Channel Health — peer state, member consistency, bundle status

Use this skill when the question is about a vPC (virtual port-channel) or a
regular port-channel (PC): is the vPC peer link up, is the vPC domain healthy,
are both member legs of a bundle forwarding, or is there a vPC consistency
(compatibility) problem. A vPC bundle can look "up" while one leg or the peer
relationship is broken — this skill checks the bundle as a whole, not just a
single port.

## When to use vs interface troubleshooting

- ONE physical port up/down/CRC → aci_interface_troubleshooting.
- The BUNDLE (PC/vPC), its peer relationship, or member consistency across the
  two leaves → this skill.
- To turn a serial/name into the leaves + member ports first →
  aci_resolve_identifier.

## vPC domain and peer state

The vPC domain lives per node: `topology/pod-1/node-<N>/sys/vpc/inst/dom`.

- `vpcDom` — the vPC domain on each peer. Key attributes:
  - `operRole` — `primary` / `secondary` (each peer takes one).
  - `peerSt` — peer adjacency; should be `up`.
  - `peerLinkPortSt` / `peerOperSt` — peer-link state.
  - `configuredPeerIp` / `operPeerIp` — the peer's address.
```
/api/node/class/vpcDom.json
```
Read both peers (node-N1 and node-N2). `peerSt` not `up` → the vPC peer link or
peer-keepalive is broken; the domain is split. This is the headline problem —
report it before anything else.

- Protection group config (which two leaves form the vPC pair):
  `fabricExplicitGEp` (the group) with `fabricNodePEp` children (the node IDs),
  and `fabricRsVpcInstPol` for the vPC policy. Use this to confirm the intended
  pair if the operational `vpcDom` is missing on one side.

## vPC member interfaces (consistency)

- `vpcIf` — a vPC interface (one logical vPC member). Key attributes:
  - `localOperSt` / `remoteOperSt` — each leg's operational state; BOTH should
    be `up` for the vPC to forward on both legs.
  - `compatSt` — compatibility/consistency status; should indicate compatible.
    A `compatSt` failure = a Type-1/Type-2 consistency mismatch (e.g. different
    VLANs, MTU, or STP settings on the two legs) and the vPC will suspend.
  - `id` / `dn` — which bundle it maps to.
```
/api/node/class/vpcIf.json?query-target-filter=wcard(vpcIf.dn,"node-101")
```
- `localOperSt=up` but `remoteOperSt=down` → only one leg forwarding; the far
  leaf's member port is down (check it with aci_interface_troubleshooting).
- `compatSt` not compatible → a consistency mismatch is suspending the vPC;
  name the mismatch and check recent changes (aci_change_audit) on both legs.

## Port-channel aggregate (member ports)

- `pcAggrIf` — the aggregated PC/vPC interface on a node. `operSt` = bundle
  operational state; `sumPortCap` / member count help confirm how many legs are
  bundled.
- Member physical ports: read the bundle's members and check each with
  aci_interface_troubleshooting (operSt, speed, CRC/FCS). A vPC/PC can be `up`
  with one member port erroring or flapping.
- LACP: `lacpIf` / `lacpAdjEp` show LACP negotiation per member — a member stuck
  out of the bundle is often an LACP mismatch (mode/rate) with the far end.

## Faults

Check faults scoped to the bundle name and to each leaf's vpc instance:
```
/api/node/class/faultInst.json?query-target-filter=or(wcard(faultInst.dn,"<BUNDLE_NAME>"),wcard(faultInst.dn,"vpc"))
```
vPC-specific faults call out peer-link down, keepalive failure, and consistency
(compatibility) parameter mismatches directly.

## Investigation order

1. Resolve the target to the two leaves + member ports (aci_resolve_identifier).
2. Read `vpcDom` on both peers — is `peerSt` up and are roles primary/secondary?
   A broken peer relationship is the headline.
3. Read `vpcIf` — are `localOperSt` and `remoteOperSt` both up, and is
   `compatSt` compatible? A consistency mismatch suspends the vPC.
4. Check each member physical port (aci_interface_troubleshooting) for down /
   speed / CRC / LACP problems.
5. Check faults on the bundle and vpc instance throughout.
6. If a config mismatch is suspected, check recent changes on BOTH legs
   (aci_change_audit) — a one-sided change is the classic cause.

## Interpret / report

- Peer link / keepalive down → vPC domain split; report first.
- One leg down → half-bandwidth, redundancy lost; name the down leaf+port.
- `compatSt` mismatch → vPC suspended by consistency check; name the mismatched
  parameter and the likely one-sided change.
- All up, both legs forwarding, compatible, no faults → bundle is HEALTHY; look
  elsewhere.

Report actual state values; never assume a leg is up without reading it. Answer
in the language the user used. Contracts remain out of scope.
