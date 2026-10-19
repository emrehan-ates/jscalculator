# ACI Search Techniques — find an object by full name, partial name, or description

Use this skill when you need to LOCATE an object but only have a name, a partial name (like a prefix), or a description — especially when an exact-match search returns nothing.

## Exact vs substring — choose deliberately

ACI filters are case-sensitive and exact by default. Two operators matter:

- eq(<class>.name,"VALUE") : exact match. Use when you have the full exact name.
- wcard(<class>.name,"VALUE") : substring match (VALUE appears anywhere). Use for partial names, prefixes, or when exact match returned nothing.

Examples:
- Full name known: /api/node/class/fvBD.json?query-target-filter=eq(fvBD.name,"XXX")
- Prefix / partial (user gives "MSY_" and wants all matching port policy groups):
  /api/node/class/infraAccBndlGrp.json?query-target-filter=wcard(infraAccBndlGrp.name,"MSY_")
  (and infraAccPortGrp the same way)
- Match name OR description (the label may be only in the description):
  or(eq(<class>.name,"SG_TEST_FW"),wcard(<class>.descr,"SG_TEST_FW"))

## Rules of thumb

- Start with eq on the exact name. If 0 results, retry with wcard (substring) BEFORE concluding it does not exist.
- If the user gives a prefix or says "the ones starting with / containing X", go straight to wcard — do NOT use eq.
- A non-MAC name is never an endpoint (fvCEp). Endpoints are matched only by MAC.
- If you do not know the type, get the candidate class from aci_object_map, then search the most likely class(es) by name AND description.
- Never run the exact same query twice. If a query returns nothing, change the class, the operator (eq -> wcard), or the field (name -> descr) before retrying.

## Worked examples (this environment)

1. "where is the test firewall" / "find SG_TEST_FW":
   Usually an EPG named SG_TEST_FW. Query fvAEPg by name OR descr:
   /api/node/class/fvAEPg.json?query-target-filter=or(eq(fvAEPg.name,"SG_TEST_FW"),wcard(fvAEPg.descr,"SG_TEST_FW"))
   If nothing, widen with wcard on the name (wcard(fvAEPg.name,"SG_TEST_FW")), then try other likely types (fvBD, fvAP) from aci_object_map.

2. "find the MSY_ ports" / "policy groups containing MSY_":
   Interface policy groups, substring search both kinds:
   /api/node/class/infraAccBndlGrp.json?query-target-filter=wcard(infraAccBndlGrp.name,"MSY_")
   /api/node/class/infraAccPortGrp.json?query-target-filter=wcard(infraAccPortGrp.name,"MSY_")
   The suffix after the prefix is typically the device serial provided by another team.

## Locate, then enrich (do not stop at the DN)

The DN alone is rarely the answer. Once you know the type and DN, fetch the real config:
- Read with children: /api/node/mo/<DN>.json?rsp-subtree=full
- For an interface profile, node IDs are NOT in it — they come from the switch profile that references it (reverse lookup on infraRsAccPortP.tDn). See aci_access_policy_chain.
- For a BD: subnet, VRF, L3Out relations. For an EPG: static bindings and domain associations.
Always report the actual configuration, not just the DN.



