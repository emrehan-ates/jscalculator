# ACI EPG Deployment — is an EPG actually on a port with the right VLAN

Use this skill when an EPG "is not working on a port", a server in a known EPG is unreachable, or you need to confirm an EPG's static binding, VLAN/encap, and domain.

For "what are epgs on CZ24160G9N" or listing ALL EPGs bound to a server,
interface profile, or its ports, use **aci_epgs_on_profile** instead. Its
inventory must inspect every resolved path without an EPG-name filter. The
first-hit stop rules below are only for checking a specified EPG, not for
enumerating all EPGs on a profile.

## Check in order

1. Static binding exists? The EPG must have an fvRsPathAtt to the port/vPC.
   Read the EPG subtree: /api/node/mo/uni/tn-T/ap-AP/epg-EPG.json?rsp-subtree=full
   - fvRsPathAtt.tDn = the path (topology/pod-1/paths-<NODE>/pathep-[<PORT>] single, or protpaths-... for vPC).
   - fvRsPathAtt.encap = the VLAN (e.g. vlan-354). Confirm it matches expected. Here the VLAN is normally encoded in the EPG name.
   - fvRsPathAtt.mode = regular (trunk/tagged), untagged (access), or native.
2. Domain associated? fvRsDomAtt under the EPG must point to the domain that the port's AEP uses. If the EPG domain and the port domain differ, the VLAN will not deploy (cross-check aci_access_policy_chain).
3. EPG -> BD bound? fvRsBd.tnFvBDName must point to a real BD.
4. Resolution/deployment immediacy: if on-demand (lazy), the VLAN only deploys when an endpoint appears. If nothing is connected yet, "not deployed" can be expected. Immediate deploys regardless.

## Confirm it actually deployed on the leaf

The binding is config. To confirm the VLAN is really programmed, the deployed VLAN appears as vlanCktEp on the node (and the EPG shows fvIfConn). If the binding exists but vlanCktEp is absent for that port/VLAN, deployment did not take effect -> walk the access policy chain.

## Interpret

- No fvRsPathAtt for the port -> EPG was never bound there. Fix: bind it.
- fvRsPathAtt present but wrong encap -> VLAN mismatch.
- Binding present, encap right, but not deployed -> domain mismatch or broken access policy chain, or on-demand immediacy with no endpoint yet.

---

## Check if an EPG is bound to a specific port

Use this when asked "is EPG X bound to [port serial / port name / leaf+port]?"

Follow the checks **in order**. Stop and report to the user as soon as any
check returns a result. Only proceed to the next check if the current one
returns 0 results.

---

### CHECK 1 — by name (serial / port name)

*Skip this check if the user gave an explicit leaf+port (e.g. "leaf 101, eth1/5").*

Binding names that contain the serial appear directly in the tDn pathep (this
covers PC and vPC bundles named after the device serial):

```
GET /api/node/class/fvRsPathAtt.json
    query-target-filter=and(
      wcard(fvRsPathAtt.dn,"epg-EPGNAME"),
      wcard(fvRsPathAtt.tDn,"SERIAL")
    )
```

**If ≥ 1 result → STOP. Report the tDn, encap (VLAN), and mode to the user.**

---

### CHECK 2 — by port location (interface profile resolution)

Resolves the port reference to its actual physical leaf+port tDn(s). This
covers access ports and any binding that was created with an explicit
leaf+port rather than by name.

**If the user gave an explicit leaf+port** (e.g. "leaf 101, eth1/5"):

Build the tDn directly and query:
- Single / access port: `topology/pod-1/paths-101/pathep-[eth1/5]`
- PC bundle: `topology/pod-1/paths-<n>/pathep-[<bundle_name>]`
- vPC bundle: `topology/pod-1/protpaths-<n1>-<n2>/pathep-[<bundle_name>]`

```
GET /api/node/class/fvRsPathAtt.json
    query-target-filter=and(
      wcard(fvRsPathAtt.dn,"epg-EPGNAME"),
      eq(fvRsPathAtt.tDn,"<tdn>")
    )
```

**If the user gave a serial/name**, resolve it to leaf+port tDn(s) with
**aci_resolve_identifier** (Path B): interface profile (`infraAccPortP`) → port
block (`infraPortBlk` = the eth port, e.g. eth1/47) → switch profile
(`infraRsAccPortP`) → node ID(s) (`infraNodeBlk`). Collect every (node × port).

Then build the tDn for each (node × port) and query fvRsPathAtt for each:
   ```
   GET /api/node/class/fvRsPathAtt.json
       query-target-filter=and(
         wcard(fvRsPathAtt.dn,"epg-EPGNAME"),
         eq(fvRsPathAtt.tDn,"topology/pod-1/paths-<node>/pathep-[eth1/X]")
       )
   ```

**If ≥ 1 result for any tDn → STOP. Report the tDn, encap (VLAN), and mode
to the user. List the leaf+port so the user knows which physical port matched.**

---

### CHECK 3 — VMM / dynamic binding

Only reach this check if both CHECK 1 and CHECK 2 returned 0 results.

With VMM integration the EPG has no `fvRsPathAtt`. Instead the EPG is
associated with a VMM domain and ACI deploys its VLAN dynamically when VMs
appear on a host whose uplink is that port. Evidence is in `fvIfConn`.

**Step A — does the EPG have a VMM domain?**

```
GET /api/node/class/fvAEPg.json
    query-target-filter=eq(fvAEPg.name,"EPGNAME")
    &rsp-subtree=children&rsp-subtree-class=fvRsDomAtt
```

Inspect each `fvRsDomAtt.tDn`:
- `uni/vmmp-VMware/dom-<NAME>` → VMware VMM domain → proceed to Step B
- `uni/phys-<NAME>` or `uni/l3dom-<NAME>` → not a VMM domain

**If no VMM domain tDn found → STOP. Report: EPG is not bound to that port
in any way (no static binding, no VMM domain).**

**Step B — is the EPG deployed on that port via VMM?**

VMM `fvIfConn` DNs embed the path inside a `dyatt-[...]` segment, e.g.:
`uni/epp/fv-[uni/tn-T/ap-AP/epg-EPGNAME]/node-104/dyatt-[topology/pod-1/protpaths-103-104/pathep-[CSY_9YZK5B3]]/conndef/conn-[vlan-2082]-[0.0.0.0]`

The pathep name (bundle name or eth port) and the node numbers all appear as
substrings in the DN — use wcard accordingly.

**If the user gave a serial / port name** (the serial appears inside `pathep-[...]`):

```
GET /api/node/class/fvIfConn.json
    query-target-filter=and(
      wcard(fvIfConn.dn,"epg-EPGNAME"),
      wcard(fvIfConn.dn,"SERIAL")
    )
```

**If the user gave an explicit leaf + port** (e.g. "leaf 101, eth1/7"):

```
GET /api/node/class/fvIfConn.json
    query-target-filter=and(
      wcard(fvIfConn.dn,"epg-EPGNAME"),
      wcard(fvIfConn.dn,"node-101"),
      wcard(fvIfConn.dn,"pathep-[eth1/7]")
    )
```

For a vPC where you know both nodes, use `wcard(fvIfConn.dn,"protpaths-N1-N2")` instead of the node filter.

**Interpret the VMM result:**

- **fvIfConn results found**: EPG IS dynamically deployed on that port via VMM.
  VMs from this EPG are running on a host connected there. Report the VMM
  domain name, the number of connections, and the encap VLAN (fvIfConn.encap).
- **No fvIfConn results**: EPG has a VMM domain association but no VMs from
  this EPG are currently on a host connected to that port. With on-demand
  immediacy the VLAN is not programmed until a VM appears. With immediate
  immediacy check vlanCktEp on the node — the VLAN should be programmed even
  without a VM. Report the VMM domain name and this explanation.


