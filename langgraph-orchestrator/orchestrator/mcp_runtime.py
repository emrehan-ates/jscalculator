"""Persistent MCP runtime layer.

Keeps stdio MCP subprocesses (aci_kb, apic_client, skills) alive for the whole
application lifetime and hands out *session-bound* tool objects, instead of the
old per-call model where every ``ainvoke()`` spawned a fresh subprocess (new
Python interpreter, new import chain, new TLS handshake, new aaaLogin).

Design notes
------------
- Tools are loaded with ``load_mcp_tools(session)`` against an OPEN session, not
  the session-less ``MultiServerMCPClient.get_tools()`` — that is exactly the
  difference between "reuse one subprocess" and "spawn one per call".
- anyio cancel-scope trap: the MCP stdio transport opens an anyio task group, and
  a cancel scope must be exited in the SAME task it was entered in. Chainlit runs
  ``on_chat_start`` / ``on_message`` / ``on_chat_end`` in different asyncio tasks,
  so a session opened in one handler and closed in another raises
  "Attempted to exit cancel scope in a different task". We therefore open AND
  close every session inside ONE long-lived owner task; other tasks only signal
  it ("ready?" / "close"). Calling a tool from another task is fine because the
  request travels over an anyio memory stream and never enters the cancel scope.
- Scope: aci_kb + skills are fabric-independent (one shared runtime); each fabric
  gets its OWN apic_client runtime, keyed by fabric id, because every fabric has
  a separate APIC server. Registries are merged per request so a duplicate tool
  name (apic_login/apic_get exist on all four) can never leak the wrong fabric.

VERSION ASSUMPTION (doğrulanması gerekiyor): this relies on
``langchain_mcp_adapters.client.MultiServerMCPClient.session(name)`` being an
async context manager that yields an initialized ``ClientSession``, and on
``langchain_mcp_adapters.tools.load_mcp_tools``. Both exist in
langchain-mcp-adapters 0.1.x.
"""
from __future__ import annotations

import asyncio
import logging
import os
from collections import defaultdict
from contextlib import AsyncExitStack

from langchain_mcp_adapters.client import MultiServerMCPClient
from langchain_mcp_adapters.tools import load_mcp_tools

log = logging.getLogger(__name__)

# Per-fabric/shared startup timeout so an unreachable APIC can never hang app
# startup or a first request forever; overridable via env, default 60s.
_START_TIMEOUT = float(os.environ.get("MCP_START_TIMEOUT", "60"))


def _shared_connections() -> dict:
    """aci_kb + skills — fabric-independent, spawned once for the whole app."""
    return {
        "aci_kb": {
            "command": os.environ["ACI_KB_PYTHON"],
            "args": [os.environ["ACI_KB_SERVER"]],
            "transport": "stdio",
        },
        "skills": {
            "command": os.environ["SKILLS_PYTHON"],
            "args": [os.environ["SKILLS_SERVER"]],
            "transport": "stdio",
        },
    }


def _fabric_connection(fabric: dict) -> dict:
    """apic_client for ONE fabric — its own server deployment/credentials."""
    return {
        "apic_client": {
            "command": fabric["apic_client_python"],
            "args": [fabric["apic_client_server"]],
            "transport": "stdio",
        },
    }


def _fabric_key(fabric: dict):
    """Stable identity for a fabric's runtime (id is unique; name is fallback)."""
    return fabric.get("id", fabric.get("name"))


class _Runtime:
    """One owner task owning one AsyncExitStack of open MCP sessions."""

    def __init__(self, name: str, connections: dict):
        self._name = name
        self._connections = connections
        self._tools: dict = {}
        self._owner_task: asyncio.Task | None = None
        self._ready = asyncio.Event()
        self._closing = asyncio.Event()
        self._error: BaseException | None = None

    async def _owner(self):
        # Open and close every session in THIS task so no cancel scope is exited
        # from a foreign task (the anyio trap described in the module docstring).
        try:
            client = MultiServerMCPClient(self._connections)
            async with AsyncExitStack() as stack:
                tools: dict = {}
                for server_name in self._connections:
                    session = await stack.enter_async_context(client.session(server_name))
                    for tool in await load_mcp_tools(session):
                        if tool.name in tools:
                            # Do not silently overwrite — warn and keep the first.
                            log.warning(
                                "Duplicate MCP tool name %r (server %r) ignored in runtime %r",
                                tool.name, server_name, self._name,
                            )
                            continue
                        tools[tool.name] = tool
                self._tools = tools
                self._ready.set()
                await self._closing.wait()  # park here, alive, until asked to close
        except asyncio.CancelledError:
            raise
        except BaseException as e:  # noqa: BLE001 - surfaced to the waiter
            self._error = e
            self._ready.set()

    async def start(self, timeout: float = _START_TIMEOUT):
        self._owner_task = asyncio.create_task(self._owner(), name=f"mcp-runtime:{self._name}")
        try:
            await asyncio.wait_for(self._ready.wait(), timeout)
        except asyncio.TimeoutError:
            await self._cancel()
            raise RuntimeError(
                f"MCP runtime {self._name!r} did not become ready within {timeout:.0f}s "
                f"(is the server/APIC reachable?)"
            )
        if self._error is not None:
            err = self._error
            await self._cancel()
            raise RuntimeError(f"MCP runtime {self._name!r} failed to start: {err}") from err

    def healthy(self) -> bool:
        # Cheap liveness check done on every borrow: owner still running, it did
        # not error, and it actually produced tools. NOTE: a subprocess that dies
        # while the owner is parked at _closing.wait() is NOT detected here — that
        # only surfaces when a tool call fails. A periodic tools/list ping would
        # catch it but is intentionally left out for now (doğrulanması gerekiyor).
        return (
            self._owner_task is not None
            and not self._owner_task.done()
            and self._error is None
            and bool(self._tools)
        )

    def tools(self) -> dict:
        return dict(self._tools)

    async def _cancel(self):
        if self._owner_task and not self._owner_task.done():
            self._owner_task.cancel()
            try:
                await self._owner_task
            except BaseException:  # noqa: BLE001 - shutdown path, swallow
                pass

    async def close(self):
        # Signal the owner; the actual context-manager exit happens inside the
        # owner task (same task that entered) — no cross-task cancel scope.
        self._closing.set()
        if self._owner_task:
            try:
                await self._owner_task
            except BaseException:  # noqa: BLE001
                await self._cancel()


class MCPRuntimeManager:
    """Owns the shared runtime and one runtime per fabric; lazy + warm-cached."""

    def __init__(self):
        self._shared: _Runtime | None = None
        self._shared_lock = asyncio.Lock()
        self._fabrics: dict = {}
        self._fabric_locks: dict = defaultdict(asyncio.Lock)

    async def _get_shared(self) -> _Runtime:
        if self._shared is not None and self._shared.healthy():
            return self._shared
        async with self._shared_lock:
            if self._shared is not None and self._shared.healthy():
                return self._shared
            if self._shared is not None:  # unhealthy → recycle
                await self._shared.close()
            rt = _Runtime("shared", _shared_connections())
            await rt.start()
            self._shared = rt
            return rt

    async def _get_fabric(self, fabric: dict) -> _Runtime:
        key = _fabric_key(fabric)
        existing = self._fabrics.get(key)
        if existing is not None and existing.healthy():
            return existing
        # Per-fabric lock: two concurrent first requests must not build two runtimes.
        async with self._fabric_locks[key]:
            existing = self._fabrics.get(key)
            if existing is not None and existing.healthy():
                return existing
            if existing is not None:
                await existing.close()
            rt = _Runtime(f"fabric:{key}", _fabric_connection(fabric))
            await rt.start()
            self._fabrics[key] = rt
            return rt

    async def get_registry(self, fabric: dict) -> dict:
        """Fresh, per-request merge of shared tools + THIS fabric's apic tools.

        Merged per request (not pooled globally) so the duplicate apic_* tool
        names across fabrics can never resolve to the wrong fabric's server.
        A shared-runtime failure must not hide a fabric-runtime failure, so the
        fabric is resolved first and its error surfaces to the caller.
        """
        fab = await self._get_fabric(fabric)
        shared = await self._get_shared()
        registry = shared.tools()
        registry.update(fab.tools())  # fabric wins on name collision, by design
        return registry

    async def warm(self, fabric: dict):
        """Best-effort pre-spawn so the first real request isn't slow; errors are
        logged, not raised — the real user-facing error surfaces on the request."""
        try:
            await self.get_registry(fabric)
        except Exception as e:  # noqa: BLE001
            log.warning("MCP warm-up failed for fabric %r: %s", _fabric_key(fabric), e)

    async def aclose(self):
        """Close every runtime (call from an app-shutdown hook if available; the
        child subprocesses also die with the parent process on exit)."""
        runtimes = list(self._fabrics.values())
        if self._shared is not None:
            runtimes.append(self._shared)
        for rt in runtimes:
            try:
                await rt.close()
            except Exception as e:  # noqa: BLE001
                log.warning("Error closing MCP runtime: %s", e)


_manager: MCPRuntimeManager | None = None


def get_runtime_manager() -> MCPRuntimeManager:
    global _manager
    if _manager is None:
        _manager = MCPRuntimeManager()
    return _manager
