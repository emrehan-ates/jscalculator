# ACI Query Syntax — build correct APIC REST queries: filters, eq vs wcard search, query-target vs rsp-subtree, paging

Mechanical reference for writing an `apic_get` query: endpoints, filter operators, exact vs substring search, subtree scope, paging and escaping. Which class to query → aci_object_map.

## 1. Two kinds of query

- **Class query** — all instances of a class: `/api/node/class/<Class>.json`.
  Response `{"imdata": [...], "totalCount": "<N>"}`. `totalCount` is the
  authoritative count and is always present (never add `query-target=count`).
- **MO query** — one object by DN: `/api/node/mo/<dn>.json`
  (e.g. `/api/node/mo/uni/tn-PROD/BD-Web-BD.json`). DNs with brackets are used
  as-is: `/api/node/mo/uni/infra/vlanns-[POOL]-static.json`.

## 2. Filters: `query-target-filter`

- `eq(fvBD.name,"Web-BD")` exact (case-sensitive) · `ne(...)`
- `gt` / `ge` / `lt` / `le` — numbers and timestamps
  (`gt(faultRecord.created,"2026-07-19T00:00:00")`). Do NOT use them on
  `vlan-N` strings (text comparison).
- `wcard(fvCEp.dn,"node-101")` — SUBSTRING match, case-sensitive.
- `and(a,b,...)`, `or(a,b,...)`, `not(a)` — prefix style, nestable.

**Batch instead of looping:** several values in ONE call:
`or(eq(fvRsPathAtt.tDn,"<p1>"),eq(fvRsPathAtt.tDn,"<p2>"),eq(fvRsPathAtt.tDn,"<p3>"))`.

## 3. Search strategy (exact vs substring)

- Full exact name known → `eq`. 0 results → retry ONCE with `wcard` before
  saying "not found".
- Prefix / fragment ("MSY_ ile başlayanlar", a serial) → go straight to `wcard`.
- Label may be only in the description → `or(eq(<C>.name,"X"),wcard(<C>.descr,"X"))`.
- Type unknown → classify with **aci_naming_conventions**, then try the likely
  classes one by one (a class query cannot span classes).
- Never run the exact same query twice; change class, operator or field.
- A non-MAC name is never an endpoint (`fvCEp` is MAC-only).

## 4. Scope: `query-target` vs `rsp-subtree` (do not confuse)

- `query-target` = which objects the FILTER is evaluated on:
  `self` (default) | `children` | `subtree`; narrow with
  `target-subtree-class=<C1>,<C2>`.
- `rsp-subtree` = what is RETURNED with each matched object:
  `no` (default) | `children` | `full`; narrow with
  `rsp-subtree-class=<C>` and `rsp-subtree-filter=<filter>`.

To READ an object plus what is attached → `rsp-subtree=full` (or `children` +
`rsp-subtree-class`). To FIND objects by something in their subtree →
`query-target=subtree&target-subtree-class=<C>&query-target-filter=...`.
Filtering on child attributes (e.g. keep only `mode=untagged` bindings):
read with `rsp-subtree` and filter the returned children yourself.

## 5. Shaping and paging

- `order-by=<Class>.<prop>|asc|desc` · `page-size=<n>` + `page=<i>` (0-based) ·
  `rsp-prop-include=config-only|naming-only`.
- High-cardinality classes (`faultRecord`, `eventRecord`, `aaaModLR`, `fvCEp`,
  unscoped `faultInst`, `fvIfConn`): always send `page-size` (20–50 sample,
  `1` for count only) and prefer `order-by`; stop after `page=0`, narrow the
  filter instead of paging.
- Config inventories you must list completely (e.g. `fvRsPathAtt` for a set of
  paths): if `totalCount` > returned, fetch the next page.

## 6. Quoting inside `apic_get` (JSON)

Escape inner quotes:
`{"action":"tool_call","tool":"apic_get","arguments":{"path":"/api/node/class/fvCEp.json","params":{"query-target-filter":"eq(fvCEp.mac,\"AA:BB:CC:DD:EE:FF\")"}}}`

## 7. Common mistakes

- `eq(<C>.<prop>,"*")` — `*` is not a wildcard; matches nothing.
- `query-target=count` — unnecessary.
- Filtering `faultRecord` / `aaaModLR` / `eventRecord` on `dn` — use `affected`.
- Filtering policy-group classes by `eth1/X` or a MAC — they have no such
  attribute (use aci_access_policy_chain Path PG).
- Concluding "not found" after only `eq`.
- Unescaped quotes → malformed JSON.

Contracts are out of scope (vzAny permitAny); do not build contract queries.
