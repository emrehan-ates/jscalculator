# ACI Server Locator — find a server by HOSTNAME or MAC address (for server / VMware / database teams)

Use this skill when someone asks "where is my server", "which port is <hostname> connected to", "which port is <MAC> connected to", or gives a HOSTNAME (e.g. dbprod-node3, CX2341TX) or a MAC address instead of an explicit leaf/port. Server, VMware, and database teams know their hostnames, not MACs — this skill bridges that gap. (For a SWITCH hostname, use aci_switch_name_resolution.)

**EPG inventory hand-off:** for "what are epgs on CZ24160G9N" or which EPGs
are bound to a server/interface profile, use **aci_epgs_on_profile**. Location
is only an intermediate step in that task: retain selector, policy-group, and
path evidence and continue to enumerate `fvRsPathAtt`; do not stop at ports.
The locator completion/stop rules below apply to location-only requests.

## Step 0 — Determine the input type (MUTUALLY EXCLUSIVE — pick ONE path only)

- **MAC address** — matches `XX:XX:XX:XX:XX:XX` or `XXXX.XXXX.XXXX` or `XXXXXXXXXXXX` (hex, 12 nibbles). Go to **Step 1 (MAC)** ONLY.
- **Hostname** — anything else (e.g. `dbprod-node3`, `CX2341TX`, `MSY_CX124553KT`, `esxi-host-05.domain.local`). Go to **Step 2 (Hostname)** ONLY.

**CRITICAL: These two paths are MUTUALLY EXCLUSIVE. Once you classify the input, follow ONLY that path and NEVER run queries from the other path. Do NOT cross-reference, do NOT "also try the MAC path", do NOT fall back from one path into the other. If your path returns results, report them and stop. If your path returns nothing after all its steps, say so and stop — do NOT switch to the other path.**

---

## Step 1 — Resolve by MAC address (ONLY when input is a MAC)

Resolve the MAC to node + physical port(s) using **aci_resolve_identifier**
(Path A): find the endpoint with `fvCEp`, parse `fabricPathDn`, expand any
vPC/PC bundle to its member ports, and fall back to LLDP/CDP adjacency if
`fvCEp` returns nothing. Record `mac`, `fabricPathDn`, `encap` (VLAN), `bdDn`,
and `dn` (tenant/AP/EPG) for the report.

Once you have every physical port, proceed to **Step 4 (report)**. Do
NOT run any hostname/LLDP-sysName/CDP-devId/access-policy-chain queries — those
belong to the hostname path only.

---

## Step 2 — Resolve by HOSTNAME (ONLY when input is a hostname — do NOT run any MAC/fvCEp queries in this path)

A hostname can resolve through a deterministic path. If the hostname is not found, report it as not found.

### 2a — Access policy chain (interface profile name, e.g. CX2341TX, MSY_CX124553KT)

When the hostname looks like a server asset tag / serial / interface-profile name
(short alphanumeric string often starting with CX, CZ, or team prefixes like MSY_, used as the ACI interface
profile name), resolve it with **aci_resolve_identifier** (Path B): interface
profile (`infraAccPortP`) → port block → switch profile (`infraRsAccPortP`) →
node ID(s) (`infraNodeBlk`). This yields leaf node ID(s) + port(s); collect all
if multiple nodes (VCF multi-node access).

Apply these completion rules for hostname/serial lookup:

- Do NOT stop at the first `infraAccPortP` match.
- Collect ALL profiles whose name includes the serial token (data candidates),
	including variants like `<TEAM>_<SERIAL>` and `<TEAM>_<SERIAL>_DATA` when they
	actually exist.
- For each resolved switch profile, also collect other attached interface
	profiles that do NOT include the serial token and treat them as management
	candidates.
- Merge all resolved `(node, port)` attachments and report the full set.

Expected in this environment: many hosts are dual-homed with separate data and
management paths (often 4 total ports across 2 leaves). Return all of them.

Port-channel note: for PC attachments, all member ports may terminate on the
same leaf; report APIC's real result, do not force two-leaf symmetry.

**SOP: Once you have the full attachment set (data + management), the locator task is complete. Do NOT query `ethpmPhysIf` or `l1PhysIf` to "verify" port state — that belongs to aci_interface_troubleshooting.**

---

## Step 4 — Report

Once you have node + port(s) from your ONE chosen path, report them clearly.

**LITERAL BOUNDARY: Do NOT query for port health, speed, or faults here. If the user's original question asks for "where is X" AND "is it up", report the location first, then explicitly state you are switching to aci_interface_troubleshooting to check the health.**

**Follow-up: policy group of the located port.** If the user (same turn or next
turn, using conversation context) asks which policy group / interface policy /
AEP that port uses:

- Hostname path (Step 2a): you already read the interface-profile subtree —
  answer from `infraRsAccBaseGrp.tDn` captured there. Re-read that one profile
  MO with `rsp-subtree=full` only if you did not keep the relation.
- MAC path (Step 1): keep the resolved `node + eth` (and bundle name from
  `fabricPathDn` if present). Switch to **aci_access_policy_chain Path PG**.
  Do NOT re-run fvCEp in a loop and do NOT wcard policy-group classes for the
  MAC or `eth1/X` string.

## Output format

Report per attachment: leaf node (resolve hostname via fabricNode if helpful),
port, and profile origin.

For hostname/serial path, include classification:
- data (profile name contains serial token)
- management (profile name does not contain serial token but is attached to the
	same switch profile)

For MAC path, also include VLAN/encap and EPG from fvCEp.

## Good behavior

- **Pick ONE path based on input type and STAY on it.** Never run both the MAC path and the hostname path for the same request.
- Never treat a hostname as an endpoint name — fvCEp is matched by MAC only.
- Multiple adjacencies / ports = multiple uplinks, not an error.
- For a vPC/PC bundle, always resolve and report the member physical ports, not just the bundle name.
- If your chosen path returns nothing after all its steps, say so and stop — do NOT switch to the other path or start guessing.
- **SOP: Locate first, then switch skill for health. Do not mix `infraAccPortP` (config) with `ethpmPhysIf` (operational) in a single tool call or sequence within this skill.**
- For hostname/serial lookups, answer is complete only after full attachment
	aggregation (data + management). Returning a single port when more are
	discoverable under the same switch profiles is incorrect.
