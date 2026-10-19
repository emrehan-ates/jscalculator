"""
ACI Knowledge Base MCP server.

Exposes three tools for constructing and validating ACI APIC API calls:
    - aci_get_mo_spec : look up schema for a managed object class
    - aci_validate_payload: validate a payload against its schema
    - aci_build_dn : construct a Distinguished Name correctly

Reads aci.db (sibling file by default; override with ACI_DB_PATH env var).
No outbound network calls; no APIC access required at runtime.

MOST IMPORTANTLY, This tool does NOT send an HTTP request to the APIC! Use APIC-CLIENT tool to send a request after your job is done with this tool.
"""
from typing import Optional

from fastmcp import FastMCP

from aci_kb import get_mo_spec, validate_payload, build_dn


mcp = FastMCP("aci_kb")


@mcp.tool()
def aci_get_mo_spec(class_name: str, include_optional: bool = True) -> dict:
    """
    Look up the schema and API construction details for an ACI managed object
    (MO) class. Call this BEFORE constructing any APIC API call so you know
    the correct DN format, required properties, allowed enum values, and
    parent containment for the target class.
    
    MOST IMPORTANTLY, your job is NOT done after this! Validate the payload and send a request by using APIC-CLIENT tool!

    Examples:
        aci_get_mo_spec("fvBD") -> Bridge Domain schema
        aci_get_mo_spec("fvTenant") -> Tenant schema
        aci_get_mo_spec("l1PhysIf") -> Physical interface schema

    Args:
        class_name: ACI class name. Accepts both forms: 'fvBD' or 'fv:BD'.
        include_optional: include optional (non-mandatory, non-naming)
            properties in the response. Set False for a tighter response
            when you only need the minimum fields to construct a payload.

    Returns:
        Dict with: class_name, label, description, dn_formats, rn_format,
        identified_by (the naming property names), parent_classes,
        naming_properties, mandatory_properties, optional_properties,
        relationship_classes (Rs* MOs to POST as children for linking),
        and api_paths (URL templates for class queries and MO operations).
        Returns {"error": "..."} if the class is unknown.
    """
    spec = get_mo_spec(class_name, include_optional=include_optional)
    if spec is None:
        return {"error": f"Unknown ACI class: {class_name}"}
    return spec


@mcp.tool()
def aci_validate_payload(
    class_name: str,
    payload: dict,
    is_update: bool = False,
) -> dict:
    """
    Validate an ACI MO payload against its schema BEFORE sending it to APIC.
    Catches missing required fields, unknown properties (typos), invalid
    enum values, regex/range violations, and read-only or create-only
    property misuse.

    ALWAYS call this before any POST or PUT to APIC. If the response has
    errors, fix the payload and re-validate. Do not send a payload to APIC
    until validate returns valid=true.
    
    MOST IMPORTANTLY, your job is NOT done after this! Send a request by using APIC-CLIENT tool to finish the job!

    Args:
        class_name: ACI class name like 'fvBD'.
        payload: the MO payload. Either APIC-native shape
        ({"fvBD": {"attributes": {"name": "Web-BD", ...}}})
        or flat attributes ({"name": "Web-BD", "arpFlood": "yes"}).
        is_update: True if updating an existing MO. Triggers create-only
        property checks. Leave False (default) for new-MO creation.

    Returns:
        {valid: bool, class_name: str, errors: [...], warnings: [...],
        normalized_attributes: {...}}.
        Each error has a code (unknown_property, missing_required,
        invalid_enum_value, regex_mismatch, ...) and a human-readable
        message; many include did_you_mean suggestions for typos.
    """
    return validate_payload(class_name, payload, is_update=is_update)


@mcp.tool()
def aci_build_dn(
    class_name: str,
    naming_values: dict,
    parent_dn: Optional[str] = None,
) -> dict:
    """
    Construct a valid ACI Distinguished Name (DN) for an MO. Use this
    instead of building DNs by hand — getting them wrong is one of the
    most common ways API calls fail.
    
    MOST IMPORTANTLY, your job is NOT done after this! Validate the payload and send a request by using APIC-CLIENT tool!

    Examples:
        aci_build_dn("fvTenant", {"name": "Production"})
            -> {"dn": "uni/tn-Production", ...}
        aci_build_dn("fvBD", {"name": "Web-BD"},
                      parent_dn="uni/tn-Production")
            -> {"dn": "uni/tn-Production/BD-Web-BD", ...}

    When the class lives multiple levels deep (e.g. fvBD under fvTenant),
    pass parent_dn explicitly. This is more reliable than relying on the
    raw DN format string, which can have ambiguous duplicate placeholders.

    Args:
        class_name: ACI class name like 'fvBD'.
        naming_values: dict mapping each naming-property name to its value.
            Use aci_get_mo_spec(class_name).identified_by to see which
            properties are required.
        parent_dn: optional. The DN of the parent MO. Strongly recommended
            for any class deeper than the universe root.

    Returns:
        {dn: str | None, rn: str | None, errors: [...], warnings: [...]}.
        On success, dn is the full DN string. On failure, dn is null and
        errors describes the problem.
    """
    return build_dn(class_name, naming_values, parent_dn=parent_dn)


if __name__ == "__main__":
    mcp.run()
