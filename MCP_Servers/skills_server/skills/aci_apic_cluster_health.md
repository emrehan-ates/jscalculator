# ACI APIC Cluster Health — controller cluster state, quorum, shard replicas

Use this skill when the question is about the APIC controllers themselves (not
the switches): is the APIC cluster healthy / fully-fit, are all controllers in
service, is there a quorum problem, or are data shards under-replicated. Most
other skills look at the fabric switches — this one checks the control plane
that runs them.

## Cluster state objects

- `infraWiNode` — each controller's view of every cluster member. This is the
  primary cluster-health object. Key attributes:
  - `health` — a member is healthy when this is `fully-fit`.
  - `operSt` — `available` / `unavailable`.
  - `id` — the APIC controller ID (1, 2, 3, ...).
  - `nodeName` / `mbSn` — controller name / serial.
```
/api/node/class/infraWiNode.json
```
Every member should show `health=fully-fit` and `operSt=available` from the
perspective of every controller. Any member not fully-fit → that APIC is out of
the cluster (down, rebooting, split, or being commissioned).

- `infraClusterPol` — the ADMIN target cluster size (`size`). Compare the target
  size against the number of `fully-fit` members: fewer fit members than the
  target size means the cluster is degraded / at risk of losing quorum.
```
/api/node/class/infraClusterPol.json
```

- `topSystem` (role=controller) — per-APIC system info: `state`
  (`in-service`), `address` (out-of-band / infra IP), `serial`, `version`,
  `systemUpTime`. Confirms each controller is actually in service and on the
  expected version.
```
/api/node/class/topSystem.json?query-target-filter=eq(topSystem.role,"controller")
```

## Quorum / minority

APIC is a 3+ node cluster and needs a MAJORITY to accept configuration changes.
- 3-node cluster: 2 fit = degraded but writable; 1 fit = MINORITY, read-only,
  configuration is blocked.
- If `infraWiNode` shows only a minority fully-fit, tell the user the cluster is
  in a minority state and configuration changes will be rejected until quorum is
  restored — this is a control-plane emergency, not a data-plane one.

## Shard replicas (data distribution)

APIC state is sharded and replicated across controllers. Under-replicated or
diverged shards indicate a cluster problem even if nodes look "up":
- `infraReplicaCont` / `infraReplica` — replica containers/entries per shard.
- Look for replicas that are not in a healthy/normal replication state; a shard
  with fewer healthy replicas than expected points to a controller that left or
  a sync still in progress.

If shard classes are unavailable in your environment, rely on `infraWiNode`
health + `infraClusterPol` size as the primary signal.

## Faults on the controllers

```
/api/node/class/faultInst.json?query-target-filter=wcard(faultInst.dn,"node-1/")
```
(controllers are usually node-1/2/3). Cluster faults call out data-layer
divergence, service (appliance) problems, and commission/decommission events.

## Investigation order

1. `infraWiNode` — is every member `fully-fit` and `available`?
2. `infraClusterPol.size` — how many controllers SHOULD there be? Compare with
   the number of fit members to judge quorum.
3. `topSystem` (role=controller) — each APIC `in-service`, on the expected
   `version`, reachable `address`.
4. If any member is unhealthy, check controller faults and (if available) shard
   replica state.

## Interpret / report

- All members `fully-fit`, count == target size, all `in-service` on the same
  version → cluster is HEALTHY.
- A member not fully-fit → name the controller (id/serial) and its state; the
  cluster is degraded. If only a minority is fit → CONFIG IS BLOCKED (read-only)
  until quorum returns — flag this loudly.
- Version mismatch across controllers → a controller upgrade is in progress or
  stalled (cross-check aci_firmware_maintenance).

Report actual health/state/version values; never assume the cluster is fit
without reading `infraWiNode`. Answer in the language the user used.
