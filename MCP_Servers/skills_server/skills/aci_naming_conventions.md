# ACI Naming Conventions — how names in THIS fabric map to servers, ports, policy groups and EPGs

Read first when the user gives a server serial, team prefix (MSY_/CSY_/SG_), switch hostname or EPG/VLAN name — tells which class to search and how to build the wcard filter.

This is the single source of truth for site naming. Other skills reference it
instead of repeating it. Naming is a HINT for where to search — APIC relations
(`infraRsAccBaseGrp`, `infraRsAttEntP`, `fvRsPathAtt`, ...) are the proof.

## Fabric basics

- Pod: always `pod-1` (use the pod in APIC-returned DNs if one ever differs).
- Leaf node IDs: `101`–`198`. Spine node IDs: `401`–`402`.
- Switch hostnames look like `ESY-B-DR2-B05-10-119`. The user usually types a
  fragment (`DR2-B05-10`, `DR1-C6-8`, `A1-511-20`). The last segment is usually
  the node ID but ALWAYS confirm with `fabricNode.id`. Zero-padding and site
  rules (EVM / KKB) → **aci_switch_name_resolution**.

## Team prefixes

| Prefix | Owner | What is usually behind it |
|---|---|---|
| `MSY_` | Main Banking Servers team | physical servers / ESXi hosts |
| `CSY_` | Subsidiary Banking Systems team | servers / ESXi hosts (VMware vPCs) |
| `SG_`  | System Security team | firewalls, SSL-VPN devices; `SG_` names are often EPGs (e.g. `SG_TEST_FW`) |

## Server identity = serial / asset tag

Other teams identify a server by its serial / asset tag (e.g. `CX124553KT`,
`CZ24160G9N`, `9YZK5B3`: 7–10 upper-case alphanumerics, often starting with
`CX`/`CZ`). The fabric encodes that serial into access-policy names:

| Object | Name pattern | Class |
|---|---|---|
| Leaf interface profile (data ports) | `<TEAM>_<SERIAL>`, sometimes `<TEAM>_<SERIAL>_DATA` | `infraAccPortP` |
| PC / vPC policy group (bundle) | usually `<TEAM>_<SERIAL>` | `infraAccBndlGrp` |
| Access (single-port) policy group | may be `<TEAM>_<SERIAL>` or a shared generic name | `infraAccPortGrp` |
| Static binding path of a bundle | `.../protpaths-N1-N2/pathep-[<TEAM>_<SERIAL>]` | `fvRsPathAtt.tDn`, `fabricPathEp` |
| VMM dynamic deployment | `.../dyatt-[...pathep-[<TEAM>_<SERIAL>]]/...` | `fvIfConn.dn` |

Consequences:

- A `<TEAM>_<SERIAL>` name is NEVER an EPG and NEVER an endpoint (`fvCEp` is
  MAC-only). Search it on `infraAccPortP`, `infraAccBndlGrp`,
  `infraAccPortGrp` (and `fabricPathEp` for the bundle path).
- The interface profile and the policy group frequently share the same name,
  but they are different objects. Confirm with the selector's
  `infraRsAccBaseGrp.tDn` before saying "the policy group is X".
- Do not invent suffixes (`_DATA`, `_MGMT`). Use only names APIC returns.
- Dual-homed hosts typically have 4 ports: 2 data + 2 management across 2
  leaves. A PC can have all members on one leaf. Report what APIC returns.

## Management ports (ESXI-Infra / ESX-Infra profiles)

A host's management ports are NOT in its own `<TEAM>_<SERIAL>` profile. Each
leaf has ONE shared infra interface profile:

| Object | Name pattern | Class |
|---|---|---|
| Infra interface profile of leaf N | `ESXI-Infra-N` or `ESX-Infra-N`, any mix of `-`/`_` — 8 spellings: `ESXI-Infra-110`, `ESXI_Infra_110`, `ESXI-Infra_110`, `ESXI_Infra-110`, `ESX-Infra-110`, `ESX_Infra_110`, `ESX-Infra_110`, `ESX_Infra-110` | `infraAccPortP` |
| Team selectors inside it (usually 2) | `ESXI-Infra-MSY-110`, `ESXI-Infra-CSY-110` | `infraHPortS` |
| One host's management port | port block whose **description** contains the host serial/name | `infraPortBlk.descr` |

So: management port of host H on leaf N = the `infraPortBlk` under
`ESXI-Infra-N` (or `ESX-Infra-N`) whose `descr` mentions H. Check EVERY leaf the host's data ports
are on (a host on 109 + 110 → look in ESXI-Infra-109 AND ESXI-Infra-110).
Procedure and tool: **aci_resolve_identifier** "Management ports".

## EPG names

- EPG names often encode the VLAN ID (e.g. `VLAN-109`, or a name ending in the
  VLAN number). Use this only as a search hint
  (`wcard(fvAEPg.name,"109")`). The real VLAN is `fvRsPathAtt.encap` /
  `fvRsDomAtt.encap` / `fvIfConn.encap`.
- To find EPGs by VLAN number with certainty, search the bindings:
  `eq(fvRsPathAtt.encap,"vlan-109")`.

## Domains

- VMware VMM domain DN: `uni/vmmp-VMware/dom-<NAME>`.
- Physical domain: `uni/phys-<NAME>`; L3 domain: `uni/l3dom-<NAME>`.
- VLAN pool DN: `uni/infra/vlanns-[<NAME>]-static` or `-dynamic`
  (VMM domains normally use dynamic pools).

## Search recipe from a bare name

1. Classify the token with the tables above (MAC? serial? `<TEAM>_` prefix?
   switch fragment? EPG/VLAN-like?).
2. Query the most likely class first with `wcard` on `name` (wcard is a
   case-sensitive substring match; serials are upper-case — retry upper-case).
3. If the class returns 0, move to the next likely class from the table — do
   NOT repeat the same query, and do NOT switch to `fvCEp` for a non-MAC.
4. If every likely class returns 0, say the name was not found and list the
   classes you searched.
