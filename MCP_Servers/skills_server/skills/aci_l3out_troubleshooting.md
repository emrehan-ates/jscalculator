# ACI L3Out Troubleshooting — external routing: interfaces, BGP/OSPF adjacencies, routes

Use this skill when the question is about an L3Out (external routed connection):
is the L3Out up, are its routing adjacencies (BGP/OSPF/EIGRP) established, are
routes being learned/advertised, or why a subnet outside the fabric is
unreachable through a specific L3Out. This covers the FABRIC-SIDE routing plane
only — it is not about contracts (out of scope here) and not about firewall
policy.

## Scope reminder

Contracts / filtering are on the firewall and OUT OF SCOPE (vzAny permitAny).
This skill covers the L3Out's own objects: logical nodes/interfaces, routing
protocol adjacencies, and the route tables. If the fabric routing plane is
healthy, the conclusion is "look outside the fabric (firewall / upstream
router / far end)", never "everything is fine".

## L3Out object model (config)

- `l3extOut` — the L3Out itself. DN: `uni/tn-T/out-NAME`. `l3extRsEctx` child =
  the VRF it lives in.
- `l3extLNodeP` — logical node profile (which leaf switches are border leaves).
  `l3extRsNodeL3OutAtt` = node assignment + `rtrId` (router ID).
- `l3extLIfP` — logical interface profile. `l3extRsPathL3OutAtt` = the physical
  interface / SVI / sub-interface path and encap.
- `l3extInstP` — the external EPG (subnets classified into it via
  `l3extSubnet`). DN under the L3Out.

Read the whole L3Out config subtree at once:
```
/api/node/mo/uni/tn-T/out-NAME.json?rsp-subtree=full
```
Confirm: a VRF is bound (`l3extRsEctx`), at least one border node with a router
ID, and at least one interface path with the expected encap/IP.

## Routing adjacencies (operational — the usual root cause)

### BGP
- Config: `bgpPeerP` (the configured neighbor, `addr`, `peerCtrl`).
- Operational: `bgpPeerEntry` — the live neighbor. Key: `operSt` should be
  `established`. Also `remoteIp`, `remoteAsn`, `connAttempts`.
```
/api/node/class/bgpPeerEntry.json?query-target-filter=eq(bgpPeerEntry.operSt,"established")
```
Scope to a border node when you know it:
`wcard(bgpPeerEntry.dn,"node-101")`.
- Not `established` (idle / connect / active) → session down: check the
  interface path (below), the far-end config, and faults on the L3Out.

### OSPF
- `ospfAdjEp` — the adjacency. Key: `operSt` should be `full` (or `2way` on a
  broadcast segment for DR/BDR pairs). `nbrId` = neighbor router ID.
```
/api/node/class/ospfAdjEp.json?query-target-filter=wcard(ospfAdjEp.dn,"node-101")
```
- Stuck in `init` / `exstart` / `exchange` → MTU mismatch, area mismatch, or
  interface down. `ospfIf` shows the interface-level OSPF config/state.

### EIGRP
- `eigrpAdjEp` — adjacency; `operSt` up when the neighbor is formed.

## Routes learned / advertised

- Operational route table: `uribv4Route` (IPv4) / `uribv6Route`, with
  `uribv4Nexthop` children for next-hops. Scope by border node:
```
/api/node/class/uribv4Route.json?query-target-filter=and(wcard(uribv4Route.dn,"node-101"),wcard(uribv4Route.prefix,"10.20.0.0"))
```
- A prefix present with a valid nexthop and `operSt`/`pref` set → being used.
- Missing prefix while the adjacency is up → the far end is not advertising it,
  or a route-map/route-control is filtering it.

## Faults on the L3Out

Always check faults scoped to the L3Out and its border nodes — they often name
the exact problem (interface down, peer unreachable, MTU mismatch):
```
/api/node/class/faultInst.json?query-target-filter=wcard(faultInst.dn,"out-NAME")
```
And per border node + interface, use the interface path fragment (see
aci_interface_troubleshooting for the physical port state).

## Investigation order

1. Read the L3Out config subtree — confirm VRF, border node(s) + router ID,
   interface path(s) + encap/IP.
2. Check the physical interface(s) the L3Out uses (aci_interface_troubleshooting):
   a down border-leaf uplink explains a dead adjacency.
3. Check the routing adjacency (BGP `bgpPeerEntry.operSt=established`, OSPF
   `ospfAdjEp.operSt=full`). This is the most common failure point.
4. If the adjacency is up but a destination is unreachable, check the route
   table (`uribv4Route`) for the prefix on the border node.
5. Check faults on the L3Out and border nodes throughout.

## Interpret / report

- Adjacency down → name the protocol, neighbor, and state; point to the likely
  cause (interface down, config/area/AS mismatch, MTU).
- Adjacency up but prefix missing → the fabric side is healthy; the far end is
  not advertising or a route filter drops it — direct the user to the upstream
  router / firewall / "Sistem Güvenliği" team.
- Everything up and routes present → fabric routing plane is CLEAR; the problem
  is beyond the fabric edge.

Report actual attribute values (states, neighbor IDs, prefixes) — never invent a
route or a neighbor state. Answer in the language the user used.
