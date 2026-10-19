"""
get_mo_spec(class_name) — return everything the LLM needs to construct
ACI API calls involving a given MO class.

Designed to be the LLM's primary "look up the schema" tool. Output is
compact and structured so it fits cleanly in a tool-call response.
"""
from typing import Optional
from . import store


def get_mo_spec(class_name: str, include_optional: bool = True) -> Optional[dict]:
    """
    Return the API construction spec for one MO class.

    Args:
        class_name: 'fvBD' or 'fv:BD' — both forms accepted, output normalized.
        include_optional: include non-mandatory properties in the response.
        Set False for tighter outputs when the LLM only needs the minimum.

    Returns:
        dict, or None if the class does not exist.
    """
    canonical = store.resolve_class_name(class_name)
    if not canonical:
        return None

    cls = store.get_class(canonical)
    if not cls:
        return None

    all_props = store.get_properties(canonical)
    naming_props = [p for p in all_props if p["is_naming"]]
    mandatory_props = [p for p in all_props if p["mandatory"] and not p["is_naming"]]
    optional_props = [p for p in all_props if not p["mandatory"] and not p["is_naming"]]

    spec = {
        "class_name": cls["class_name"],
        "class_colon_name": cls["class_colon_name"],
        "label": cls["label"],
        "description": cls["description"],
        "category": cls["mo_category"],
        "is_abstract": bool(cls["is_abstract"]),
        "is_configurable": bool(cls["is_configurable"]),
        "is_deprecated": bool(cls["is_deprecated"]),

        # Naming and placement
        "dn_formats": store.get_dn_formats(canonical),
        "rn_format": cls["rn_format"],
        "identified_by": store.get_identified_by(canonical),
        "parent_classes": store.get_parents(canonical),

        # Properties — split by role for clarity
        "naming_properties": [_summarize_prop(p) for p in naming_props],
        "mandatory_properties": [_summarize_prop(p) for p in mandatory_props],

        # Linking — Rs* MOs this class owns. The LLM POSTs these as children
        # to link to other objects (e.g. fvRsCtx links a BD to a VRF).
        "relationship_classes": store.get_relations_to(canonical),

        # Hint for the LLM about how to construct an HTTP path.
        "api_paths": _build_api_path_hints(cls, store.get_dn_formats(canonical)),
    }

    if include_optional:
        spec["optional_properties"] = [_summarize_prop(p) for p in optional_props]

    return spec


def _summarize_prop(p: dict) -> dict:
    """Compact property representation for LLM consumption."""
    out = {
        "name": p["prop_name"],
        "type": p["base_type"],
        "ui_type": p["ui_type"],
        "description": p["description"],
    }
    if p["default_value"] is not None:
        out["default"] = p["default_value"]
    if p["create_only"]:
        out["create_only"] = True
    if p["read_only"]:
        out["read_only"] = True

    # Surface enum values inline — saves the LLM another lookup
    if p["valid_values"]:
        # validValues entries have shape:
        # {"value": "no", "localName": "defaultValue", ...}
        # Filter out the synthetic "defaultValue" entry; expose real values only.
        real = [
            v for v in p["valid_values"]
            if v.get("localName") != "defaultValue"
        ]
        if real:
            out["valid_values"] = [v["value"] for v in real]

    # Surface validator constraints (regex / numeric range) compactly
    if p["validators"]:
        constraints = []
        for v in p["validators"]:
            if "regexs" in v:
                for rx in v["regexs"]:
                    constraints.append({"regex": rx.get("regex"), "type": rx.get("type")})
            if "min" in v or "max" in v:
                cmin = v.get("min")
                cmax = v.get("max")
                constraints.append({"min": cmin, "max": cmax})
        if constraints:
            out["constraints"] = constraints

    return out


def _build_api_path_hints(cls: dict, dn_formats: list[str]) -> dict:
    """
    Construct example API path patterns the LLM can use as a starting point.
    These are templates with {placeholder} naming variables left intact.
    """
    cn = cls["class_name"]
    hints: dict = {
        "class_query": f"/api/node/class/{cn}.json",
        "mo_query_templates": [],
        "mo_post_templates": [],
    }
    for dn in dn_formats:
        hints["mo_query_templates"].append(f"/api/node/mo/{dn}.json")
        hints["mo_post_templates"].append(f"/api/node/mo/{dn}.json (POST)")
    return hints
