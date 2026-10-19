# ACI Subtree Analysis — use rsp-subtree and filter the returned tree

Use this skill when the user asks for objects that must be found by inspecting related data in the APIC response tree, not just by a top-level attribute. This includes questions like:

- "Show BDs where arpFlood is off"
- "List static bindings on an EPG and keep only trunk bindings"
- "Find objects where a child relation exists or is missing"
- "Read the object plus its children, then filter on child attributes"

## Core rule

Use `rsp-subtree` to read the object tree, then filter the returned objects by the attribute or child class the user asked for. Do not answer from a top-level list alone if the answer depends on nested data.

## When to use which read pattern

- If the condition is on the object itself, read the class or MO and filter the returned objects by that attribute.
- If the condition is in child MOs, read the parent with `rsp-subtree=full` and inspect the children.
- If you only need the direct children, `rsp-subtree=children` is enough.
- If you need nested relations, use `rsp-subtree=full` so you do not miss deeper children.

## Examples

### Bridge domains with `arpFlood=no`

Read the BD class and filter the returned bridge domains whose attributes include `arpFlood=no`.

- Query pattern: `/api/node/class/fvBD.json`
- Keep only BDs where the BD object itself has `arpFlood` equal to `no`.

If the user wants only a tenant, first narrow the scope to that tenant, then apply the same filter.

### Static bindings on an EPG filtered by mode

Read the EPG subtree and inspect its `fvRsPathAtt` children.

- Read pattern: `/api/node/mo/uni/tn-T/ap-AP/epg-EPG.json?rsp-subtree=full`
- Keep only `fvRsPathAtt` children whose `mode` matches the requested value:
  - `regular` -> trunk or tagged bindings
  - `untagged` -> access bindings
  - `native` -> native trunk bindings

If the user asks for only one mode, report only those bindings and ignore the rest.

### Missing or unexpected child relations

Use the subtree to confirm whether the child relation exists at all. Examples:

- EPG -> `fvRsBd`
- EPG -> `fvRsDomAtt`
- policy group -> `infraRsAttEntP`
- domain -> `infraRsVlanNs`

If the relation is missing, report that specific gap instead of guessing the object is misconfigured.

## Good behavior

- Filter the response tree by the exact class and attribute the user asked for.
- Do not merge unrelated objects into the answer.
- Do not guess a child relation from naming alone if the subtree can confirm it.
- If a query returns many objects, keep the response scoped to the requested filter instead of dumping the whole tree.

## Relationship to other skills

- Use aci_object_map first to choose the correct class.
- Use aci_object_inspection when the user names one object and wants its properties or attached children.
- Use aci_search_techniques to locate the right object before reading its subtree.
- Use aci_epg_deployment, aci_access_policy_chain, and aci_fault_investigation after subtree inspection when you need root cause analysis.

## Fabric policy note

Contracts are out of scope in this environment. The fabric uses vzAny permitAny and filtering happens on the firewall, so do not analyze contracts, subjects, or filters while doing subtree inspection.