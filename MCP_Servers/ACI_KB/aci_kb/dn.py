"""
build_dn(class_name, naming_values) — produce a valid ACI Distinguished Name
from a class and the values for its naming properties.

Handles the common case where dnFormats lists a single template like
'uni/tn-{name}/BD-{name}' — note the duplicated {name} which is ACI's way
of saying "this property name is reused at multiple levels of the DN."

For these duplicated-placeholder DN formats, the LLM should instead pass
'parent_dn' explicitly and we'll concatenate the rn_format ourselves.
"""
import re
from typing import Optional
from . import store


_PLACEHOLDER = re.compile(r"\{([^}]+)\}")


def build_dn(
    class_name: str,
    naming_values: dict,
    parent_dn: Optional[str] = None,
) -> dict:
    """
    Build the DN for an MO.

    Args:
        class_name: 'fvBD' or 'fv:BD'.
        naming_values: {prop_name: value} for this MO's naming properties.
            For fvBD that's {"name": "Web-BD"}.
        parent_dn: optional. If provided, we use rn_format and append to the
            parent's DN, which is more reliable than dn_formats templates
            since some templates have ambiguous duplicate placeholders.

    Returns:
        {"dn": "uni/tn-Production/BD-Web-BD", "rn": "BD-Web-BD",
        "warnings": [...], "errors": [...]}
        On failure, "dn" is None and errors describe the problem.
    """
    canonical = store.resolve_class_name(class_name)
    if not canonical:
        return {
            "dn": None, "rn": None,
            "errors": [f"Unknown class '{class_name}'"],
            "warnings": [],
        }

    cls = store.get_class(canonical)
    rn_format = cls["rn_format"] or ""
    dn_formats = store.get_dn_formats(canonical)
    identified_by = store.get_identified_by(canonical)
    errors: list[str] = []
    warnings: list[str] = []

    # Sanity check naming values
    for nb in identified_by:
        if nb not in naming_values:
            errors.append(f"Missing naming property '{nb}' for {canonical}")
    if errors:
        return {"dn": None, "rn": None, "errors": errors, "warnings": warnings}

    # Build RN
    rn = _substitute(rn_format, naming_values, errors, warnings)
    if errors:
        return {"dn": None, "rn": None, "errors": errors, "warnings": warnings}

    # Build DN
    if parent_dn is not None:
        dn = f"{parent_dn.rstrip('/')}/{rn}"
    else:
        # Use the (first) dn_format. Substitute placeholders; complain if
        # the template has placeholders we can't fill.
        if not dn_formats:
            errors.append(f"{canonical} has no DN format and no parent_dn given")
            return {"dn": None, "rn": rn, "errors": errors, "warnings": warnings}
        dn = _substitute(dn_formats[0], naming_values, errors, warnings,
                         allow_unfilled=False)
        if errors:
            errors.append(
                f"DN template '{dn_formats[0]}' contains placeholders not "
                f"in naming_values. Pass parent_dn explicitly to disambiguate."
            )
            return {"dn": None, "rn": rn, "errors": errors, "warnings": warnings}

    return {"dn": dn, "rn": rn, "errors": [], "warnings": warnings}


def _substitute(
    template: str,
    values: dict,
    errors: list,
    warnings: list,
    allow_unfilled: bool = True,
) -> str:
    """Replace {placeholder} with values[placeholder]. Track unfilled ones."""
    result_parts: list[str] = []
    last_end = 0
    for m in _PLACEHOLDER.finditer(template):
        result_parts.append(template[last_end:m.start()])
        key = m.group(1)
        if key in values:
            result_parts.append(str(values[key]))
        elif allow_unfilled:
            result_parts.append(m.group(0)) # leave {placeholder} intact
            warnings.append(f"Placeholder '{{{key}}}' left unfilled")
        else:
            errors.append(f"Unfilled placeholder '{{{key}}}' in '{template}'")
            return ""
        last_end = m.end()
    result_parts.append(template[last_end:])
    return "".join(result_parts)
