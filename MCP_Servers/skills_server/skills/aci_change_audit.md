# ACI Change Audit — what changed, who changed it, and does it explain a fault

Use this skill when the user asks "what changed", "who modified X", "was there a config change last night", or when a fault/problem appeared recently and you need to check whether a configuration change coincides with it. The #1 troubleshooting question after "what is broken" is "what changed right before it broke".

## The two log classes

- **aaaModLR** = the APIC audit log. One record per configuration change (create/modify/delete) made through the GUI, API, or any automation.
- **eventRecord** = operational events (link went down/up, node events, state transitions) that did NOT necessarily raise a fault. Use it to see link flaps and transient events that faultRecord misses.

Both are append-only logs, filtered the same way as faultRecord: by `affected` (the changed/affected object DN) and `created` (timestamp).

## aaaModLR — key attributes

- affected : DN of the object that was changed — filter on THIS.
- user : who made the change (local or remote user; automation accounts show here too).
- ind : creation / modification / deletion.
- changeSet : WHICH attributes changed and their new values (most useful field).
- descr : human-readable description of the change.
- created : when.
- trig : what triggered it (config, etc.).

## Query patterns

Changes in a time window (compute window start = current UTC minus N hours, format 2026-06-23T00:00:00; default 48h):
```
/api/node/class/aaaModLR.json?query-target-filter=gt(aaaModLR.created,"<window-start>")&order-by=aaaModLR.created|desc
```

Changes on a specific object (port, policy group, EPG, AEP...):
```
/api/node/class/aaaModLR.json?query-target-filter=and(gt(aaaModLR.created,"<window-start>"),wcard(aaaModLR.affected,"<dn fragment>"))&order-by=aaaModLR.created|desc
```

Changes by a specific user:
```
/api/node/class/aaaModLR.json?query-target-filter=and(gt(aaaModLR.created,"<window-start>"),eq(aaaModLR.user,"<username>"))&order-by=aaaModLR.created|desc
```

Operational events on a port (link flaps that raised no fault):
```
/api/node/class/eventRecord.json?query-target-filter=and(gt(eventRecord.created,"<window-start>"),wcard(eventRecord.affected,"node-<N>"),wcard(eventRecord.affected,"eth1/X"))&order-by=eventRecord.created|desc
```

## Fault correlation workflow

1. Get the fault's timestamp: faultRecord.created (or faultInst.lastTransition) — see aci_fault_investigation.
2. Query aaaModLR in a window around that time (e.g. 2 hours before the fault) with `affected` fragments for the object AND its policy chain parents — the change that broke a port is often on the policy group, AEP, VLAN pool, or EPG, not the port itself. Try fragments in this order: the port/bundle name, the policy group name, the EPG name, the AEP name.
3. Query eventRecord for the same window on the port to see the exact link-transition sequence.
4. Report: "fault F#### on <object> was raised at <time>; N minutes earlier user <user> modified <affected> (changeSet: ...)". If NO change is found, say so explicitly — that points back to a physical cause.

## Good behavior

- Always report user + changeSet + time, not just "a change happened".
- A change AFTER the fault does not explain it — compare timestamps carefully.
- Broad fabric-wide windows can return many records; scope by affected fragment whenever the user's problem is scoped.
- Do not analyze contracts (vzAny permitAny environment) even if contract changes appear in the log — mention them only as "a contract change occurred" without deep analysis.
