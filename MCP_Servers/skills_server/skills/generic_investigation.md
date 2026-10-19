# Generic Investigation — fallback procedure when no specific skill matches

Use this skill when NO more specific skill fits the request. It is a general,
read-only investigation procedure: scope the question, look at faults first,
read the relevant object state, corroborate with counters/health, and report
honestly. It never invents fabric facts or thresholds. If a more specific skill
exists for the task (faults, interfaces, endpoints, EPG deployment, access
policy, inventory, object inspection, connectivity, switch-name resolution,
change audit, VMM), prefer that skill instead.

## Follow these steps in order

### 1. Scope the question first

- Extract the concrete targets named by the user: tenant / application profile /
  EPG, or node / interface, or a device name / serial, or a MAC address.
- If the request is ambiguous (no object type, no name, no clear symptom), ask
  ONE short clarifying question instead of querying broadly. Do NOT spend
  iterations guessing across many classes. Example: "Which object do you mean —
  a specific EPG, a leaf/port, or an endpoint MAC?"
- If the user gave a switch by hostname (letters + hyphens, not pure digits),
  resolve it to a numeric node ID first (see aci_switch_name_resolution) before
  using it in any query.
- If the user asks for the **policy group of a port** (and you already know or
  can resolve leaf+eth), stop using this generic path — read
  aci_access_policy_chain **Path PG** instead. Never wcard policy-group classes
  for an eth port or MAC string.

### 2. Faults first

Before anything else, check for faults scoped to the identified object(s). A
fault often answers the question directly.

```
/api/node/class/faultInst.json?query-target-filter=wcard(faultInst.dn,"<object dn fragment>")
```

- Scope to the node + port, tenant/EPG, or the resolved DN fragment — never to a
  raw MAC or a CEp DN (faults are not raised on endpoints).
- If the user asks about a time window or history, also read `faultRecord`
  filtered on `affected` and `created` (see aci_fault_investigation).

### 3. Read object state

Query the object class(es) suggested in the injected "ACI object-model classes
likely relevant..." hint block at the top of your context — use those EXACT
class names.

```
/api/node/class/<CLASS>.json?query-target-filter=eq(<CLASS>.name,"<NAME>")
```

- If a class you need is NOT in the hints, call `aci_lookup_class` with the
  class name or a plain-English description to get its schema, DN format, and
  filter targets. Do NOT guess a class name.
- If an exact-name query returns 0 results, retry with `wcard` (substring)
  before concluding the object is missing.
- To see what is attached to a named object, read its subtree
  (`rsp-subtree=full`, optionally `rsp-subtree-class=<CHILD>`).

### 4. Corroborate

Confirm findings with independent signals for the objects found:

- Operational state (admin vs oper, e.g. `l1PhysIf.adminSt` /
  `ethpmPhysIf.operSt` / `operStQual`).
- Health scores (`healthInst`) on the object where available.
- Counters (RMON CRC/FCS/error/discard counters on physical ports) when a
  physical-layer problem is plausible.

Report the raw values. Do NOT invent a pass/fail threshold — unless an existing
skill defines one — let the user judge the numbers.

### 5. Synthesize honestly

Structure the answer:

- **Findings**: what you determined.
- **Evidence**: which query showed what (name the class/filter and the key
  attribute values). Cite actual data, never assumptions.
- **Not found / not checkable**: state clearly what you searched for and could
  not find, and anything you could not verify with the available tools.
- **Suggested next step**: one concrete manual step the user could take.

Never fabricate DNs, node IDs, VLANs, or values. If the object was not found,
say so plainly and say what you searched.

### 6. Budget awareness

If you see an iteration-budget warning, or several lookups in a row return
nothing, STOP querying and synthesize immediately with what you have — an honest
partial answer that states what remains unknown is better than looping to the
cap.

**Large-result stop (same priority as empty fail-fast):** if a class query
returns a `totalCount` much larger than the sample you received (or the tool
result says the body was truncated / "LARGE RESULT"), do **not** open more pages
to dump the rest. Summarize from `totalCount` + the sample you have, optionally
one narrower filtered query (`severity`, node, shorter window, `page-size` ≤ 50,
`page=0` only), then `final_answer`. Prefer few essential calls over exhaustive
APIC crawls — this applies to every investigation, not only faults.

## Fabric policy note

Contracts are out of scope in this environment (vzAny permitAny; filtering is on
the firewall). Do not analyze contracts, subjects, or filters. Users may ask in
Turkish or English — answer in the language the user used.
