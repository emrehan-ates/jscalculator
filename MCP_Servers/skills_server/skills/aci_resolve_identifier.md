# ACI Resolve Identifier — find where a server/host is connected: MAC, serial, interface-profile name or hostname → leaf + port(s)

Use for "where is server X / hangi porta bağlı", management (mgmt/yönetim) ports of a host, a MAC, serial (CX…/CZ…), MSY_/CSY_ name or ESXi hostname → leaf + port(s).

Almost every port/fault/binding task starts here. This is the single source of
truth for resolution chains; other skills reference it. Naming rules →
**aci_naming_conventions**. Switch hostnames (`DR1-C6-8`) →
**aci_switch_name_resolution** (different chain).

Pick ONE path by input type and stay on it:

- **MAC** (`XX:XX:XX:XX:XX:XX`, `XXXX.XXXX.XXXX`, 12 hex nibbles) → **Path A**.
- **Serial / asset tag / interface-profile name** (`CX124553KT`, `MSY_CX124553KT`) → **Path B**.
- **Free hostname** (`dbprod-node3`, `esxi-05.domain.local`) → **Path C**.

Never search `fvCEp` with a non-MAC; never run Path A for a serial.

## Path A — MAC → node + port

1. `/api/node/class/fvCEp.json?query-target-filter=eq(fvCEp.mac,"AA:BB:CC:DD:EE:FF")`
   (MAC upper-case, colon format). Keep `fabricPathDn`, `encap`, `bdDn`, `dn`
   (tenant/AP/EPG from `uni/tn-T/ap-AP/epg-E/...`).
2. Parse `fabricPathDn`:
   - `paths-N/pathep-[eth1/X]` → access port node N eth1/X. PG via
     **aci_access_policy_chain** Path PG.
   - `protpaths-N1-N2/pathep-[B]` / `paths-N/pathep-[B]` (B not `eth`) → vPC/PC.
     `B` is the bundle policy-group name and usually `<TEAM>_<SERIAL>` = the
     server. Member ports: Path PORTS in aci_access_policy_chain.
3. 0 results → the MAC may be a directly attached neighbor's chassis/port ID.
   One query covers both LLDP fields:
   `/api/node/class/lldpAdjEp.json?query-target-filter=or(wcard(lldpAdjEp.chassisIdV,"aabb.ccdd.eeff"),wcard(lldpAdjEp.portIdV,"aabb.ccdd.eeff"))`
   then the same on `cdpAdjEp` (`devId`/`portId` fields if chassisIdV absent).
   The adjacency dn `topology/pod-1/node-N/sys/lldp/inst/if-[eth1/X]/adj-1` →
   node N eth1/X. Try the MAC formats APIC uses (`aa:bb:..`, `aabb.ccdd.eeff`).
4. Still nothing → "MAC not learned and not an LLDP/CDP neighbor"; likely
   causes in **aci_endpoint_troubleshooting**.

## Path B — serial / interface-profile name → ALL attachments

B1. Data-candidate profiles (contain the serial token):
`/api/node/class/infraAccPortP.json?query-target-filter=wcard(infraAccPortP.name,"<SERIAL>")`
Collect ALL hits (`MSY_<SERIAL>`, `MSY_<SERIAL>_DATA`, `CSY_<SERIAL>` …). 0 hits
→ retry upper-case / a shorter fragment once, then also try the bundle name
`/api/node/class/fabricPathEp.json?query-target-filter=wcard(fabricPathEp.name,"<SERIAL>")`
(gives the vPC/PC path directly). Still 0 → not found; stop.

B2. Ports + PG per profile: `/api/node/mo/<profile dn>.json?rsp-subtree=full`.
For EACH `infraHPortS` keep its `infraPortBlk` ports and its own
`infraRsAccBaseGrp.tDn` (= policy group; `accbundle-` → PC/vPC, `accportgrp-` → access).

B3. Switch profiles (batch all profiles in one call):
`/api/node/class/infraRsAccPortP.json?query-target-filter=or(eq(infraRsAccPortP.tDn,"<dn1>"),eq(infraRsAccPortP.tDn,"<dn2>"))`
→ unique `nprof-<SWPROF>`.

B4. Node IDs: `/api/node/mo/uni/infra/nprof-<SWPROF>.json?rsp-subtree=full&rsp-subtree-class=infraNodeBlk`
→ `infraNodeBlk.from_`/`to_`. Several switch profiles → collect all nodes.

B5. Management ports (when the user asks for management/all ports, or the
full attachment set): follow **Management ports** below with the B4 leaves.

Result: every (node × port) with profile, selector, PG, type (access/PC/vPC)
and data/management label. Typical dual-homed host: 2 data + 2 management
ports across 2 leaves; a PC may sit on one leaf — report what APIC returns.

## Management ports — ESXI-Infra-<leaf> / ESX-Infra-<leaf> profiles

Management ports are NOT in the host's own profile. Each leaf N has a shared
interface profile `ESXI-Infra-N` or `ESX-Infra-N` (any `-`/`_` mix) with team selectors such as
`ESXI-Infra-MSY-N`; the port block whose **description** names the host is
its management port (see **aci_naming_conventions**).

**Fast path:** call the tool `find_management_ports(host="<serial or name>")`
(add `nodes=["109","110"]` if the user named the leaves). It runs the steps
below and returns leaf, port, profile, selector, description and binding path.

Manual steps (same logic):

M1. Leaves to check: the host's data leaves (B1–B4), or the leaves the user
named. A host on 109 + 110 → check both.

M2. Per leaf, ONE query covers all 8 spellings (ESXI or ESX × `-`/`_`):
`/api/node/class/infraAccPortP.json?query-target-filter=or(eq(infraAccPortP.name,"ESXI-Infra-110"),eq(infraAccPortP.name,"ESXI_Infra_110"),eq(infraAccPortP.name,"ESXI-Infra_110"),eq(infraAccPortP.name,"ESXI_Infra-110"),eq(infraAccPortP.name,"ESX-Infra-110"),eq(infraAccPortP.name,"ESX_Infra_110"),eq(infraAccPortP.name,"ESX-Infra_110"),eq(infraAccPortP.name,"ESX_Infra-110"))`
0 results → `or(wcard(infraAccPortP.name,"nfra-110"),wcard(infraAccPortP.name,"nfra_110"))`
and keep only names of the form ESX(I)?Infra?110.

M3. Read it: `/api/node/mo/uni/infra/accportprof-<NAME>.json?rsp-subtree=full`.
Under each `infraHPortS` (e.g. `ESXI-Infra-MSY-110`) check every
`infraPortBlk.descr`; case-insensitive contains the serial (without the
MSY_/CSY_ prefix) or the hostname → that block's `eth<fromCard>/<fromPort>`
is the management port on leaf 110.

M4. Leaf not known at all: one query
`/api/node/class/infraPortBlk.json?query-target-filter=wcard(infraPortBlk.descr,"<SERIAL>")`
and keep blocks whose dn is under an `accportprof-ESX(I)?Infra?N` profile.

Report per leaf: management port, profile, selector, description. A leaf
whose ESXI-Infra profile has no matching description is reported as
"no management port found on leaf N" — keep the other leaf's result.

## Path C — free hostname

1. If the hostname contains a serial-like token or a `<TEAM>_` prefix → Path B.
2. Otherwise the host may announce itself over LLDP/CDP (ESXi, many servers):
   `/api/node/class/lldpAdjEp.json?query-target-filter=wcard(lldpAdjEp.sysName,"<host>")`
   then `/api/node/class/cdpAdjEp.json?query-target-filter=wcard(cdpAdjEp.devId,"<host>")`.
   Adjacency dn → node + eth port; then Path PG for its policy group (its PG /
   interface profile name usually reveals the serial → Path B for the full set).
3. For an ESXi host also: `/api/node/class/compHv.json?query-target-filter=wcard(compHv.name,"<host>")`
   (VMM inventory; see **aci_vmm_visibility**).
4. Nothing anywhere → not found; list what you searched.

## After resolution — hand off

- EPGs on the management ports → **aci_epg_bindings** B with the paths
  returned by `find_management_ports` (`tdns`).
- Policy group / AEP / domain of the port → **aci_access_policy_chain** (PG
  is often already captured in B2 — reuse it).
- EPGs / VLANs on the server's ports → **aci_epg_bindings** B.
- Port up/down, CRC → **aci_interface_troubleshooting**; faults → **aci_fault_investigation**; vPC state → **aci_vpc_health**.

Location tasks are complete once the attachment set is collected; do not query
`l1PhysIf`/`ethpmPhysIf` unless the user asked about port state.

## Answering

Report per attachment: leaf (hostname via `fabricNode` if useful), port or
bundle, profile, PG, data/management (management only from the ESXI-Infra
description match — never guess it from other profiles). If some steps failed (e.g. profile found
but switch profile lookup empty), report the partial result and name the
missing link. Only "not found" when every step of the chosen path was empty.
