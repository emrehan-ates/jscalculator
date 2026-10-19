"""
APIC client MCP server.

Exposes four tools:
    - apic_login : manually re-authenticate (auto-login also happens)
    - apic_get : GET to any APIC endpoint
    - apic_post : POST a payload to APIC (create/update MOs)
    - apic_delete : DELETE an MO at the given DN

Tool descriptions deliberately reference the aci_kb server's tools so
the LLM chains schema lookup → validation → DN building → API call
in the right order.
"""
from typing import Any, Optional

from dotenv import load_dotenv
from fastmcp import FastMCP

# Load .env BEFORE the client module imports config
load_dotenv()

from apic_client import (
    get_client,
    APICError,
)


mcp = FastMCP("apic-client")


def _error_response(e: APICError) -> dict:
    """Convert exception to the structured dict the LLM should see."""
    return e.to_dict()


@mcp.tool()
def apic_login() -> dict:
    """
    Authenticate to the APIC controller. Login normally happens automatically
    on the first GET/POST/DELETE call, so you usually do NOT need to call
    this directly. Use this only when:
        - You want to verify connectivity before starting a longer workflow.
        - You suspect the cached session is stale and want to force re-login.

    Returns:
    On success: {status: "authenticated", host: str, expires_in_sec: int}
    On failure: {error: str, url: str, http_status: int, apic_errors: [...]}
    """
    try:
        return get_client().login(force=True)
    except APICError as e:
        return _error_response(e)


@mcp.tool()
def apic_get(path: str, params: Optional[dict] = None) -> dict:
    """
    Send a GET request to APIC. Use for reading any MO, querying classes,
    or fetching faults/health.

    Path examples (all valid):
    "fvBD.json" (just the resource)
    "/api/node/class/fvBD.json" (absolute API path)
    "/api/node/mo/uni/tn-Production.json"

    Common query parameters:
    query-target-filter: 'eq(fvBD.name,"Web-BD")'
    rsp-subtree: 'children' | 'full'
    rsp-prop-include: 'config-only'
    order-by: e.g. 'faultRecord.created|desc' or 'faultInst.severity|desc'
    page-size / page: paginate large result sets (page is 0-indexed).
    totalCount in the response is still the FULL match count even when
    page-size limits how many objects are returned in imdata.

    Large / high-cardinality classes (faultRecord, eventRecord, aaaModLR,
    fvCEp, unscoped faultInst, etc.): ALWAYS pass a small page-size (1 for
    count-only, 20-50 for a diagnostic sample). Do not omit page-size and
    do not walk page=1,2,3… to dump the entire set — answer from totalCount
    plus the sample, or narrow the filter instead.

    BEFORE calling this, if you don't know the exact class name or DN format,
    call `aci_get_mo_spec(class_name)` from the aci-kb server. The spec tells
    you the DN template, available properties, and valid filter targets.

    Args:
    path: Endpoint path. Class queries use /api/node/class/<class>.json,
    single-MO queries use /api/node/mo/<dn>.json.
    params: Optional dict of query parameters (e.g. query-target-filter,
    rsp-subtree, rsp-prop-include, order-by, page-size, page).

    Returns:
    APIC's JSON response (typically {"imdata": [...], "totalCount": str})
    or a structured error dict if the request fails.
    """
    try:
        return get_client().get(path, params=params)
    except APICError as e:
        return _error_response(e)


@mcp.tool()
def apic_post(path: str, payload: dict) -> dict:
    """
    Send a POST request to APIC. Use for creating or updating managed
    objects (MOs).

    CRITICAL — REQUIRED PRE-FLIGHT WORKFLOW:
    Before calling this tool with any payload, you MUST complete the
    following steps in order. Skipping them causes APIC to reject the
    request with cryptic errors that are slow to debug.

    1. Call `aci_get_mo_spec(class_name)` (from the aci-kb server) to
    retrieve the schema for the target MO class. The spec lists the
    DN format, naming properties, mandatory properties, allowed enum
    values, and valid parent classes.

    2. Build the payload using ONLY properties that appear in the spec.
    For any property with a `valid_values` list, use one of those
    values verbatim — APIC rejects close-but-wrong values like "true"
    when the schema says "yes".

    3. Call `aci_validate_payload(class_name, payload)` and check the
    response. If valid is false, read the errors, fix the payload,
    and validate again. Each error has a code and a human-readable
    message; many include `did_you_mean` for typos.

    4. Call `aci_build_dn(class_name, naming_values, parent_dn)` to get
    the correct DN string. Do NOT construct DNs by hand.

    5. Only after validate returns valid=true, call this tool with the
    validated payload and the DN from build_dn.

    Path format:
    "/api/node/mo/<dn>.json" for creating/updating a specific MO
    Example: "/api/node/mo/uni/tn-Production/BD-Web-BD.json"

    The client always sets attributes.status to "created,modified" on the
    posted MO and any nested children. That is APIC's upsert: create if
    missing, update if it already exists. Do not send status:"created"
    (that fails when the object is already present).

    Args:
    path: APIC endpoint, typically /api/node/mo/<dn>.json.
    payload: APIC-native MO payload, shaped as
    {"<className>": {"attributes": {...}, "children": [...]}}.

    Returns:
    APIC's response on success, or a structured error dict on failure.
    Errors include http_status and apic_errors with APIC's own error
    codes — use these to diagnose schema or permission issues.
    """
    try:
        return get_client().post(path, payload)
    except APICError as e:
        return _error_response(e)


@mcp.tool()
def apic_delete(path: str) -> dict:
    """
    Send a DELETE request to APIC. Use to remove a managed object at the
    given DN.

    Before deleting, consider:
    - Use `apic_get` first to confirm the MO exists and to see its current
    state, especially if the user's request is ambiguous.
    - Deletes in ACI are often cascading — deleting a tenant removes all
    its bridge domains, EPGs, and contracts. Confirm scope with the
    user when the impact is wide.
    - Use `aci_build_dn(class_name, naming_values, parent_dn)` to construct
    the DN reliably rather than typing it by hand.

    Path format:
    "/api/node/mo/<dn>.json"
    Example: "/api/node/mo/uni/tn-Production/BD-Web-BD.json"

    Args:
    path: APIC endpoint pointing to the MO to delete.

    Returns:
    APIC's response on success, or a structured error dict on failure.
    """
    try:
        return get_client().delete(path)
    except APICError as e:
        return _error_response(e)


if __name__ == "__main__":
    mcp.run()
