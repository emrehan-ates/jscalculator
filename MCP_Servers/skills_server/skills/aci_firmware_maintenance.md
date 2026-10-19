# ACI Firmware & Maintenance — running versions and in-progress upgrades

Use this skill when the question is about software versions or upgrades: what
firmware a switch or APIC is running, whether versions are consistent across the
fabric, or whether an upgrade / maintenance is in progress (which can explain
expected, transient link flaps and faults). This is read-only version/status
reporting — it never triggers or schedules an upgrade.

## Running versions

- `firmwareRunning` — the running firmware on each FABRIC NODE (leaf/spine).
  Key attributes: `version`, `type` (switch), `dn` (contains the node).
```
/api/node/class/firmwareRunning.json
```
- `firmwareCtrlrRunning` — the running firmware on each APIC CONTROLLER.
  Key attributes: `version`, `dn` (contains the controller node).
```
/api/node/class/firmwareCtrlrRunning.json
```

To check version CONSISTENCY, list all `firmwareRunning` versions and flag any
node whose `version` differs from the rest. A mixed-version fabric is normal
DURING an upgrade but a problem if no upgrade is running (it can cause feature
inconsistencies).

Cross-check controller versions with aci_apic_cluster_health (a controller
version mismatch there means a controller upgrade is mid-flight or stalled).

## Upgrade / maintenance status

- `maintUpgJob` — the per-node upgrade job. Key attributes:
  - `upgradeStatus` — e.g. `notScheduled`, `scheduled`, `inProgress`,
    `completeOk`, `completeNok`.
  - `desiredVersion` — the target version for this node.
  - `instlProgPct` — install progress percentage.
  - `dn` — which node.
```
/api/node/class/maintUpgJob.json?query-target-filter=ne(maintUpgJob.upgradeStatus,"notScheduled")
```
  Any node in `inProgress` → an upgrade is actively running; expect that node to
  reload and its links to flap. `completeNok` → an upgrade FAILED on that node —
  a real problem to report.

- `maintMaintP` — maintenance policy (the upgrade group / scheduler config):
  which maintenance group and schedule govern the upgrade. Use it to explain
  WHICH nodes are grouped for a staged upgrade.

- `firmwareFwStatusCont` / `firmwareCtrlrFwStatusCont` — rollup status
  containers; useful for a one-shot "is anything upgrading" view if present.

## Correlating with faults / flaps

When a user reports link flaps, node reloads, or transient faults, check whether
an upgrade explains them BEFORE calling it a hardware fault:
1. `maintUpgJob` — is the affected node `inProgress` (or recently completed)?
2. If yes, the reload/flap during that window is EXPECTED maintenance behavior —
   correlate the fault/event timestamps (aci_fault_investigation /
   aci_change_audit) with the upgrade window and say so.
3. If no upgrade is running, treat the flap as a real fault and investigate
   normally (aci_interface_troubleshooting).

## Investigation order

1. `firmwareRunning` (+ `firmwareCtrlrRunning`) — current versions; flag any
   inconsistency.
2. `maintUpgJob` — is anything `inProgress` / `completeNok`? Note
   `desiredVersion` and progress.
3. If a flap/fault was reported, correlate its timing with any in-progress
   upgrade window before concluding a hardware problem.

## Interpret / report

- All nodes on the same expected version, no jobs in progress → firmware is
  consistent and stable.
- An upgrade `inProgress` → report the node(s), target version, and progress;
  note that reloads/flaps on those nodes are expected right now.
- `completeNok` → an upgrade failed on a named node — report it as a real issue
  and suggest checking that node's faults.
- Version mismatch with NO active upgrade → flag it as an unexpected
  inconsistency to investigate.

Report actual version and status values; never guess a version. Answer in the
language the user used. Contracts remain out of scope.
