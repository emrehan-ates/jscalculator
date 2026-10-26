"""VLAN Desk API client (free-VLAN lookup and "in use" reservations).

Config (.env):
  VLAN_API_URL        e.g. http://10.6.107.105:8003
  VLAN_API_USERNAME   VLAN Desk site account (not the APIC account)
  VLAN_API_PASSWORD
  VLAN_API_TIMEOUT    seconds, default 15
A new VLAN must be free on EVERY environment the API knows (all APICs,
Core L3s and DCIs): queries send all source ids, and the API returns their
intersection. A pool is optional; with no pool the whole VLAN range counts.

The API uses a session cookie (no token): every public call opens a client,
logs in, does its work and closes. Querying never reserves; a reservation is
global (the VLAN leaves every free list) and answers 409 when the VLAN is no
longer free.
"""
import os

import httpx

# Banka fabrics must not be queried with the DIB pool (API guide: the result
# is silently empty).
_DIB_EXCLUDED_SOURCES = {"APIC_EVM_BANKA", "APIC_KKB_BANKA"}


class VlanApiError(Exception):
    pass


class VlanConflict(VlanApiError):
    """409: the VLAN is no longer free / already reserved."""


def _config():
    url = (os.environ.get("VLAN_API_URL") or "").rstrip("/")
    user = os.environ.get("VLAN_API_USERNAME") or ""
    pw = os.environ.get("VLAN_API_PASSWORD") or ""
    if not (url and user and pw):
        raise VlanApiError("VLAN API is not configured (VLAN_API_URL / VLAN_API_USERNAME / "
                           "VLAN_API_PASSWORD)")
    try:
        timeout = float(os.environ.get("VLAN_API_TIMEOUT", "15"))
    except ValueError:
        timeout = 15.0
    return url, user, pw, timeout


def _err_text(resp):
    try:
        return resp.json().get("error") or resp.text
    except ValueError:
        return resp.text


class _Session:
    """async with _Session() as s: await s.get(...) — logged-in client."""

    async def __aenter__(self):
        url, user, pw, timeout = _config()
        self.client = httpx.AsyncClient(base_url=url, timeout=timeout)
        try:
            r = await self.client.post("/api/login", json={"username": user, "password": pw})
        except httpx.HTTPError as e:
            await self.client.aclose()
            raise VlanApiError(f"VLAN API unreachable: {e}")
        if r.status_code != 200:
            await self.client.aclose()
            raise VlanApiError(f"VLAN API login failed ({r.status_code}): {_err_text(r)}")
        return self

    async def __aexit__(self, *exc):
        try:
            await self.client.post("/api/logout", json={})
        except httpx.HTTPError:
            pass
        await self.client.aclose()

    async def call(self, method, path, **kw):
        try:
            r = await self.client.request(method, path, **kw)
        except httpx.HTTPError as e:
            raise VlanApiError(f"VLAN API {method} {path} failed: {e}")
        if r.status_code == 409:
            raise VlanConflict(_err_text(r))
        if r.status_code >= 400:
            raise VlanApiError(f"VLAN API {method} {path} -> {r.status_code}: {_err_text(r)}")
        return r.json() if r.content else {}


def _ids(items):
    out = []
    for it in items or []:
        out.append(it.get("id") if isinstance(it, dict) else str(it))
    return [i for i in out if i]


def match_pool(user_text, pool_ids):
    """Map what the user typed ("legacy", "static", "DIB") to a pool id.
    Returns (pool_id, None) or (None, error)."""
    want = str(user_text or "").strip().lower().replace(" ", "_")
    if not want:
        return None, "no VLAN pool given"
    exact = [p for p in pool_ids if p.lower() == want]
    if exact:
        return exact[0], None
    fuzzy = [p for p in pool_ids if want in p.lower() or p.lower().split("_")[0] == want.split("_")[0]]
    if len(fuzzy) == 1:
        return fuzzy[0], None
    if not fuzzy:
        return None, f"unknown VLAN pool '{user_text}' (available: {', '.join(pool_ids)})"
    return None, f"VLAN pool '{user_text}' is ambiguous: {', '.join(fuzzy)}"


async def list_pools():
    async with _Session() as s:
        data = await s.call("GET", "/api/sources")
    return _ids(data.get("pools"))


async def resolve_scope(pool_text=None):
    """(sources, pool) for a new VLAN: every environment the API knows, and the
    pool the user/admin named (None = no pool filter). The DIB pool is only
    valid for İştirak environments, so the Banka APICs are left out with it
    (API guide: sending them with DIB silently returns nothing)."""
    async with _Session() as s:
        data = await s.call("GET", "/api/sources")
    sources = _ids(data.get("sources"))
    if not sources:
        raise VlanApiError("the VLAN API returned no environments")
    pool = None
    if pool_text and str(pool_text).strip().lower() not in ("none", "any", "yok", "-"):
        pool, err = match_pool(pool_text, _ids(data.get("pools")))
        if err:
            raise VlanApiError(err)
        if pool.upper().startswith("DIB"):
            sources = [s for s in sources if s not in _DIB_EXCLUDED_SOURCES]
    return sources, pool


async def free_vlans(sources, pool=None, limit=None):
    """VLAN ids (ascending) free on ALL `sources`, optionally inside `pool`."""
    params = {"source": ",".join(sources)}
    if pool:
        params["pool"] = pool
    if limit:
        params["limit"] = int(limit)
    async with _Session() as s:
        data = await s.call("GET", "/api/vlans", params=params)
    return [int(v) for v in data.get("vlans", [])]


async def reserve(vlan, sources, pool, note):
    """Mark a VLAN in use. Raises VlanConflict if it is no longer free."""
    body = {"vlan_id": int(vlan), "source_ids": list(sources), "note": str(note)[:180]}
    if pool:
        body["pool_ids"] = [pool]
    async with _Session() as s:
        await s.call("POST", "/api/reservations", json=body)


async def release(vlan):
    """Remove a reservation (used when the APIC push fails)."""
    async with _Session() as s:
        await s.call("DELETE", "/api/reservations", json={"vlan_id": int(vlan)})
