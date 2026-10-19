# ACI Network Health Verdict — evidence-based "is the network the problem?" report

Use this skill when another team (server, VMware, database) suspects the network, when a ticket needs a formal statement of fabric health for **specific hosts**, or when the user asks for a verdict like "prove the network is/isn't the issue for server X". Database teams (e.g. Oracle RAC interconnect issues) ask this constantly. The output must be EVIDENCE, not opinion: every claim backed by a query result.

## Scope reminder

**This skill is host/ticket-scoped**, not a cheap fabric-wide status check. If the
user only asks a vague fabric overview ("how is our fabric doing?", "fabric
sağlıklı mı?", "any critical faults on the fabric?") with **no host/MAC/port**:
do **not** run this full multi-port composed path. Follow
**aci_fault_investigation** "Fabric / global health overview (cheap ladder)"
instead (active `faultInst` / `faultSummary` sample + totalCount). Optionally
add **aci_apic_cluster_health** if controllers/APIC cluster are in scope.

Contracts are vzAny permitAny; filtering is on the firewall and OUT OF SCOPE. This verdict covers the fabric's physical/operational layer only. If everything is clean, the conclusion is "look outside the fabric (firewall, host, application)" — never "everything is fine".

## Workflow (this is a composed investigation — reuse the other skills)

1. **Resolve the host(s)** to leaf/port(s): MAC -> aci_endpoint_troubleshooting; hostname/serial -> aci_resolve_identifier; VM name -> aci_vmm_visibility. Two hosts (e.g. RAC nodes that cannot talk) -> resolve both.
2. **Endpoint stability**: is each MAC learned, on ONE consistent path? Learned on multiple paths or endpoint-move history -> flag flapping.
3. **Port health on EVERY uplink** (aci_interface_troubleshooting): adminSt/operSt, operStQual if down, operSpeed (must be >= 10G here), CRC/FCS/errors/discards — interpreted per that skill's rules (note the single-snapshot limitation honestly).
4. **Faults, 48h window** (aci_fault_investigation): scoped `faultInst` + `faultRecord` on each **resolved** port, bundle, and EPG only. Every history query must use small `page-size` (≤ 50), `order-by`, and **page=0 only** — never fabric-wide unpaged `faultRecord` and never multi-page crawls.
5. **Recent changes** (aci_change_audit): aaaModLR on the ports/policy groups/EPGs in the window; eventRecord for link flaps — same sample/page-size discipline on high-cardinality logs.
6. **If two endpoints**: BD/VRF relationship per aci_connectivity_between_endpoints (same BD = pure L2; different VRF = leaves the fabric — direct the user to the "Sistem Güvenliği" team for routing/firewall).

## Verdict format

Start with ONE of these verdicts, then the evidence:

- **FABRIC CLEAR** — all endpoints learned and stable, all ports up at expected speed, zero CRC/FCS, no relevant faults or changes in the window. State clearly: "no evidence of a fabric-side problem; investigate host NIC/driver, firewall, or application." 
- **FABRIC SUSPECT** — name the exact element and evidence: which port, which counter/fault/change, with values and timestamps.
- **INCONCLUSIVE** — state exactly which checks could not be completed and why (e.g. endpoint not learned, budget ran out).

Evidence section, per host: MAC/hostname, learned path(s), VLAN/EPG/BD, per-port state + speed + counter values, faults found (code/severity/time), changes found (user/time/changeSet). Keep it compact — this text will be pasted into a ticket.

## Good behavior

- Never soften a SUSPECT verdict to avoid blame, and never declare CLEAR without having actually run the port + fault + change checks.
- Non-zero discards with clean CRC/FCS = likely congestion — say "load-related, not cabling" rather than a vague "network issue".
- Answer in the language the user asked (Turkish or English); these reports often go to non-network teams, so avoid raw DNs in the conclusion — put DNs only in the evidence details.
