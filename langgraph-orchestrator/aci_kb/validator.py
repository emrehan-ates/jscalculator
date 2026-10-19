"""
validate_payload(class_name, payload) — schema-check an ACI MO payload
before it hits APIC.

Catches:
    - unknown properties (typos)
    - missing mandatory properties
    - invalid enum values
    - regex/range violations
    - type mismatches (where determinable)
    - read-only properties being set
    - create-only properties being modified (when is_update=True)

Does NOT validate:
    - DN correctness (use build_dn for that)
    - parent containment (the LLM is expected to put the MO under a valid parent)
    - nested children — call validate_payload separately for each child class
"""
import re
from typing import Any, Optional
from . import store


def validate_payload(
    class_name: str,
    payload: dict,
    is_update: bool = False,
) -> dict:
    """
    Validate an ACI MO payload against its schema.

    Args:
        class_name: 'fvBD' or 'fv:BD'.
        payload: the dict you would pass as the MO body. Two accepted shapes:
            1) APIC-native: {"fvBD": {"attributes": {"name": "Web-BD", ...}}}
            2) Flat attributes: {"name": "Web-BD", "arpFlood": "yes", ...}
        is_update: True if this is a PUT/POST modifying an existing MO.
            Triggers create_only checks.

    Returns:
        {
            "valid": bool,
            "class_name": "fvBD",
            "errors": [...],
            "warnings": [...],
            "normalized_attributes": {...} # attributes in flat form
        }
    """
    canonical = store.resolve_class_name(class_name)
    if not canonical:
        return {
            "valid": False,
            "class_name": class_name,
            "errors": [{"code": "unknown_class",
                        "message": f"No such class '{class_name}'"}],
            "warnings": [],
            "normalized_attributes": {},
        }

    attrs = _extract_attributes(payload, canonical)

    cls = store.get_class(canonical)
    if cls["is_abstract"]:
        return {
            "valid": False,
            "class_name": canonical,
            "errors": [{"code": "abstract_class",
                        "message": f"{canonical} is abstract; cannot instantiate"}],
            "warnings": [],
            "normalized_attributes": attrs,
        }

    props_by_name = {p["prop_name"]: p for p in store.get_properties(canonical)}

    errors: list[dict] = []
    warnings: list[dict] = []

    # 1. Unknown properties (typos)
    for name in attrs:
        if name not in props_by_name:
            # Suggest near-matches by simple prefix/casefold check
            suggestion = _suggest_property(name, props_by_name.keys())
            err = {
                "code": "unknown_property",
                "property": name,
                "message": f"'{name}' is not a property of {canonical}",
            }
            if suggestion:
                err["did_you_mean"] = suggestion
            errors.append(err)

    # 2. Missing mandatory properties (only on create)
    if not is_update:
        for pname, p in props_by_name.items():
            if (p["mandatory"] or p["is_naming"]) and pname not in attrs:
                # Naming properties must always be in the payload on create
                # (they're how APIC builds the DN), and mandatory props are
                # required by definition.
                errors.append({
                    "code": "missing_required",
                    "property": pname,
                    "message": (f"'{pname}' is required when creating "
                                f"{canonical}"),
                    "is_naming": bool(p["is_naming"]),
                })

    # 3. Per-property value checks
    for pname, raw_value in attrs.items():
        p = props_by_name.get(pname)
        if p is None:
            continue # already reported as unknown

        if p["read_only"]:
            errors.append({
                "code": "read_only_property",
                "property": pname,
                "message": f"'{pname}' is read-only and cannot be set",
            })
            continue

        if is_update and p["create_only"]:
            errors.append({
                "code": "create_only_property",
                "property": pname,
                "message": (f"'{pname}' is create-only; cannot be modified "
                            f"after the MO is created"),
            })
            continue

        value = _stringify(raw_value)

        # Enum check
        valid_values = [
            v["value"] for v in p["valid_values"]
            if v.get("localName") != "defaultValue"
        ]
        if valid_values and value not in valid_values:
            # ACI enums often have aliases ('yes'/'no' vs 'true'/'false');
            # check localName too before erroring.
            local_names = [
                v.get("localName") for v in p["valid_values"]
                if v.get("localName") and v.get("localName") != "defaultValue"
            ]
            if value not in local_names:
                errors.append({
                    "code": "invalid_enum_value",
                    "property": pname,
                    "value": value,
                    "valid_values": valid_values,
                    "message": (f"'{value}' is not valid for '{pname}'. "
                                f"Allowed: {valid_values}"),
                })
                continue

        # Validator checks (regex, range, length)
        for v in p["validators"]:
            v_errs = _check_validator(pname, value, v, p["base_type"])
            errors.extend(v_errs)

    return {
        "valid": len(errors) == 0,
        "class_name": canonical,
        "errors": errors,
        "warnings": warnings,
        "normalized_attributes": attrs,
    }


# ---------- helpers ----------

def _extract_attributes(payload: dict, canonical: str) -> dict:
    """
    Accept either:
        {"fvBD": {"attributes": {...}, "children": [...]}} APIC-native
        {"fv:BD": {"attributes": {...}}} colon-form variant
        {"name": "...", "arpFlood": "..."} flat attrs

    Return the flat attributes dict.
    """
    if not isinstance(payload, dict) or not payload:
        return {}

    cls = store.get_class(canonical)
    keys = {canonical, cls["class_colon_name"]}

    # Native APIC shape
    for k in list(payload.keys()):
        if k in keys and isinstance(payload[k], dict):
            inner = payload[k]
            if "attributes" in inner and isinstance(inner["attributes"], dict):
                return dict(inner["attributes"])
            # Sometimes the LLM omits the "attributes" wrapper
            return {kk: vv for kk, vv in inner.items() if kk != "children"}

    # Flat shape — assume top-level is attributes
    # (best-effort; if the keys aren't valid properties we'll catch them below)
    return dict(payload)


def _stringify(v: Any) -> str:
    """ACI sends/expects most attribute values as strings."""
    if isinstance(v, bool):
        return "yes" if v else "no" # ACI's preferred bool form
    if v is None:
        return ""
    return str(v)


def _check_validator(
    prop_name: str,
    value: str,
    validator: dict,
    base_type: str,
) -> list[dict]:
    errs: list[dict] = []

    # Numeric range — only check if base_type indicates a number
    is_numeric = base_type and ("scalar:" in base_type) and (
    "Bool" not in base_type
    )
    vmin = validator.get("min")
    vmax = validator.get("max")

    if is_numeric and (vmin is not None or vmax is not None):
        try:
            num = float(value)
            if vmin is not None and num < float(vmin):
                errs.append({
                    "code": "value_below_min",
                    "property": prop_name,
                    "value": value,
                    "min": vmin,
                    "message": f"'{prop_name}'={value} is below minimum {vmin}",
                })
            if vmax is not None and num > float(vmax):
                errs.append({
                    "code": "value_above_max",
                    "property": prop_name,
                    "value": value,
                    "max": vmax,
                    "message": f"'{prop_name}'={value} exceeds maximum {vmax}",
                })
        except (ValueError, TypeError):
            errs.append({
                "code": "non_numeric_value",
                "property": prop_name,
                "value": value,
                "message": f"'{prop_name}' expects a number, got '{value}'",
            })
    elif vmin is not None or vmax is not None:
        # String-length range
        n = len(value)
        if vmin is not None and n < int(vmin):
            errs.append({
                "code": "length_below_min",
                "property": prop_name,
                "length": n,
                "min": vmin,
                "message": f"'{prop_name}' length {n} is below minimum {vmin}",
            })
        if vmax is not None and n > int(vmax):
            errs.append({
                "code": "length_above_max",
                "property": prop_name,
                "length": n,
                "max": vmax,
                "message": f"'{prop_name}' length {n} exceeds maximum {vmax}",
            })

    # Regex constraints
    for rx in validator.get("regexs", []):
        pattern = rx.get("regex")
        rtype = rx.get("type", "include") # "include" = must match, "exclude" = must not
        if not pattern:
            continue
        try:
            matches = re.search(pattern, value) is not None
        except re.error:
            continue
        if rtype == "include" and not matches:
            errs.append({
                "code": "regex_mismatch",
                "property": prop_name,
                "value": value,
                "regex": pattern,
                "message": (f"'{prop_name}'='{value}' does not match required "
                            f"pattern {pattern}"),
            })
        elif rtype == "exclude" and matches:
            errs.append({
                "code": "regex_excluded",
                "property": prop_name,
                "value": value,
                "regex": pattern,
                "message": (f"'{prop_name}'='{value}' matches forbidden "
                            f"pattern {pattern}"),
            })

    return errs


def _suggest_property(typo: str, valid_names) -> Optional[str]:
    """Cheap typo correction. Returns a single suggestion or None."""
    typo_l = typo.casefold()
    # Exact case-fold match first
    for n in valid_names:
        if n.casefold() == typo_l:
            return n
    # Prefix match
    for n in valid_names:
        if n.casefold().startswith(typo_l) or typo_l.startswith(n.casefold()):
            return n
    return None
