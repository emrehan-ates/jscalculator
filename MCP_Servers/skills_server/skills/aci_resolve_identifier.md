# ACI Resolve Identifier — turn a MAC, serial, or device/interface name into leaf node + physical port(s)

Use this skill whenever you have an identifier (a MAC address, a device serial, an
interface-profile name, or a host/device name) and you need the concrete
leaf **node ID + physical port(s)** before you can query faults, interface
state, bindings, or counters. Almost every troubleshooting task starts here.
This is the single source of truth for the resolution chains — other skills
reference it instead of repeating the steps.

Pick the ONE path that matches your input type. Do not mix paths.

- Input is a **MAC address** (`XX:XX:XX:XX:XX:XX`, `XXXX.XXXX.XXXX`, or 12 hex
  nibbles) → **Path A**.
- Input is a **serial / interface-profile name / device asset tag** (e.g.
  `CX124553KT`, `MSY_CX124553KT`) → **Path B**.
- Input is a **switch hostname** (letters + hyphens, e.g. `DR1-C6-8`) → this is a
  different chain; use aci_switch_name_resolution.
- Input is a **server/ESXi/VM hostname** → use aci_server_locator. Its hostname
  path resolves deterministically via access-policy objects
  (`infraAccPortP`/`infraRsAccPortP`/`infraNodeBlk`) and returns the attachment
  set (data + management).

---

## Path A — MAC address → node + port

### A1. Find the learned endpoint

```
/api/node/class/fvCEp.json?query-target-filter=eq(fvCEp.mac,"AA:BB:CC:DD:EE:FF")
```

Key fields:
- `mac` — the endpoint MAC.
- `fabricPathDn` — WHERE it is learned (the most important field).
- `encap` — the VLAN (e.g. `vlan-354`).
- `bdDn` — the bridge domain.
- `dn` — parse for tenant / app profile / EPG (`uni/tn-T/ap-AP/epg-EPG/...`).

### A2. Parse `fabricPathDn` into node + port

- `paths-<NODE>/pathep-[eth1/X]` → single access port on node `<NODE>`. Record
  node + port. Policy group is NOT in `fabricPathDn` for access — if the user
  asks for the policy group next, hand off to **aci_access_policy_chain Path PG**
  with this node + eth port (do not re-query fvCEp).
- `protpaths-<N1>-<N2>/pathep-[<BUNDLE>]` or `paths-<NODE>/pathep-[<BUNDLE>]`
  (bundle name, not `eth...`) → vPC or PC. **`<BUNDLE>` is already the
  policy-group name** (`infraAccBndlGrp`). Record it immediately. You MUST still
  expand the bundle to member physical ports and node IDs: find the interface
  profile(s) that reference this bundle via `infraRsAccBaseGrp.tDn` ending in
  `accbundle-<BUNDLE>`, read their `infraPortBlk` eth ranges, and resolve nodes
  via switch profile `infraRsAccPortP` → `infraNodeBlk` (see
  aci_access_policy_chain). Do NOT search policy-group classes by MAC or eth
  port string.

Collect ALL physical ports — direct-attached ports and every member port of any
port-channel/vPC.

### A3. Fallback when `fvCEp` returns 0 results

The MAC may be a directly-attached neighbor's chassis/port ID rather than a
learned host. Try the adjacency tables:

```
/api/node/class/lldpAdjEp.json?query-target-filter=or(wcard(lldpAdjEp.chassisIdV,"AABB.CCDD.EEFF"),wcard(lldpAdjEp.portIdV,"AABB.CCDD.EEFF"))
```

Then `cdpAdjEp` with the same filter on `cdpAdjEp.chassisIdV` / `cdpAdjEp.portIdV`.
The adjacency dn is `topology/pod-1/node-<N>/sys/lldp/inst/if-[eth1/X]/adj-...`
→ the local attachment is `node-<N>` + `eth1/X`.

If still nothing, the MAC is not learned and not an adjacency — report that
plainly (see aci_endpoint_troubleshooting for the likely causes).

---

## Path B — serial / interface-profile name → ALL node + port attachments

### B1. Find ALL data-candidate interface profiles

```
/api/node/class/infraAccPortP.json?query-target-filter=wcard(infraAccPortP.name,"CX124553KT")
```

Collect ALL returned interface profiles whose name contains the serial token
(e.g. `MSY_<SERIAL>`, `MSY_<SERIAL>_DATA`, `CSY_<SERIAL>`). These are
data-candidate profiles. Do NOT stop at the first match.

Do NOT invent suffixes like `_DATA` or `_MGMT` if they were not returned by
APIC. Use only names that actually exist.

### B2. Resolve each data-candidate profile to node + port(s)

```
/api/node/mo/<profile_dn>.json?rsp-subtree=full
```

From each profile subtree, read `infraPortBlk.fromCard` / `fromPort` (and
`toCard` / `toPort` if a range) to extract the physical port set. On the same
subtree, also capture `infraRsAccBaseGrp.tDn` (policy group DN/name). If the
user's question is (or becomes) "what is the policy group", that relation is
the answer — do not start a separate blind search.

### B3. Reverse-lookup switch profile(s) for each profile

```
/api/node/class/infraRsAccPortP.json?query-target-filter=eq(infraRsAccPortP.tDn,"<profile_dn>")
```

Extract `nprof-<SWPROF>` from each result dn and keep all unique switch
profiles.

### B4. Get node ID(s) from each switch profile

```
/api/node/mo/uni/infra/nprof-SWPROF.json?rsp-subtree=full&rsp-subtree-class=infraNodeBlk
```

`infraNodeBlk.from_` / `to_` → the node IDs.

If the profile is bound by more than one switch profile (VCF multi-node access),
repeat and collect ALL node IDs. Each (node × port) is a physical attachment
point to check.

### B5. Add management-candidate profiles from the SAME switch profile(s)

After data-candidate profiles are resolved, enumerate other interface profiles
attached to the same switch profile(s) and include them as management
candidates when their profile name does NOT include the serial token.

Practical rule used in this environment:
- profile name includes serial token → data candidate
- profile name does NOT include serial token (but attached to same nprof) →
  management candidate

Resolve management candidates with the same B2-B4 chain and merge into one final
attachment set.

Expected outcome for dual-homed hosts in this fabric: often 4 ports total
(2 data + 2 management across 2 leaves). Do NOT stop after finding one port.

Port-channel note: for PC-based attachments, all member ports can be on the
same leaf; report exactly what APIC returns (do not force two-leaf symmetry).

---

## After resolution

You now have node ID(s) + physical port(s) (and, for bundles or interface
profiles, often the policy-group name already). Hand off to whichever skill the
task needs — the resolution itself is done:

- **Policy group / AEP / access-policy of this port** → aci_access_policy_chain
  **Path PG** (or report `infraRsAccBaseGrp.tDn` / bundle name if already
  captured above). Do not re-resolve the MAC/serial.
- Faults on the port/bundle → aci_fault_investigation.
- Port up/down, speed, CRC/FCS/discards → aci_interface_troubleshooting.
- List ALL EPGs bound to this server/interface profile (e.g. "what are epgs
  on CZ24160G9N") -> aci_epgs_on_profile. Keep per-selector policy-group and
  attachment evidence; resolution is not the final answer for this request.
- Is an EPG bound / deployed there → aci_epg_deployment.
- Why a VLAN is not deploying (full forward chain) → aci_access_policy_chain.

## Good behavior

- Pick ONE path by input type and stay on it; never run the MAC path for a
  serial/name or vice versa.
- A non-MAC name is never an endpoint — `fvCEp` is matched by MAC only.
- `wcard` is case-sensitive; if an exact/`wcard` match returns 0, retry with a
  shorter fragment or different case before concluding it does not exist.
- Always expand a vPC/PC bundle to its member ports — do not stop at the bundle
  name.
- For serial-based resolution, aggregate all attachments (data + management)
  before reporting. First-match answers are considered incomplete.
