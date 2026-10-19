# ACI Inventory Validation — count objects without guessing

Use this skill when the user asks "how many", "how much", "list all", or otherwise wants a count of ACI objects such as tenants, BDs, APs, EPGs, interfaces, domains, or faults.

## Core rule

Never estimate or reuse a previously seen number. Always query APIC for the exact scope the user asked for, then report the APIC count.
**Do NOT add a query filter as count or subtree**, just call for the class and totalCount will be a block in the response json. 

## How to answer counts safely

- If the user asks for a fabric-wide count, query the class directly with no tenant/parent filter unless the user explicitly narrows the scope.
- If the user asks for a tenant-scoped count, query under that tenant DN or use an exact tenant filter.
- If the user asks for a specific object type, map it first with aci_object_map, then query that class directly.
- Use the APIC response totalCount as the authoritative count.
- Do not invent a number from memory, from a similar fabric, or from a partial list.
- If a count looks unexpectedly high, verify that the query scope is correct before answering.
- For high-cardinality classes (`faultRecord`, `eventRecord`, `aaaModLR`, `fvCEp`,
  unscoped `faultInst`), still trust `totalCount`, but request a tiny body so APIC
  does not materialize a huge `imdata`: use `page-size=1` (count-only) or
  `page-size=20–50` with `order-by` when a sample is useful. Never page through
  the full set just to answer "how many".
- If the count is legitimately huge, report the count (and optional severity /
  scope breakdown via separate filtered count calls). Do not list thousands of
  rows.

## Examples

- "How many bridge domains are there?" -> query /api/node/class/fvBD.json and report totalCount.
- "How many bridge domains are in tenant PROD?" -> query only PROD's subtree or filter the tenant scope first, then report the count.
- "How many endpoints are learned on leaf 101?" -> query fvCEp with the relevant fabricPathDn or leaf scope, then report totalCount.
- "How many fault records in the last 48h?" -> faultRecord with time filter +
  `page-size=1` (or a small sample + order-by); answer with totalCount, not a dump.

## Good response pattern

State the scope and the count together, for example:

- "Fabric-wide bridge domains: 9"
- "Tenant PROD bridge domains: 4"

## What not to do

- Do not answer with a number unless it came from APIC.
- Do not mix counts from different scopes.
- Do not count objects from a memory of earlier results.
- Do not treat a partial list as a full inventory unless the APIC response totalCount confirms it.

## Relationship to other skills

- Use aci_object_map to choose the right class.
- Use aci_search_techniques when you need to locate an object by name before counting its children.
- Use aci_fault_investigation, aci_interface_troubleshooting, aci_endpoint_troubleshooting, aci_epg_deployment, and aci_access_policy_chain for root-cause analysis after the count or inventory check.

## ACI policy note for this environment

Contracts are out of scope here. The fabric uses vzAny permitAny and filtering happens on the firewall. For count or inventory questions, stay focused on APIC object scope and do not analyze contracts, subjects, or filters.