# Switch Name Resolution

## Purpose

Resolve a switch hostname fragment (e.g. "DR1-C6-8", "DR2-B05-10") to its
numeric node ID and pod, using the APIC `fabricNode` class.

## When to Use

- The user refers to a switch by name instead of a numeric node ID.
- You need a numeric node ID for a DN like `topology/pod-1/node-119` but the
  user gave something like "DR1-C6-8".
- Any time a non-numeric string appears where a leaf/spine ID is expected.

## Procedure

### Step 1 — Query fabricNode with wcard

Use a **wcard** filter on `fabricNode.name` with the user's fragment:

```
GET /api/node/class/fabricNode.json?query-target-filter=wcard(fabricNode.name,"DR1-C6-8")
```

### Step 2 — Check results

- **Exactly 1 match**: Extract `id` (numeric node ID) and `dn` (contains pod).
  Proceed with subsequent queries using the resolved ID.
- **Multiple matches**: The fragment is ambiguous. Either:
  - Pick the best match if one result's name contains the fragment as a suffix
    (e.g. "ESY-B-DR1-C6-8" for fragment "DR1-C6-8").
  - Ask the user to be more specific.
- **0 matches**: Try zero-padding variants (Step 3).

### Step 3 — Zero-padding retry (do it in ONE query)

Some sites zero-pad numeric segments to 2 digits (e.g. "C06" instead of "C6",
"B05" instead of "B5"). Instead of one GET per padding guess, OR **all** the
variants into a **single** wcard query so one round-trip covers every form:

```
GET /api/node/class/fabricNode.json?query-target-filter=or(wcard(fabricNode.name,"DR1-C6-8"),or(wcard(fabricNode.name,"DR1-C06-8"),or(wcard(fabricNode.name,"DR1-C6-08"),wcard(fabricNode.name,"DR1-C06-08"))))
```

| Original   | Variants to OR together             |
|------------|-------------------------------------|
| DR1-C6-8   | DR1-C6-8, DR1-C06-8, DR1-C6-08, DR1-C06-08 |
| DR2-B05-10 | DR2-B05-10, DR2-B5-10               |

### Site conventions (canonical forms)

If you know which site the fabric belongs to, normalize to the canonical form
first — it reduces the number of variants needed:

- **EVM sites** — `<LEFT>-C<nn>-<nn>` (e.g. `DR2-C05-08`). The middle numeric
  part is zero-padded to 2 digits; the right part is zero-padded to 2 digits
  only when it is a single digit (`8` -> `08`, `18` stays `18`). The left
  segment (e.g. `DR2`) is used as given.
- **KKB sites** — `A1-<nnn>-<nn>` (e.g. `A1-511-20`). The left segment is always
  `A1`. The middle is 3 digits; a user may type it combined (`511`) or split
  (`7 11` -> `711`, `05 01` -> `501`). The right part is zero-padded to 2 digits
  when single-digit (`8` -> `08`).

### Step 4 — Extract node ID

From the matching `fabricNode` attributes:

| Attribute | Example value                | Use                          |
|-----------|------------------------------|------------------------------|
| `name`    | ESY-B-DR2-B05-10-119         | Full hostname (for display)  |
| `id`      | 119                          | Numeric node ID (for DNs)    |
| `dn`      | topology/pod-1/node-119      | Contains pod ID (1)          |
| `role`    | leaf                         | Switch role                  |

Use the `id` attribute as the numeric node ID in subsequent queries.

## Examples

### Resolve "DR1-C6-8"

1. Query ONE OR'd wcard: `or(wcard(...,"DR1-C6-8"),or(wcard(...,"DR1-C06-8"),or(wcard(...,"DR1-C6-08"),wcard(...,"DR1-C06-08"))))` → 1 result
2. Result: `name="ESY-B-DR1-C06-8-101", id="101", dn="topology/pod-1/node-101"`
3. Use node ID **101** in subsequent queries.

### Resolve "DR2-B05-10"

1. Query: `wcard(fabricNode.name,"DR2-B05-10")` → 1 result
2. Result: `name="ESY-B-DR2-B05-10-119", id="119", dn="topology/pod-1/node-119"`
3. Use node ID **119**.

## Common Pitfalls

- **Don't assume node IDs**: The last segment of the hostname is usually the
  node ID, but always confirm via the `id` attribute — don't parse it from the
  name.
- **Zero-padding varies by site**: ESY sites use 2-digit padding (A06, B05);
  KKB sites typically don't. Always try both if the first query fails.
- **wcard is substring match**: "DR1-C6" matches any name containing that
  substring. If you get too many results, add more segments for specificity.
