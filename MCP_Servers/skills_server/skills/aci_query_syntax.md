# ACI Query Syntax — how to build a correct APIC REST query (filters, scope, response shaping)

Use this skill as a mechanical reference whenever you are unsure how to write an
APIC `apic_get` query: which endpoint to hit, how to filter, how to scope the
tree, how to shape or page the response, and which common syntax mistakes to
avoid. Read it before building any non-trivial (nested / range / subtree) query.
It is pure syntax — for WHICH class to query, use aci_object_map; for search
STRATEGY (exact vs substring), use aci_search_techniques.

## 1. Two kinds of query

- **Class query** — every instance of a class fabric-wide:
  `/api/node/class/<Class>.json`
  The response is `{"imdata": [...], "totalCount": "<N>"}`. `totalCount` is the
  authoritative object count and is ALWAYS present — never add
  `query-target=count`, and for "how many" just read `totalCount`
  (see aci_inventory_validation).
- **MO query** — one specific object by its DN, plus optionally its children:
  `/api/node/mo/<dn>.json`
  Example: `/api/node/mo/uni/tn-PROD/BD-Web-BD.json`

Path may be given with or without the leading `/api/node/...` — `fvBD.json`,
`/api/node/class/fvBD.json`, and `/api/node/mo/<dn>.json` are all accepted.

## 2. Filtering: `query-target-filter`

Filters reference a property as `<Class>.<prop>` and compare it to a value.

Comparison operators:
- `eq(fvBD.name,"Web-BD")` — exact match (case-sensitive).
- `ne(...)` — not equal.
- `gt` / `ge` / `lt` / `le` — greater/less than (or equal). Use for timestamps
  and numbers, e.g. `gt(faultRecord.created,"2026-07-19T00:00:00")`.
- `bw(fvnsEncapBlk.from,"vlan-100","vlan-200")` — inclusive range (between).
- `wcard(fvCEp.dn,"node-101")` — SUBSTRING match (value appears anywhere).
  Case-sensitive. Use for partial names, DN fragments, prefixes.

Logical combiners (they WRAP the comparisons, prefix-style):
- `and(a,b,c)` — all must hold.
- `or(a,b,c)` — any may hold.
- `not(a)` — negate.

Nesting example (fault records in a window, affecting a node + port):
```
and(gt(faultRecord.created,"2026-07-27T00:00:00"),wcard(faultRecord.affected,"node-101"),wcard(faultRecord.affected,"eth1/5"))
```

## 3. Scope: `query-target` vs `rsp-subtree` (do NOT confuse these)

These control TWO DIFFERENT things:

- `query-target` = WHICH objects the filter is evaluated against:
  - `self` (default) — the object(s) the path points at.
  - `children` — direct children.
  - `subtree` — the whole subtree.
  With `subtree`/`children`, narrow the class with
  `target-subtree-class=<Class1>,<Class2>` and filter matched objects with
  `query-target-filter`.

- `rsp-subtree` = WHAT is RETURNED in the response for each matched object:
  - `no` (default) — the object only.
  - `children` — object + direct children.
  - `full` — object + entire subtree.
  Narrow returned children with `rsp-subtree-class=<Class>`, and filter them with
  `rsp-subtree-filter=<filter>`.

Rule of thumb: to READ an object plus what is attached to it, use
`rsp-subtree=full` (or `children` + `rsp-subtree-class`). To MATCH objects by
something in their subtree, use `query-target=subtree` +
`target-subtree-class` + `query-target-filter`.

## 4. Shaping the response

- `order-by=<Class>.<prop>|asc|desc` — sort, e.g.
  `order-by=faultRecord.created|desc` (newest first).
- `page-size=<n>` and `page=<i>` — paginate large result sets (page is
  0-indexed). `totalCount` still reports the full count.
- `rsp-prop-include=all | config-only | naming-only` — trim which properties
  come back (`config-only` is handy for audits).

## 4b. When result sets are large (REQUIRED discipline)

High-cardinality classes (`faultRecord`, `eventRecord`, `aaaModLR`, `fvCEp`,
unscoped `faultInst`, and similar logs) can return tens of thousands of objects.
Never rely on the APIC default dump.

Rules:
- Always send an explicit `page-size` for these classes. Use **20–50** for a
  diagnostic sample; use **`page-size=1`** when you only need `totalCount`.
- Prefer `order-by` so the sample is useful (newest first, or severity desc).
- Read `totalCount` from the response header — that is the full population size.
- **Stop after `page=0`** unless the user explicitly asks to list more.
- **Never** loop `page=1,2,3…` to reconstruct a full dump. That burns tokens and
  time without improving the answer.
- Prefer a **narrower filter** (severity, node, port, shorter time window) over
  paging when you need more detail.
- Answer pattern: state `totalCount`, summarize patterns from the sample, offer
  a scoped follow-up if needed.

Example (newest history sample, not a full crawl):
```
/api/node/class/faultRecord.json
  ?query-target-filter=gt(faultRecord.created,"<window-start>")
  &order-by=faultRecord.created|desc
  &page-size=25
  &page=0
```

## 5. Quoting inside `apic_get` arguments (JSON)

The filter value's double quotes must be escaped because the whole call is JSON.
Inside a JSON string, write `\"` for every literal quote:
```
{"action":"tool_call","tool":"apic_get","arguments":{"path":"/api/node/class/fvCEp.json","params":{"query-target-filter":"eq(fvCEp.mac,\"AA:BB:CC:DD:EE:FF\")"}}}
```

## 6. Common mistakes (do NOT do these)

- `query-target=count` — unnecessary; `totalCount` is already in every class
  response.
- `eq(<Class>.<prop>,"*")` — `"*"` is NOT a wildcard literal; `eq` does exact
  match, so this matches NOTHING. For "any/substring" use `wcard`, or omit the
  filter entirely for a fabric-wide list/count.
- Mixing up `query-target` and `rsp-subtree` (section 3) — the classic reason a
  query returns the wrong shape or an empty tree.
- Concluding "not found" after only an `eq` query — retry with `wcard`
  (substring) before giving up (see aci_search_techniques).
- Filtering `faultRecord` / `aaaModLR` / `eventRecord` on `dn` — these logs are
  filtered on the `affected` attribute, not `dn`.
- Unescaped quotes in the filter value — causes a malformed-JSON re-emit.

## Relationship to other skills

- aci_object_map — pick the correct class/DN shape first.
- aci_search_techniques — eq vs wcard strategy for locating objects.
- aci_subtree_analysis — worked examples of rsp-subtree filtering.
- aci_inventory_validation — counts via totalCount.

## Fabric policy note

Contracts are out of scope in this environment (vzAny permitAny; filtering is on
the firewall). Do not build queries to analyze contracts, subjects, or filters.
Users may ask in Turkish or English — answer in the language the user used.
