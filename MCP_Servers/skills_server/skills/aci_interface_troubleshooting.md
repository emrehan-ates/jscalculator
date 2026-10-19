# ACI Interface Troubleshooting — port up/down, errors, CRC/FCS, speed

Use this skill when the user asks about a physical interface/port: is it up, why is it down, are there errors or CRC/FCS counters, is the speed correct.

## Config vs operational state live in different objects

- l1PhysIf = CONFIG (adminSt). DN: topology/pod-P/node-N/sys/phys-[eth1/X]
- ethpmPhysIf = OPERATIONAL child (operSt, operSpeed, operStQual = reason down).

Read both:
/api/node/mo/topology/pod-1/node-<N>/sys/phys-[<PORT>].json?rsp-subtree=full
(<PORT> like eth1/1, kept exactly inside the brackets.)
For all ports on a node: query l1PhysIf children of the node sys, pull the ethpmPhysIf child for operSt.

## Interpret state

- adminSt up AND operSt up -> port up; check errors below.
- adminSt down -> administratively shut (a config choice, not a fault).
- adminSt up AND operSt down -> physical problem. Read operStQual on ethpmPhysIf for the reason (e.g. sfp-missing, link-failure, sfp-speed-mismatch, wait-linkup). Likely cable, transceiver/SFP, or far-end device.
- operSpeed: in this environment a host-facing port must run at 10G or faster. If operSpeed is below 10G, flag it as a speed / auto-negotiation problem.

## Error counters (CRC / FCS / discards)

In rmon children of the interface:
/api/node/mo/topology/pod-1/node-<N>/sys/phys-[<PORT>].json with query-target=children and target-subtree-class=rmonEtherStats,rmonDot3Stats,rmonIfIn,rmonIfOut

- rmonEtherStats.cRCAlignErrors -> CRC errors
- rmonDot3Stats.fCSErrors -> FCS errors
- rmonIfIn.errors / rmonIfIn.discards -> input errors / drops
- rmonIfOut.errors / rmonIfOut.discards -> output errors / drops

<!-- SYNC NOTE: this "Interpret counters" section is the single source of truth
     for CRC/FCS interpretation. aci_fault_investigation.md references it
     instead of restating the rules. If you change the rules, change them HERE. -->

## Interpret counters (single source of truth)

- Zero on all counters = clean port.
- Non-zero CRC or FCS -> flag as a likely PHYSICAL-LAYER problem: bad cable, dirty/failing transceiver/SFP, or far-end NIC. There is NO fixed threshold — always report the actual values; never invent a threshold.
- "Growing" cannot be proven from ONE snapshot. ACI counters are cumulative since the last clear/reboot, and you only see a point-in-time value. Say this explicitly: report the current values, and recommend re-checking after 10-15 minutes (or comparing against the counter baseline, if a baseline/delta tool is available in your tool list) to confirm whether the counters are actively increasing.
- Non-zero discards with ZERO CRC/FCS usually indicate congestion or buffer drops, not a cabling fault — report them separately and say the cause is more likely load-related.

## Good behavior

- Always report BOTH the operational state and the counter values, then give the interpretation from the rules above.
- If the port is down, operStQual is the headline — counters are secondary.
- Tie a faulty port back to the endpoint or EPG the user cares about (aci_endpoint_troubleshooting, aci_epg_bindings).
