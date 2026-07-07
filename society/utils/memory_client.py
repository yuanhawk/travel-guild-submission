"""memory_client.py — genuine MCP client seam for the travel_memory server.

#159 — wires the ONLY non-redundant capability the travel_memory MCP surface offers
(the learned cross-trip preference loop) into the live orchestrator, as a REAL MCP
client: a full `mcp.client.session.ClientSession` handshake (`initialize()` ->
`list_tools()`-capable discovery -> `call_tool(name, args)` JSON-RPC framing), not a
relabeled function call.

Two tools only (see the #159 design-spec investigation — everything else on the
travel_memory surface is either an exact duplicate of the live UCP-merchant catalog
channel, or too thin/narrow-seeded to headline):
  - `log_search`               (WRITE) — persists a booked leg's {query, selection}
    and updates the user's 21-dim EMA preference vector (store.py §10.3).
  - `resolve_geographic_scope` (READ)  — cross-checks the stored preference vector
    against the trip's first-leg city and returns an `affinity_city` hint.

Transport (mirrors the house provider-seam convention in utils/emergency_feed.py /
utils/dining — factory + client, off by default, honest degrade-to-None):
  - MEMORY_MCP_URL set      -> out-of-process FastMCP `streamable-http` server (the
    deployed/demo topology; parallels the separately-hosted Go UCP merchant).
  - MEMORY_MCP_EMBEDDED=1   -> in-process FastMCP instance reached via the SDK's
    in-memory transport (mcp.shared.memory) — SAME ClientSession/initialize/
    call_tool code path, zero network/port/subprocess failure surface. Used by
    tests and as a no-extra-service fallback for the demo.
  - neither set             -> build_memory_client() returns None -> the orchestrator
    seam is fully bypassed -> byte-identical to today (var-0 sacred; this seam is
    OFF the deterministic booking/itinerary/price path by construction — see
    orchestrator._maybe_log_search / _maybe_read_affinity).

Async->sync bridge: ClientSession is asyncio-based; orchestrator.negotiate() is
synchronous and (per server.py's _run_negotiate) already runs inside a
ThreadPoolExecutor worker thread with NO running event loop — so each call here runs
its own short-lived asyncio.run() on a dedicated daemon thread with a hard timeout
(mirrors mcp_services/travel_memory/retrieval.py._fetch_live_hotels's
ThreadPoolExecutor+timeout pattern). ANY failure (import/handshake/timeout/tool
error/malformed result) -> None. NEVER raises: memory is opt-in and fully OFF the
var-0/money path (booking, itinerary, prices, consent are never affected).
"""
from __future__ import annotations

import logging
import os
import threading
from typing import Any

logger = logging.getLogger(__name__)

# Off-critical-path calls (2 per trip: one write, one read) — generous but bounded so a
# dead/slow memory server can never meaningfully stall a negotiation.
_DEFAULT_TIMEOUT_S = 5.0


def _run_coro_with_timeout(coro, timeout_s: float):
    """Run `coro` to completion on a dedicated thread with its own event loop, and
    return its result — or None if it doesn't finish within `timeout_s` or raises.

    Never blocks the caller past `timeout_s`: on timeout the worker thread is
    abandoned (daemon=True) rather than joined further.
    """
    box: dict[str, Any] = {}

    def worker() -> None:
        import asyncio
        try:
            box["v"] = asyncio.run(coro)
        except Exception as exc:  # noqa: BLE001 — surfaced to the caller via `box`
            box["e"] = exc

    t = threading.Thread(target=worker, daemon=True)
    t.start()
    t.join(timeout_s)
    if t.is_alive():
        logger.warning("memory_client: MCP call timed out after %.1fs (ignored)", timeout_s)
        return None
    if "e" in box:
        logger.warning("memory_client: MCP call failed (ignored): %s", box["e"])
        return None
    return box.get("v")


def _structured_content(result) -> dict | None:
    """Extract the tool's structuredContent dict from a CallToolResult, or None on
    any error/malformed shape (never a guess)."""
    if result is None or getattr(result, "isError", False):
        return None
    sc = getattr(result, "structuredContent", None)
    return sc if isinstance(sc, dict) else None


class MemoryClient:
    """Genuine MCP client for the travel_memory server's memory-loop tools.

    Every public method returns the tool's structuredContent dict, or None on ANY
    failure — NEVER raises (honest-degradation contract; see module docstring).
    """

    def __init__(
        self,
        *,
        url: str | None = None,
        embedded_server: Any = None,
        timeout_s: float = _DEFAULT_TIMEOUT_S,
    ) -> None:
        # Exactly one transport is expected to be configured by the factory below;
        # url takes precedence if (unusually) both are set.
        self._url = url
        self._embedded_server = embedded_server
        self._timeout_s = timeout_s

    # -- public tool calls --------------------------------------------------

    def log_search(self, *, user_id: str, query: dict, selection: dict | None) -> dict | None:
        """WRITE: mcp call_tool('log_search', {user_id, query, selection})."""
        return self._call_tool(
            "log_search", {"user_id": user_id, "query": query, "selection": selection}
        )

    def resolve_geographic_scope(self, *, prompt: str, user_id: str) -> dict | None:
        """READ: mcp call_tool('resolve_geographic_scope', {prompt, user_id})."""
        return self._call_tool(
            "resolve_geographic_scope", {"prompt": prompt, "user_id": user_id}
        )

    # -- transport ------------------------------------------------------------

    def _call_tool(self, name: str, arguments: dict) -> dict | None:
        try:
            return _run_coro_with_timeout(self._call_tool_async(name, arguments), self._timeout_s)
        except Exception as exc:  # noqa: BLE001 — memory is opt-in & off the var-0/money path
            logger.warning("memory_client: %s call failed (ignored): %s", name, exc)
            return None

    async def _call_tool_async(self, name: str, arguments: dict) -> dict | None:
        if self._url:
            from mcp.client.session import ClientSession
            from mcp.client.streamable_http import streamablehttp_client

            async with streamablehttp_client(self._url) as (read, write, _get_session_id):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    result = await session.call_tool(name, arguments)
                    return _structured_content(result)
        if self._embedded_server is not None:
            from mcp.shared.memory import create_connected_server_and_client_session

            async with create_connected_server_and_client_session(
                self._embedded_server
            ) as session:
                result = await session.call_tool(name, arguments)
                return _structured_content(result)
        return None  # no transport configured — should not happen (factory-gated)


# ---------------------------------------------------------------------------
# Factory — mirrors utils/emergency_feed.build_emergency_client(): env-gated, off by
# default. Returns None (seam fully bypassed, var-0 byte-identical) unless the
# operator opts in.
# ---------------------------------------------------------------------------

def build_memory_client() -> "MemoryClient | None":
    """MEMORY_MCP_URL set     -> out-of-process streamable-http client (demo/prod).
    MEMORY_MCP_EMBEDDED=1     -> in-process in-memory-transport client (tests / no
                                  extra service). Requires `mcp` installed AND the
                                  travel_memory FastMCP instance to have initialised
                                  (mcp_services.travel_memory.server._mcp is not None);
                                  falls back to None (honest no-op) otherwise.
    neither                  -> None (default; today's behavior, byte-identical).
    """
    url = os.getenv("MEMORY_MCP_URL", "").strip()
    if url:
        return MemoryClient(url=url)

    if os.getenv("MEMORY_MCP_EMBEDDED", "").strip().lower() in ("1", "true", "yes"):
        try:
            from mcp_services.travel_memory import server as _travel_memory_server
        except Exception as exc:  # noqa: BLE001 — mcp_services not importable → no-op
            logger.warning("memory_client: embedded mode unavailable (ignored): %s", exc)
            return None
        if _travel_memory_server._mcp is None:
            logger.warning(
                "memory_client: MEMORY_MCP_EMBEDDED set but mcp SDK is not installed "
                "(travel_memory server's FastMCP instance is None) — memory seam stays off"
            )
            return None
        return MemoryClient(embedded_server=_travel_memory_server._mcp._mcp_server)

    return None
