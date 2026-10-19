# ACI Fault Investigation — read faults and find the affected object

Use this skill when the user asks about faults, alarms, or "what is wrong" on the fabric, a node, a port, a device, or an endpoint/MAC — including ACTIVE faults and fault HISTORY over a time window (e.g. the last 48 hours).

## Fabric / global health overview (cheap ladder)

Use this path when the question is fabric-wide or vague ("how is our fabric
doing?", "any critical faults?", "fabric sağlıklı mı?") and there is **no**
specific host/MAC/port yet. Goal: **few essential calls**, never a history dump.

1. **Active faults sample first** — one (or two) calls only:
   ```
   /api/node/class/faultInst.json?order-by=faultInst.severity|desc&page-size=25&page=0
   ```
   and/or severity-filtered counts/samples:
   ```
   /api/node/class/faultInst.json?query-target-filter=eq(faultInst.severity,"critical")&page-size=25&page=0
   /api/node/class/faultInst.json?query-target-filter=eq(faultInst.severity,"major")&page-size=25&page=0
   ```
   Read `totalCount` + top rows. That is enough for an overview answer.
2. **Optional aggregate** — if useful in this fabric, one call to
   `faultSummary` for rolled-up counts (not a substitute for reading severities
   on `faultInst` when summary is empty).
3. **Do NOT** open fabric-wide unpaged `faultRecord` for 48h (or any window).
   History is only for scoped follow-up (node/port/EPG) or when the user
   explicitly asks for history — and then only with `page-size` ≤ 50,
   `order-by=faultRecord.created|desc`, **`page=0` only**. Never walk pages
   1…N to reconstruct tens of thousands of records.
4. Answer with: total active (and critical/major if queried), notable codes /
   affected objects from the sample, and offer a scoped deep-dive if needed.

If the user later names a host/port/MAC, switch to the scoped path below.

## Find the affected object first (when the question is scoped to a port, device, or MAC)

Faults are filtered by the DN of the AFFECTED object, so first turn the user's target into a DN fragment.

**CRITICAL: Do NOT filter faultInst or faultRecord by a MAC address, CEp DN, or
endpoint DN directly. Faults are NEVER raised on fvCEp objects. The correct
affected-object fragment is always a NODE + PORT (or bundle). Querying with a
CEp fragment will always return 0 results.**

```
WRONG: wcard(faultRecord.affected,"cep-00:50:56:B9:EE")   <- always empty
WRONG: wcard(faultInst.dn,"00:50:56:B9:EE")               <- always empty
RIGHT (after resolving to leaf 101, eth1/5):
  and(wcard(faultRecord.affected,"node-101"),wcard(faultRecord.affected,"eth1/5"))
```

Always resolve the user's input to a leaf + port FIRST, then query faults.

### Resolve the input to node + port

Turn a MAC, a port serial, or a device/interface name into leaf node + physical
port(s) using **aci_resolve_identifier** (Path A for a MAC, Path B for a
serial/name). That skill is the single source of truth for the resolution
chains — follow it, then come back here and query faults with the node + port
fragment.

Once resolved, the fault filter fragment is the node + port, e.g.:
```
/api/node/class/faultInst.json
    ?query-target-filter=and(wcard(faultInst.dn,"node-<N>"),wcard(faultInst.dn,"eth1/47"))
    &order-by=faultInst.severity|desc&page-size=25&page=0
```
And for history (always small page-size; page=0 only unless user asks for more):
```
/api/node/class/faultRecord.json
    ?query-target-filter=and(gt(faultRecord.created,"<window>"),
      wcard(faultRecord.affected,"node-<N>"),wcard(faultRecord.affected,"eth1/47"))
    &order-by=faultRecord.created|desc&page-size=25&page=0
```
If the profile resolved to multiple nodes (VCF multi-node access), repeat for
each. If it is a PC/vPC, also check bundle-level faults:
`wcard(faultInst.dn,"<BUNDLE_NAME>")`.

---

### Resolve by explicit leaf + port (e.g. "leaf101 eth1/1")

The fragment is `node-<N>/sys/phys-[eth<card>/<port>]`, or combine with `and(...)`:
```
and(wcard(faultInst.dn,"node-101"),wcard(faultInst.dn,"eth1/1"))
```

### MAC-triggered full investigation (physical ports + port-channel + CRC)

When the user asks about problems or faults for a specific MAC (e.g. "what is the problems of aa:aa:aa:bb:bb:bb"), do NOT stop at querying faults on the endpoint alone. A MAC-based question requires a full chain covering the endpoint itself, every physical port, every port-channel bundle, AND CRC/error counters. Follow all steps below:

**Step 1 — Locate the endpoint and collect ALL ports**

Resolve the MAC to node + physical port(s) using **aci_resolve_identifier**
(Path A). That gives you the endpoint fields (`mac`, `fabricPathDn`, `encap`,
`bdDn`, `dn` → tenant/AP/EPG) and — after expanding any port-channel/vPC bundle
to its member ports — the complete list of physical ports to check.

Collect ALL physical ports: both direct-attached ports and every member port of
any port-channel/vPC. You will check each one. If the MAC is not learned and not
an adjacency, report that and direct the user to aci_endpoint_troubleshooting.

**Step 2 — Query faultInst (active faults) for every object**

For EACH physical port (small page-size; do not dump unbounded results):
```
/api/node/class/faultInst.json?query-target-filter=and(wcard(faultInst.dn,"node-<N>"),wcard(faultInst.dn,"eth<X/Y>"))&order-by=faultInst.severity|desc&page-size=25&page=0
```

For a port-channel bundle (if applicable):
```
/api/node/class/faultInst.json?query-target-filter=wcard(faultInst.dn,"<BUNDLE_NAME>")&order-by=faultInst.severity|desc&page-size=25&page=0
```

For the endpoint's EPG:
```
/api/node/class/faultInst.json?query-target-filter=wcard(faultInst.dn,"<EPG_DN_fragment>")&order-by=faultInst.severity|desc&page-size=25&page=0
```

**Step 3 — Query faultRecord (fault history) for every object**

Use a 48-hour window by default (adjust if user specifies). Compute `window-start` = current UTC time minus 48 hours, formatted like `2026-06-29T00:00:00`.
Every history call MUST include `page-size` ≤ 50, `order-by=...|desc`, and
`page=0`. Use `totalCount` + the sample. Do **not** fetch further pages.

For EACH physical port:
```
/api/node/class/faultRecord.json?query-target-filter=and(gt(faultRecord.created,"<window-start>"),wcard(faultRecord.affected,"node-<N>"),wcard(faultRecord.affected,"eth<X/Y>"))&order-by=faultRecord.created|desc&page-size=25&page=0
```

For a port-channel bundle:
```
/api/node/class/faultRecord.json?query-target-filter=and(gt(faultRecord.created,"<window-start>"),wcard(faultRecord.affected,"<BUNDLE_NAME>"))&order-by=faultRecord.created|desc&page-size=25&page=0
```

For the endpoint's EPG:
```
/api/node/class/faultRecord.json?query-target-filter=and(gt(faultRecord.created,"<window-start>"),wcard(faultRecord.affected,"<EPG_DN_fragment>"))&order-by=faultRecord.created|desc&page-size=25&page=0
```

Note: For faultRecord, filter on the `affected` attribute (not `dn`).

**Step 4 — Read CRC/FCS/error counters on every physical port**

For EACH physical port (including port-channel member ports), query RMON counters:
```
/api/node/mo/topology/pod-1/node-<N>/sys/phys-[eth<X/Y>].json?query-target=children&target-subtree-class=rmonEtherStats,rmonDot3Stats,rmonIfIn,rmonIfOut
```

Key counters:
- `rmonEtherStats.cRCAlignErrors` → CRC errors
- `rmonDot3Stats.fCSErrors` → FCS errors
- `rmonIfIn.errors` → input errors
- `rmonIfIn.discards` → input discards/drops
- `rmonIfOut.errors` → output errors
- `rmonIfOut.discards` → output discards/drops

CRC interpretation: Zero = clean. Non-zero and growing = physical-layer problem (bad cable, dirty/failing transceiver/SFP, or far-end NIC). There is NO fixed threshold — report actual values. Do not invent a threshold.

Also check port operational state for each physical port (see aci_interface_troubleshooting):
```
/api/node/mo/topology/pod-1/node-<N>/sys/phys-[eth<X/Y>].json?rsp-subtree=full
```
- `l1PhysIf.adminSt` = configured admin state
- `ethpmPhysIf.operSt` = actual operational state (up/down)
- `ethpmPhysIf.operStQual` = reason if down (e.g. sfp-missing, link-failure, speed-mismatch)
- `ethpmPhysIf.operSpeed` = negotiated speed (should not be less than 10G)

**Step 5 — Correlate and report**

Structure the report as follows:

1. **Endpoint Summary**: MAC, VLAN, BD, EPG, where learned (leaf/port or vPC bundle), whether learned on one path or multiple (flag flapping if multiple).
2. **Active Faults (faultInst)**: For each port and the endpoint, list all raised faults — severity, code, cause, description, affected object, lifecycle state.
3. **Fault History (faultRecord)**: For each port and the endpoint, list recent fault events — when raised/cleared, severity, code, description, whether it recovered.
4. **CRC/FCS/Error Counters**: For each physical port, report operational state (up/down, speed), CRC errors, FCS errors, input/output errors and discards. Flag any non-zero CRC/FCS values as likely physical-layer problems.
5. **Conclusion**: If any port is down → primary problem. If CRC/FCS is non-zero and growing → physical-layer issue (cable, SFP, far-end NIC). If faults exist on the port or bundle → report with severity. If everything is clean → state that no faults or errors were found on the endpoint's path. If the endpoint is not learned → direct user to aci_endpoint_troubleshooting.

IMPORTANT:
- ALWAYS check BOTH physical ports AND port-channel member ports. A vPC bundle may be up while individual member ports have errors.
- ALWAYS check both faultInst AND faultRecord — the user needs current state and recent history.
- ALWAYS check CRC counters on physical ports even if no faults are raised — CRC errors may exist without a corresponding fault.
- Do NOT analyze contracts or filters — out of scope (vzAny permitAny).

## Query faults

ACTIVE faults (faultInst — current state):
- All, most severe first: /api/node/class/faultInst.json?order-by=faultInst.severity|desc
- By severity: /api/node/class/faultInst.json?query-target-filter=eq(faultInst.severity,"critical") (values: critical, major, minor, warning)
- On a specific object: filter on the dn, e.g. wcard(faultInst.dn,"<object dn fragment>")
- Port-scoped (node + port): /api/node/class/faultInst.json?query-target-filter=and(wcard(faultInst.dn,"node-101"),wcard(faultInst.dn,"eth1/1"))

Fault HISTORY / records over a time window (faultRecord — append-only log; use this for "last N hours / 48 hours"):
- A faultRecord stores the affected object in the `affected` attribute and the event time in `created`.
- Compute the window start from the CURRENT UTC TIME given in your context, minus N hours, formatted like 2026-06-23T00:00:00 (ACI also accepts the timezone-qualified form 2026-06-23T00:00:00.000+00:00).
- Last N hours on a specific object, newest first: /api/node/class/faultRecord.json?query-target-filter=and(gt(faultRecord.created,"<window-start>"),wcard(faultRecord.affected,"<object dn fragment>"))&order-by=faultRecord.created|desc&page-size=25&page=0
- Last N hours fabric-wide is expensive and often returns tens of thousands of rows. Prefer the cheap overview ladder (active faultInst / faultSummary) first. If the user still wants fabric-wide history, allow **only** with a small sample: `gt(faultRecord.created,"<window-start>")` + optional severity filter + `order-by=faultRecord.created|desc` + `page-size=25` + `page=0`. Report `totalCount` + patterns from the sample. **Forbidden:** multi-page crawls (`page=1,2,…`) to dump the full set.
- For a port, combine the node and port fragments: and(gt(faultRecord.created,"<window-start>"),wcard(faultRecord.affected,"node-101"),wcard(faultRecord.affected,"eth1/1")) — still with page-size and page=0.

For scoped host/port investigations, report BOTH: the still-raised active faults (faultInst) AND what was raised/cleared in the window (faultRecord sample), so the user sees the current state and recent flapping/history. For fabric-wide overview, active sample + counts is enough unless history was requested.

## Read each fault

- severity : critical / major / minor / warning
- code : fault code (F####), identifies the problem class
- cause : machine cause string
- descr : human-readable description (most useful)
- dn : the AFFECTED object — parse it to know what is faulted (node, port, tenant, EPG, etc.)
- lc (lifecycle) : raised / soaking / clearing / retaining. "retaining"/"clearing" means it already recovered.
- created / lastTransition : recency. Prioritize recent, raised, high-severity faults.

For faultRecord (history): same severity / code / cause / descr, PLUS `affected` (the affected object dn — filter on THIS, not dn) and `created` (event time); `ind` / `changeSet` shows whether the event was a creation, modification, or clearing ("clearing" / deletion means the fault recovered).

faultDelegate = faults delegated upward; faultSummary = aggregated counts for a quick overview.

## Interpret

Lead with the most recent, highest-severity, still-raised faults. For each, state the affected object (from the dn) and what descr indicates. Tie port faults to aci_interface_troubleshooting and endpoint faults to aci_endpoint_troubleshooting.

## Fault codes seen in this environment

- F3074: SSD Lifetime hit 80% which means a RMA is needed.
- F3073: SSD Lifetime hit 90% which means an urgent RMA is needed.


