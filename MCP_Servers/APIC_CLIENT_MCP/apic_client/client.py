"""
Thread-safe lazy-login HTTP client for Cisco APIC.

Design:
- Module load does NOT touch APIC. Login happens on the first real request.
- Sessions are cached and refreshed automatically before TTL expiry.
- All errors are typed (see errors.py) so the tool layer can return
structured, LLM-readable responses.
"""
from __future__ import annotations

import copy
import logging
import os
import threading
import time
from typing import Any, Optional

import requests
import urllib3

from .errors import APICError, APICAuthError, APICConnectionError, APICRequestError


# APIC commonly uses self-signed certs internally; suppress the warning
# spam and let APIC_VERIFY_SSL control actual verification.
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

log = logging.getLogger(__name__)

# APIC MO POST semantics: "created" fails if the object already exists.
# "created,modified" is the upsert (create if missing, update if present).
POST_MO_STATUS = "created,modified"


def _env_bool(key: str, default: bool) -> bool:
    raw = os.environ.get(key)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


class APICClient:
    """
    One instance per process. Holds the session cookie and refreshes it
    automatically. Thread-safe via an internal lock around login.
    """

    def __init__(self) -> None:
        self.host = os.environ.get("APIC_HOST", "").rstrip("/")
        self.username = os.environ.get("APIC_USERNAME", "")
        self.password = os.environ.get("APIC_PASSWORD", "")
        self.verify_ssl = _env_bool("APIC_VERIFY_SSL", False)
        self.timeout = int(os.environ.get("APIC_TIMEOUT", "30"))
        self.refresh_before = int(os.environ.get("APIC_REFRESH_BEFORE", "60"))

        if not self.host:
            raise APICAuthError("APIC_HOST not set in environment")
        if not self.username or not self.password:
            raise APICAuthError("APIC_USERNAME or APIC_PASSWORD not set")

        self._session = requests.Session()
        self._session.verify = self.verify_ssl
        self._token_expires_at: float = 0.0
        self._lock = threading.Lock()

    # ---------- public API ----------

    def login(self, force: bool = False) -> dict:
        """
        Login to APIC and cache the session cookie. Returns a small dict
        suitable for returning from a tool (no secrets included).
        """
        with self._lock:
            if not force and self._is_session_valid():
                return {
                    "status": "already_authenticated",
                    "host": self.host,
                    "expires_in_sec": int(self._token_expires_at - time.time()),
                }

            url = f"{self.host}/api/aaaLogin.json"
            payload = {
                "aaaUser": {
                    "attributes": {
                    "name": self.username,
                    "pwd": self.password,
            }
            }
            }
            try:
                resp = self._session.post(
                    url, json=payload, timeout=self.timeout
                )
            except requests.exceptions.SSLError as e:
                raise APICConnectionError(
                f"SSL error connecting to APIC: {e}", url=url
                )
            except requests.exceptions.ConnectionError as e:
                raise APICConnectionError(
                f"Cannot reach APIC at {self.host}: {e}", url=url
                )
            except requests.exceptions.Timeout as e:
                raise APICConnectionError(
                f"Timeout connecting to APIC: {e}", url=url
                )
            except Exception as e:
                raise APICConnectionError(
                f"Unexpected error contacting APIC: {e}", url=url
                )

            if resp.status_code != 200:
                apic_errors = self._extract_apic_errors(resp)
                raise APICAuthError(
                    f"Login failed with HTTP {resp.status_code}",
                    url=url, status=resp.status_code,
                    apic_errors=apic_errors,
                )

            data = resp.json()
            # APIC returns refreshTimeoutSeconds in aaaLogin response
            try:
                login_attrs = data["imdata"][0]["aaaLogin"]["attributes"]
                ttl = int(login_attrs.get("refreshTimeoutSeconds", "600"))
            except (KeyError, IndexError, ValueError):
                ttl = 600
            # Subtract a small safety margin so we refresh before expiry
            self._token_expires_at = time.time() + max(ttl - self.refresh_before, 30)

            log.info("APIC login successful, session valid for ~%d seconds", ttl)
            return {
                "status": "authenticated",
                "host": self.host,
                "expires_in_sec": ttl,
            }

    def get(self, path: str, params: Optional[dict] = None) -> dict:
        return self._request("GET", path, params=params)

    def post(self, path: str, payload: dict) -> dict:
        body = _apply_post_mo_status(payload)
        return self._request("POST", path, json_body=body)

    def delete(self, path: str) -> dict:
        return self._request("DELETE", path)

    # ---------- internals ----------

    def _is_session_valid(self) -> bool:
        return time.time() < self._token_expires_at

    def _ensure_session(self) -> None:
        if not self._is_session_valid():
            self.login(force=True)

    def _request(
        self,
        method: str,
        path: str,
        params: Optional[dict] = None,
        json_body: Optional[dict] = None,
    ) -> dict:
        self._ensure_session()

        # Normalize path: accept "fvBD.json", "/api/class/fvBD.json", or full URL
        url = self._build_url(path)

        try:
            resp = self._session.request(
            method, url,
            params=params, json=json_body,
            timeout=self.timeout,
            )
        except requests.exceptions.ConnectionError as e:
            raise APICConnectionError(
            f"Connection error during {method} {path}: {e}", url=url
            )
        except requests.exceptions.Timeout as e:
            raise APICConnectionError(
            f"Timeout during {method} {path}: {e}", url=url
            )
        except Exception as e:
            raise APICConnectionError(
            f"Unexpected error during {method} {path}: {e}", url=url
            )

        # Session might have expired silently; APIC returns 403 with
        # specific code. Retry once after re-login.
        if resp.status_code == 403:
            apic_errors = self._extract_apic_errors(resp)
            if any("Token timeout" in (e.get("text") or "") or
                    e.get("code") == "403"
                    for e in apic_errors):
                log.info("Session expired mid-request, re-authenticating")
                self.login(force=True)
                resp = self._session.request(
                    method, url, params=params, json=json_body,
                    timeout=self.timeout,
                )

        if not (200 <= resp.status_code < 300):
            apic_errors = self._extract_apic_errors(resp)
            raise APICRequestError(
                f"APIC returned HTTP {resp.status_code}",
                url=url, status=resp.status_code,
                apic_errors=apic_errors,
        )

        # Most APIC responses are JSON; some DELETE responses are empty
        if not resp.content:
            return {"imdata": [], "totalCount": "0"}
        try:
            return resp.json()
        except ValueError:
            return {"raw": resp.text, "warning": "non-json response"}

    def _build_url(self, path: str) -> str:
        if path.startswith("http://") or path.startswith("https://"):
            return path
        if not path.startswith("/"):
            path = "/" + path
        if not path.startswith("/api"):
            path = "/api" + path
        return f"{self.host}{path}"

    @staticmethod
    def _extract_apic_errors(resp: requests.Response) -> list[dict]:
        """
        APIC error responses look like:
            {"imdata":[{"error":{"attributes":{"code":"105","text":"..."}}}],
            "totalCount":"1"}
        Extract the error attributes for structured responses.
        """
        try:
            data = resp.json()
        except ValueError:
            return [{"text": resp.text[:500]}] if resp.text else []
        out = []
        for item in data.get("imdata", []) or []:
            err = item.get("error", {}).get("attributes", {})
            if err:
                out.append(err)
        return out


def _apply_post_mo_status(payload: Any) -> Any:
    """
    Return a copy of payload with MO attributes.status set to
    "created,modified" on every nested MO. APIC uses this as upsert;
    "created" alone fails if the object already exists.
    """
    if not isinstance(payload, dict):
        return payload

    body = copy.deepcopy(payload)

    def _walk(node: Any) -> None:
        if isinstance(node, list):
            for item in node:
                _walk(item)
            return
        if not isinstance(node, dict):
            return

        attrs = node.get("attributes")
        children = node.get("children")
        if isinstance(attrs, dict):
            attrs["status"] = POST_MO_STATUS
            if isinstance(children, list):
                _walk(children)
            return

        for value in node.values():
            _walk(value)

    _walk(body)
    return body


# Singleton accessor so all tools share one client (and one session)
_client: Optional[APICClient] = None
_client_lock = threading.Lock()


def get_client() -> APICClient:
    global _client
    if _client is None:
        with _client_lock:
            if _client is None:
                _client = APICClient()
    return _client
