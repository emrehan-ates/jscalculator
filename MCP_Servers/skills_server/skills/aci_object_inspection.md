# ACI Object Inspection — answer questions about ONE specific named object

Use this skill when the user names a specific object and asks about IT: its
properties, its configuration, or what is attached to it. Examples that fail
without this skill:

- "what are the properties of ai-test-2 EPG"
- "what are the static bindings of VLAN-109 EPG"
- "show me the config of the Web-BD bridge domain"
- "what domains is the SG_TEST EPG associated with"
- "which ports are in the MSY_SWProf interface profile"
- "what application profiles are under tenant PROD"

## The two-step pattern (always)

1. LOCATE the object: map the stated type to its class with aci_object_map,
   then query that class by name. The class query already returns the object's
   OWN attributes — that alone answers "what are the properties of X".

   /api/node/class/<CLASS>.json?query-target-filter=eq(<CLASS>.name,"<NAME>")

   If 0 results, retry with wcard (substring) before concluding it is missing
   (see aci_search_techniques). Endpoints (fvCEp) are matched by MAC only.

2. ENRICH with children when the question is about what is ATTACHED to the
   object (static bindings, domains, subnets, ports, BD/VRF relations). Add the
   subtree to the SAME class query, or read the MO by its DN:

   /api/node/class/<CLASS>.json?query-target-filter=eq(<CLASS>.name,"<NAME>")&rsp-subtree=full
   /api/node/mo/<DN>.json?rsp-subtree=full

   To return only the child class you care about, scope it:
   rsp-subtree=children&rsp-subtree-class=<CHILD_CLASS>

Then filter/report the returned attributes or children (see aci_subtree_analysis).

## What answers each question, by object type

### EPG (fvAEPg) — DN: uni/tn-T/ap-AP/epg-NAME
- "properties of <EPG>": query fvAEPg by name; report its attributes
  (name, pcEnfPref, prefGrMemb, prio, descr, dn -> tenant/AP).
- "static bindings of <EPG>": read the EPG subtree; list the fvRsPathAtt
  children. Each has tDn (the path: paths-<node>/pathep-[<port>] or protpaths-
  for vPC), encap (vlan-XXX), and mode (regular=trunk, untagged=access,
  native). Filter by mode if asked.
- "what BD is <EPG> in": fvRsBd.tnFvBDName child.
- "what domains is <EPG> in": fvRsDomAtt children (tDn = the domain).

### Bridge Domain (fvBD) — DN: uni/tn-T/BD-NAME
- "properties of <BD>": query fvBD by name; report arpFlood, unicastRoute,
  unkMacUcastAct, multiDstPktAct, mac, descr.
- "subnets/gateways of <BD>": fvSubnet children (ip).
- "what VRF is <BD> in": fvRsCtx.tnFvCtxName child.
- "L3Outs of <BD>": fvRsBDToOut children.

### Application Profile (fvAP) — DN: uni/tn-T/ap-NAME
- "EPGs under <AP>": read the AP subtree; list fvAEPg children.

### Tenant (fvTenant) — DN: uni/tn-NAME
- "BDs / APs / VRFs under <tenant>": read the tenant subtree scoped to the
  child class (rsp-subtree-class=fvBD, fvAP, or fvCtx), or query that child
  class filtered to the tenant DN.

### Leaf interface profile (infraAccPortP) — DN: uni/infra/accportprof-NAME
- "ports in <interface profile>": read its subtree; the infraHPortS port
  selectors contain infraPortBlk (fromPort/toPort = the eth ports) and
  infraRsAccBaseGrp (the policy group).
- "policy group of <interface profile>": same subtree read; report
  infraRsAccBaseGrp.tDn (and the name segment after accportgrp-/accbundle-).
- "which nodes/switches use it": node IDs are NOT in the interface profile.
  Reverse-lookup the switch profile that binds it (infraRsAccPortP.tDn) — see
  aci_access_policy_chain.

### Physical leaf port (already known node + ethX/Y) — NOT a single MO name
- "what is the policy group of this port" / "bu portun policy group'u ne" /
  "which interface policy is on leaf N eth1/Y":
  There is NO class filter `eq(....name,"eth1/Y")` for policy groups.
  Use **aci_access_policy_chain Path PG** (node → switch profile → interface
  profiles → matching infraPortBlk → infraRsAccBaseGrp.tDn).
- If conversation history already has fabricPathDn with a non-eth bundle name
  (`pathep-[BUNDLE]`), that BUNDLE is the infraAccBndlGrp name — query it
  directly; do not restart a blind class search.

### Switch profile (infraNodeP) — DN: uni/infra/nprof-NAME
- "nodes in <switch profile>": infraNodeBlk children (from_/to_).
- "interface profiles bound": infraRsAccPortP children.

### Policy group (infraAccPortGrp / infraAccBndlGrp)
- "properties / AEP of <policy group NAME>": query by name on
  infraAccPortGrp and/or infraAccBndlGrp (try both if type unknown), then
  infraRsAttEntP child. Then AEP -> domain -> VLAN pool if asked
  (aci_access_policy_chain).
- If the user did NOT give a policy-group name — only a port, MAC, or
  serial — do NOT wcard policy-group classes for the port string; resolve
  the port first (aci_resolve_identifier) then Path PG.

## Worked example 1 — properties of an EPG

User: "what are the properties of ai-test-2 EPG"
1. fvAEPg is the EPG class (aci_object_map).
2. /api/node/class/fvAEPg.json?query-target-filter=eq(fvAEPg.name,"ai-test-2")
3. Report the returned attributes. If 0 results, retry with
   wcard(fvAEPg.name,"ai-test-2").

## Worked example 2 — static bindings of an EPG

User: "what are the static bindings of VLAN-109 EPG"
1. fvAEPg is the EPG class.
2. /api/node/class/fvAEPg.json?query-target-filter=eq(fvAEPg.name,"VLAN-109")&rsp-subtree=children&rsp-subtree-class=fvRsPathAtt
   (or read the EPG DN with rsp-subtree=full and pick the fvRsPathAtt children).
3. For each fvRsPathAtt report tDn (leaf/port or vPC), encap (VLAN), and mode
   (regular/untagged/native). If the user asks for one mode, return only those.

## Good behavior

- Map the type first; query that class directly. Do NOT probe unrelated classes.
- For a properties question, the class query result is usually enough — do not
  over-fetch.
- For an "attached to" question, always pull the subtree; do not answer from
  the bare object.
- Report the ACTUAL attribute/child values, not just the DN.
- If exact match returns nothing, widen with wcard before saying "not found".

## Fabric policy note

Contracts are out of scope here (vzAny permitAny; firewall does filtering). Do
not analyze contracts, subjects, or filters when inspecting an object.
