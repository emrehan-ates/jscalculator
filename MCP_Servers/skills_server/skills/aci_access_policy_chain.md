# ACI Access Policy Chain — policy group of a port, and why a VLAN/port is (not) deployed

Use for "policy group of this port/MAC", policy grubu, AEP/access-policy of a known leaf+eth, or broken VLAN deploy on an access port — Path PG reverse-walks node→switch profile→interface profile→infraRsAccBaseGrp (never filter PG classes by eth/MAC).

Use this skill when:
- the user already knows a leaf/port (or just found it via MAC/hostname) and asks
  **which policy group** that port uses (`policy group`, `policy grubu`,
  `hangi interface policy`, AEP of this port, access-policy of this port), OR
- a port "does not work", a VLAN is "not passing", or an endpoint is not learned
  even though cabling looks fine (broken link in the access policy chain).

This skill is the single source of truth for walking the access-policy chain in
BOTH directions. Do NOT invent filters on `infraAccPortGrp`/`infraAccBndlGrp`
for a physical eth port name — policy groups are not keyed by `eth1/X`.

## The chain (each link must exist for a VLAN to deploy on a port)

```
Switch profile (infraNodeP)
  -> binds interface profile (infraRsAccPortP)
  -> holds node IDs (infraNodeBlk)
Interface profile (infraAccPortP)
  -> port selector (infraHPortS)
  -> port block (infraPortBlk = eth ports)
  -> relation to policy group (infraRsAccBaseGrp.tDn)
Policy group (infraAccPortGrp access | infraAccBndlGrp PC/vPC)
  -> AEP (infraRsAttEntP)
AEP (infraAttEntityP) -> domain (infraRsDomP)
Domain -> VLAN pool (infraRsVlanNs)
VLAN pool -> encap blocks (fvnsEncapBlk) must CONTAIN the VLAN
```

If any link is missing, or the VLAN is outside the pool range, the VLAN will not
deploy even though everything "looks" configured.

## Path PG — known leaf + physical port → policy group (PRIMARY for "policy group of this port")

Use this when you already have `node-N` + `ethX/Y` (from conversation history,
`fvCEp.fabricPathDn`, LLDP/CDP, or aci_resolve_identifier / aci_server_locator).

**STOP conditions:** once you have `infraRsAccBaseGrp.tDn` (and optionally the
policy-group name parsed from it), answer. Do NOT keep re-querying the same
class. Do NOT probe random MO classes looking for the eth port string.

### PG0. Fast path when `fabricPathDn` already names a bundle

If the known path is:

- `.../protpaths-<N1>-<N2>/pathep-[<BUNDLE>]` or
- `.../paths-<N>/pathep-[<BUNDLE>]` where `<BUNDLE>` is **not** `eth...`

then `<BUNDLE>` **is** the PC/vPC policy-group name. Resolve it directly:

```
/api/node/class/infraAccBndlGrp.json?query-target-filter=eq(infraAccBndlGrp.name,"<BUNDLE>")
```

Optionally read that MO with `rsp-subtree=full` for AEP (`infraRsAttEntP`).
You are done — skip PG1–PG4.

If the path is `.../pathep-[eth1/X]` (access), continue with PG1.

### PG1. Find switch profile(s) that include this node

```
/api/node/class/infraNodeBlk.json?query-target-filter=and(le(infraNodeBlk.from_,"<N>"),ge(infraNodeBlk.to_,"<N>"))
```

Each hit's DN parent is `uni/infra/nprof-<SWPROF>`. Collect unique switch
profile names. If 0 results, the node is not in any switch profile — report that.

### PG2. List interface profiles bound to each switch profile

```
/api/node/mo/uni/infra/nprof-<SWPROF>.json?rsp-subtree=children&rsp-subtree-class=infraRsAccPortP
```

Each `infraRsAccPortP.tDn` is an interface profile DN
(`uni/infra/accportprof-...`).

### PG3. Match the physical port inside each interface profile

For each candidate interface profile:

```
/api/node/mo/<interface_profile_dn>.json?rsp-subtree=full
```

Walk children:

1. Under each `infraHPortS`, read every `infraPortBlk`.
2. A block covers the target port when card/port fall inside
   `fromCard`/`fromPort` … `toCard`/`toPort` (single-port blocks have from=to).
3. On the **same** `infraHPortS` that matched the port, read
   `infraRsAccBaseGrp.tDn` — that is the policy group DN.

Parse the PG DN:

- `uni/infra/funcprof/accportgrp-<NAME>` → access policy group `infraAccPortGrp`
- `uni/infra/funcprof/accbundle-<NAME>` → PC/vPC policy group `infraAccBndlGrp`

If several selectors match (unusual), report all distinct policy groups.

### PG4. Optional enrich (only if user also asked AEP / domain / VLAN pool)

```
/api/node/mo/<policy_group_dn>.json?rsp-subtree=full
```

Then continue the forward walk below from step 3.

### Anti-patterns (these cause the "10 identical empty queries" failure)

- Do NOT query `infraAccPortGrp` / `infraAccBndlGrp` with `wcard` on `eth1/47`
  or the MAC — those classes have no eth/MAC attributes.
- Do NOT re-run the same `fvCEp` / `fabricNode` / empty class query hoping the
  policy group appears.
- Do NOT treat the interface-profile name and the policy-group name as the same
  object until `infraRsAccBaseGrp.tDn` proves it (they often match by naming
  convention, but APIC truth is the relation).
- If PG1–PG3 yield no match after all bound interface profiles are checked, say
  "no access policy covers leaf N ethX/Y" once and stop.

## How to walk it forward (from a known interface profile or policy group)

1. If you only have the interface profile name/DN, read its subtree
   (`rsp-subtree=full`): confirm the port block; read
   `infraRsAccBaseGrp.tDn` = the policy group. (This is the short path when
   the user named `MSY_<SERIAL>` / interface profile directly.)
2. Find the switch profile for the interface profile by reverse lookup when you
   need nodes:
   `/api/node/class/infraRsAccPortP.json?query-target-filter=eq(infraRsAccPortP.tDn,"<interface profile dn>")`
   Parent `uni/infra/nprof-<SWPROF>` is the switch profile; `infraNodeBlk` =
   node IDs.
3. Read the policy group subtree: `infraRsAttEntP.tDn` = the AEP.
4. Read the AEP subtree: `infraRsDomP` = the domain(s).
5. Read the domain: `infraRsVlanNs` = the VLAN pool.
6. Read the VLAN pool subtree (`fvnsEncapBlk` from_/to_): confirm the VLAN is
   inside a block.

Report which link is missing or breaks. Most common breaks: policy group has no
AEP, AEP has no domain, or the VLAN is outside the pool range.

## Cross-check the tenant side

Even with a complete chain, the EPG must associate the SAME domain (`fvRsDomAtt`)
and be statically bound to the port with the right encap. See aci_epg_deployment.

## Hand-off

- Need leaf+port first from MAC/serial/hostname → aci_resolve_identifier or
  aci_server_locator, **then return here** for Path PG.
- Only asked "is the port up / CRC" → aci_interface_troubleshooting (not this
  skill).

