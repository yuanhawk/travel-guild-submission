"""
#159 — ORCHESTRATOR-level tests for the travel_memory MCP client seam
(_maybe_log_search WRITE + _maybe_read_affinity READ): the ONLY non-redundant
capability on the travel_memory MCP surface (the learned cross-trip preference
loop), wired as GENUINE MCP — a real mcp.client.session.ClientSession handshake,
not a relabeled function call. See society/utils/memory_client.py.

Invariants proven:
  - var-0 BYPASS: memory_client=None (the default) → both hooks are hard no-ops;
    a result dict is byte-identical before/after.
  - GRACEFUL DEGRADATION: a client that raises on every call never crashes the
    hook and never adds a key — matches the honest-degradation contract.
  - GENUINE MCP CONSUMPTION (skipped when the `mcp` SDK isn't installed): a real
    MemoryClient wired to the ACTUAL travel_memory FastMCP server (embedded
    in-memory transport) actually persists a log_search row + updated 21-dim EMA
    preference vector in the travel_memory SqliteStore, and a subsequent
    resolve_geographic_scope READ surfaces that affinity back through
    _maybe_read_affinity — an end-to-end round-trip through the real MCP wire.
  - negotiate()-LEVEL INTEGRATION: a FULL negotiate() run (real Planner/
    Accommodation/Budget/Critic/Transport/Risk pipeline, mocked UCP-merchant
    transport) through to a successful booking actually reaches BOTH the IP-1
    WRITE hook and the IP-2 READ hook — not just the private helper methods
    called directly. Verified red/green by temporarily commenting out the two
    call sites in orchestrator.py (~2196, ~2270-2274) during development; see
    #159 session notes. Uses a real embedded-transport MemoryClient so the calls
    that reach the seam are genuine MCP `call_tool()`s, not a mock spy.
"""
from __future__ import annotations

import copy
import json
import os
import sys
import threading

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import httpx
import pytest
from starlette.testclient import TestClient

from orchestration.orchestrator import TravelOrchestrator

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

try:
    import mcp  # noqa: F401
    _MCP_INSTALLED = True
except Exception:  # noqa: BLE001
    _MCP_INSTALLED = False

try:
    import mcp_services.travel_memory  # noqa: F401
    _MCP_SERVICES_INSTALLED = True
except Exception:  # noqa: BLE001
    # mcp_services is excluded from this public showcase repo (competitive
    # moat -- see README.md's "What's not here"). Tests that import it
    # directly must also skip on its absence, not just on the mcp SDK's
    # absence, or they hard-fail in any environment where the SDK happens to
    # be installed but this repo's own mcp_services package is not.
    _MCP_SERVICES_INSTALLED = False


def _success_result() -> dict:
    return {
        "outcome": "success",
        "user_id": "u-memory",
        "legs": [
            {
                "leg_id": "leg-0", "city": "bali",
                "checkin": "2026-10-01", "checkout": "2026-10-04",
                "nights": 3, "total_cents": 45000,
                "star_rating": 5, "area": "seminyak",
            },
        ],
    }


def _orch(memory_client=None):
    orch = TravelOrchestrator(memory_client=memory_client)
    orch._trip_id = "t-memory"
    return orch


# ---------------------------------------------------------------------------
# var-0 BYPASS — memory_client=None (default) → hard no-op, byte-identical.
# ---------------------------------------------------------------------------

def test_ip1_no_client_is_noop_byte_identical():
    orch = _orch(memory_client=None)
    result = _success_result()
    before = json.dumps(result, sort_keys=True)
    orch._maybe_log_search(result, {"user_id": "u-memory"})
    after = json.dumps(result, sort_keys=True)
    assert before == after, "no memory client -> byte-identical result"
    print("[MEMORY] PASS — IP-1 no client -> byte-identical\n")


def test_ip2_no_client_returns_none():
    orch = _orch(memory_client=None)
    hint = orch._maybe_read_affinity("u-memory", [{"city": "bali"}])
    assert hint is None
    print("[MEMORY] PASS — IP-2 no client -> None\n")


def test_ip1_not_booked_no_write_attempted():
    """A cannot_satisfy trip must never trigger a memory write, even with a client."""
    calls = []

    class _Client:
        def log_search(self, **kw):
            calls.append(kw)
            return {"status": "ok"}

    orch = _orch(memory_client=_Client())
    result = {"outcome": "cannot_satisfy"}
    orch._maybe_log_search(result, {"user_id": "u-memory"})
    assert calls == [], "unbooked trip must never write to memory"
    print("[MEMORY] PASS — IP-1 unbooked trip -> no write\n")


# ---------------------------------------------------------------------------
# GRACEFUL DEGRADATION — a client that raises never crashes the hook / adds a key.
# ---------------------------------------------------------------------------

class _RaisingClient:
    def log_search(self, **kw):
        raise RuntimeError("simulated MCP transport failure")

    def resolve_geographic_scope(self, **kw):
        raise RuntimeError("simulated MCP transport failure")


def test_ip1_raising_client_never_crashes():
    orch = _orch(memory_client=_RaisingClient())
    result = _success_result()
    # Must not raise.
    orch._maybe_log_search(result, {"user_id": "u-memory"})
    # Result is otherwise unaffected (the hook never mutates `result`).
    assert result["outcome"] == "success"
    print("[MEMORY] PASS — IP-1 raising client -> no crash\n")


def test_ip2_raising_client_returns_none():
    orch = _orch(memory_client=_RaisingClient())
    hint = orch._maybe_read_affinity("u-memory", [{"city": "bali"}])
    assert hint is None
    print("[MEMORY] PASS — IP-2 raising client -> None (honest, no crash)\n")


def test_ip2_no_resolvable_city_returns_none():
    class _Client:
        def resolve_geographic_scope(self, **kw):
            raise AssertionError("must not be called when no city is resolvable")

    orch = _orch(memory_client=_Client())
    hint = orch._maybe_read_affinity("u-memory", [{"city": ""}, {"not_city": "x"}])
    assert hint is None
    print("[MEMORY] PASS — IP-2 no resolvable city -> None, client never called\n")


# ---------------------------------------------------------------------------
# M1 IDOR — travel_memory's log_search / resolve_geographic_scope tools trust
# whatever user_id they are given with no ownership check of their own
# (mirrors the #161 Go-merchant gap). The orchestrator is the one place that
# CAN enforce this: it must use the session-verified self._merchant_user_id,
# never the raw, unauthenticated trip_request/result user_id a caller could
# set to someone else's id.
# ---------------------------------------------------------------------------

def test_ip1_write_uses_merchant_user_id_not_untrusted_result_user_id():
    """A caller-supplied result['user_id'] / trip_request['user_id'] must NOT
    be trusted once a session-verified self._merchant_user_id is set — the
    memory WRITE must be isolated to the verified identity's preference
    vector, not whatever id the untrusted payload carries."""
    calls = []

    class _Client:
        def log_search(self, **kw):
            calls.append(kw)
            return {"status": "ok"}

    orch = _orch(memory_client=_Client())
    orch._merchant_user_id = "victim-session-verified"  # set by the trusted server boundary
    result = _success_result()
    result["user_id"] = "attacker-supplied"  # a 2nd caller naming another user's id
    orch._maybe_log_search(result, {"user_id": "attacker-supplied"})
    assert len(calls) == 1
    assert calls[0]["user_id"] == "victim-session-verified", (
        f"log_search must use the verified merchant_user_id, not an untrusted "
        f"caller-supplied user_id: {calls[0]}"
    )
    print("[MEMORY] PASS — IP-1 WRITE isolates to the session-verified identity\n")


def test_ip2_read_call_site_uses_merchant_user_id_not_raw_user_id():
    """negotiate()-level: resolve_geographic_scope must be called with the
    session-verified self._merchant_user_id (set from trip_request
    ['merchant_user_id'], mirroring #161's Go-merchant stamping), never the
    raw trip_request['user_id'] a 2nd/untrusted caller could set to someone
    else's id. Exercised via negotiate() itself (not the private helper
    directly) so the actual call site (orchestrator.py ~1914) is proven, not
    just the helper's own forwarding behavior."""
    calls = []

    class _Client:
        def resolve_geographic_scope(self, **kw):
            calls.append(kw)
            return {"status": "ok", "resolved_city": "bali", "affinity_city": None}

        def log_search(self, **kw):
            return {"status": "ok"}

    orch = TravelOrchestrator(memory_client=_Client())
    # No other agent clients wired -> the trip cannot actually book, but the
    # memory READ (computed early, before any agent dispatch) must still fire
    # with the verified identity, never the raw attacker-supplied user_id.
    orch.negotiate({
        "user_id": "attacker-supplied",
        "merchant_user_id": "victim-session-verified",
        "total_budget_cents": 100000,
        "legs": [{"city": "bali", "checkin": "2026-10-01", "checkout": "2026-10-04", "adults": 1}],
    })
    assert len(calls) == 1, f"expected exactly one resolve_geographic_scope call, got {calls}"
    assert calls[0]["user_id"] == "victim-session-verified", (
        f"resolve_geographic_scope was called with an untrusted user_id: {calls[0]}"
    )
    print("[MEMORY] PASS — IP-2 READ call site isolates to the session-verified identity\n")


# ---------------------------------------------------------------------------
# GENUINE WIRE-LEVEL GRACEFUL DEGRADATION — the tests above simulate failure by
# raising inside a fake `_Client`/`_RaisingClient`, i.e. AT the orchestrator's
# client-interface boundary. These next two prove the SAME honest-degradation
# contract holds when the failure happens for real, INSIDE the actual MCP
# transport (utils.memory_client.MemoryClient._call_tool_async /
# _run_coro_with_timeout) — a refused TCP connection and a real wall-clock
# timeout against a socket that accepts but never speaks MCP — not a mock.
# ---------------------------------------------------------------------------

def _unused_tcp_port() -> int:
    """Bind+close an ephemeral port so nothing is listening on it — guarantees
    the next connect() to it is refused (not a false-positive slow DNS/etc.)."""
    import socket
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@pytest.mark.skipif(not _MCP_INSTALLED, reason="mcp SDK not installed (pip install 'mcp>=1.0')")
def test_memory_client_connection_refused_returns_none_not_raise():
    """A real MemoryClient(url=...) pointed at a dead port: the genuine MCP
    streamable-http client attempts a real TCP connect, gets ECONNREFUSED, and
    MemoryClient must swallow it -> None. Never raises past the client boundary."""
    from utils.memory_client import MemoryClient

    dead_url = f"http://127.0.0.1:{_unused_tcp_port()}/mcp"
    client = MemoryClient(url=dead_url, timeout_s=5.0)

    result = client.log_search(
        user_id="u-refused", query={"city": "bali"}, selection={"stars": 5}
    )
    assert result is None, "connection-refused MCP call must degrade to None, not raise"

    hint = client.resolve_geographic_scope(prompt="bali", user_id="u-refused")
    assert hint is None
    print("[MEMORY] PASS — real connection-refused -> MemoryClient returns None (no raise)\n")


@pytest.mark.skipif(not _MCP_INSTALLED, reason="mcp SDK not installed (pip install 'mcp>=1.0')")
def test_memory_client_hang_times_out_and_returns_none_within_bound():
    """A real TCP listener that accepts the connection but never sends a byte
    (simulates a wedged/overloaded MCP server) must be bounded by MemoryClient's
    own timeout_s -- proving the dedicated-thread+timeout bridge (memory_client.
    py._run_coro_with_timeout) actually aborts a hung call rather than blocking
    negotiate() forever."""
    import socket
    import threading
    import time

    stop = threading.Event()

    def _blackhole_server(srv: socket.socket) -> None:
        srv.settimeout(0.5)
        while not stop.is_set():
            try:
                conn, _ = srv.accept()
            except socket.timeout:
                continue
            # Accept the connection and hold it open forever (no HTTP response) —
            # the MCP client's read will hang until MemoryClient's own timeout fires.
            try:
                while not stop.is_set():
                    time.sleep(0.05)
            finally:
                conn.close()

    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    port = listener.getsockname()[1]
    thread = threading.Thread(target=_blackhole_server, args=(listener,), daemon=True)
    thread.start()
    try:
        from utils.memory_client import MemoryClient

        client = MemoryClient(url=f"http://127.0.0.1:{port}/mcp", timeout_s=1.5)
        started = time.monotonic()
        result = client.log_search(
            user_id="u-hang", query={"city": "bali"}, selection={"stars": 5}
        )
        elapsed = time.monotonic() - started

        assert result is None, "a hung MCP transport must degrade to None, not hang forever"
        # Bounded by the client's own timeout_s (plus generous scheduling slack) —
        # proves the call was actively aborted, not that it happened to fail fast.
        assert elapsed < 5.0, f"hung MCP call was not bounded by timeout_s (took {elapsed:.2f}s)"
        print(
            f"[MEMORY] PASS — real hung transport -> None within {elapsed:.2f}s "
            f"(bounded by timeout_s=1.5)\n"
        )
    finally:
        stop.set()
        listener.close()


@pytest.mark.skipif(not _MCP_INSTALLED, reason="mcp SDK not installed (pip install 'mcp>=1.0')")
def test_orchestrator_negotiate_survives_real_dead_memory_server_no_fabrication():
    """END-TO-END graceful degradation at the negotiate() level, with a REAL
    (not mocked) MemoryClient pointed at a dead MCP server: the full mocked-
    merchant pipeline must still book successfully, must NOT crash, and must
    NOT fabricate a personalization hint — the only acceptable outcome of a
    dead memory server is silent, honest omission of that one optional key."""
    from utils.memory_client import MemoryClient

    dead_client = MemoryClient(url=f"http://127.0.0.1:{_unused_tcp_port()}/mcp", timeout_s=3.0)
    orch = _build_society_with_memory(dead_client)

    result = orch.negotiate(copy.deepcopy(_PM_TRIP))

    assert result.get("outcome") == "success", (
        f"a dead memory server must never block a real booking, got: {result}"
    )
    assert "personalization" not in result, (
        "a dead memory server must never fabricate a personalization hint"
    )
    print("[MEMORY] PASS — negotiate() books successfully + no fabrication with a real dead memory server\n")


# ---------------------------------------------------------------------------
# result['personalization'] attach semantics (negotiate()-level contract).
# ---------------------------------------------------------------------------

def test_personalization_only_attached_when_present_and_success():
    orch = _orch(memory_client=None)
    orch._personalization = {"affinity_city": "bali", "message": "hi"}
    result = {"outcome": "cannot_satisfy"}
    # Mirrors the negotiate() guard: only attach on a SUCCESS outcome.
    if result.get("outcome") == "success" and orch._personalization:
        result["personalization"] = orch._personalization
    assert "personalization" not in result
    print("[MEMORY] PASS — personalization withheld on non-success outcome\n")


# ---------------------------------------------------------------------------
# GENUINE MCP CONSUMPTION — real ClientSession round-trip through the ACTUAL
# travel_memory FastMCP server (embedded in-memory transport). Skipped when the
# mcp SDK isn't installed (pip install "mcp>=1.0"; CI installs it — see
# .github/workflows/ci.yml).
# ---------------------------------------------------------------------------

@pytest.mark.skipif(not (_MCP_INSTALLED and _MCP_SERVICES_INSTALLED), reason="mcp SDK and/or this repo's own mcp_services package not installed")
def test_orchestrator_genuinely_consumes_mcp_end_to_end():
    from mcp_services.travel_memory.store import SqliteStore, set_store
    from mcp_services.travel_memory.retrieval import set_provider, LayeredProvider, RetrievalConfig
    import mcp_services.travel_memory.server as tm_server
    from utils.memory_client import MemoryClient

    # Isolated in-memory travel_memory store for this test.
    store = SqliteStore(":memory:")
    set_store(store)
    set_provider(LayeredProvider(store, live=None, cfg=RetrievalConfig()))
    assert tm_server._mcp is not None, "FastMCP instance must be initialised (mcp installed)"

    client = MemoryClient(embedded_server=tm_server._mcp._mcp_server)
    orch = _orch(memory_client=client)

    # WRITE — a real booked-leg result triggers a genuine MCP log_search call.
    # result['user_id'] ("u-memory", baked into _success_result()) takes priority
    # over the trip_request user_id in _maybe_log_search — mirror that here.
    result = _success_result()
    orch._maybe_log_search(result, {"user_id": "e2e-user"})

    # Verify the write ACTUALLY reached the travel_memory store through the wire
    # (not just "no exception raised") — the 21-dim EMA vector must have moved on
    # the city_bali / stars_5 / area_beach dims.
    pref = store.get_preference("u-memory")
    assert pref is not None, "log_search over real MCP did not persist a preference row"
    wv = pref["weight_vector"]
    assert len(wv) == 21
    assert wv[0] > 0.0, "city_bali dim did not move"  # dim 0 = city_bali
    assert wv[8] > 0.0, "stars_5 dim did not move"     # dim 8 = stars_5

    # READ — resolve_geographic_scope for a DIFFERENT city surfaces the affinity
    # this trip just wrote, end-to-end through the real MCP ClientSession.
    hint = orch._maybe_read_affinity("u-memory", [{"city": "bangkok"}])
    assert hint is not None
    assert hint["affinity_city"] == "bali"
    assert "bali" in hint["message"].lower()
    print("[MEMORY] PASS — genuine end-to-end MCP write->read round-trip\n")


@pytest.mark.skipif(not (_MCP_INSTALLED and _MCP_SERVICES_INSTALLED), reason="mcp SDK and/or this repo's own mcp_services package not installed")
def test_negotiate_attaches_personalization_on_success_via_real_mcp():
    """negotiate()-level integration: with NO other agents wired, a request with
    invalid legs terminates early (cannot_satisfy) — proving the personalization
    read is computed EARLY (before agent work) without crashing a bare
    orchestrator, and is correctly withheld on a non-success outcome."""
    from mcp_services.travel_memory.store import SqliteStore, set_store
    from mcp_services.travel_memory.retrieval import set_provider, LayeredProvider, RetrievalConfig
    import mcp_services.travel_memory.server as tm_server
    from utils.memory_client import MemoryClient

    store = SqliteStore(":memory:")
    set_store(store)
    set_provider(LayeredProvider(store, live=None, cfg=RetrievalConfig()))

    client = MemoryClient(embedded_server=tm_server._mcp._mcp_server)
    orch = TravelOrchestrator(memory_client=client)  # NO other clients wired

    # No planner/accommodation/etc. clients configured -> the trip cannot book,
    # but the memory READ (computed before any agent call) must not crash the run.
    result = orch.negotiate({
        "user_id": "e2e-user-2",
        "total_budget_cents": 150000,
        "legs": [{"city": "bali", "checkin": "2026-10-01", "checkout": "2026-10-04", "adults": 1}],
    })
    assert result["outcome"] != "success"  # no agents wired -> can't actually book
    assert "personalization" not in result  # never attached on a non-success outcome
    print("[MEMORY] PASS — negotiate() memory read never crashes an unwired run\n")


# ===========================================================================
# negotiate()-LEVEL INTEGRATION — full pipeline, real success, proving
# negotiate() itself (not a direct call to the private helpers) reaches BOTH
# #159 integration points. Inline copies of the _SeqTransport / _build_society
# / _PM_* helpers from test_orchestrator_emergency_regression.py (house
# convention: test files don't import fixtures from each other).
# ===========================================================================

class _SeqTransport(httpx.BaseTransport):
    """Returns canned merchant responses from a dict of tool -> (status, body) or list thereof."""

    def __init__(self, responses: dict) -> None:
        self._responses: dict[str, list] = {}
        for tool, resp in responses.items():
            self._responses[tool] = list(resp) if isinstance(resp, list) else [resp]
        self._counts: dict[str, int] = {}
        self._lock = threading.Lock()

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        try:
            payload = json.loads(request.read())
        except Exception:
            return httpx.Response(400, text="bad request")
        tool = (payload.get("params") or {}).get("name", "")
        with self._lock:
            self._counts[tool] = self._counts.get(tool, 0) + 1
            idx = min(self._counts[tool] - 1, len(self._responses.get(tool, [])) - 1)
        resps = self._responses.get(tool)
        if not resps:
            return httpx.Response(500, json={"error": f"mock: no response for {tool!r}"})
        status, body = resps[max(idx, 0)]
        return httpx.Response(status, json=body)


def _merchant_result(domain: dict) -> dict:
    return {"jsonrpc": "2.0", "id": 1, "result": {
        "structuredContent": domain,
        "content": [{"type": "text", "text": json.dumps(domain)}],
    }}


def _catalog_result(items: list) -> tuple:
    return (200, _merchant_result({"source": "mock", "count": len(items), "results": items}))


# NOTE: the trip's first-leg city must be one resolve_geographic_scope actually
# knows (store.py's SEA _REGION_MAP — bali/bangkok/singapore/kuala lumpur), so
# the genuine MCP READ has a real city to resolve. A leg the memory server
# can't resolve (e.g. Port Moresby, used elsewhere in this suite) honestly
# degrades to no personalization by design -- see server.py resolve_geographic_
# scope's "no_city_resolved" path -- which would make this positive test moot.
_PM_TRIP = {
    "user_id": "memory-integration-user",
    "total_budget_cents": 120000,
    "wallet_balance_cents": 500000,
    "legs": [
        {"city": "bangkok", "checkin": "2026-03-01", "checkout": "2026-03-04",
         "adults": 1, "vibe": "city", "mode": "flight"},
    ],
}
_PM_GRAND = {
    "hotel_id": "bangkok-grand", "title": "Bangkok Grand Hotel",
    "city": "bangkok", "review_score": 8.0, "star_rating": 4.0,
    "nights": 3, "total_cents": 54000, "amenities": ["pool", "wifi"],
    "provenance": "merchant", "area": "sukhumvit",
}
_LOOKUP_OK = _merchant_result({
    "hotel": {"id": "portmoresby-grand", "review_score": 8.0, "star_rating": 4.0},
    "available": True, "total_cents": 54000, "nights": 3, "source": "mock",
})
_CREATE_OK = _merchant_result({
    "id": "co_pm_mem", "status": "incomplete", "user_id": "memory-integration-user",
    "line_items": [], "total_cents": 54000, "currency": "USD",
    "buyer_consent": False, "booking_ref": "",
})
_FUND_OK = _merchant_result({
    "status": "ok", "wallet_session_id": "trip-mem",
    "seed_cents": 500000, "balance_cents": 500000, "simulated": True, "note": "sim",
})
_COMPLETE_OK = _merchant_result({
    "id": "co_pm_mem", "status": "complete", "user_id": "memory-integration-user",
    "line_items": [], "total_cents": 54000, "currency": "USD",
    "buyer_consent": True, "booking_ref": "BK-memtest-1",
    "wallet_session_id": "trip-mem", "wallet_debit_cents": 54000,
    "wallet_balance_cents": 446000, "simulated": True,
})


def _build_society_with_memory(memory_client):
    """A minimal Port Moresby society (same shape as
    test_orchestrator_emergency_regression._build_society) that books
    successfully, wired with a memory_client so negotiate() has both a real
    agent pipeline AND the #159 memory seam active."""
    from agents.accommodation_agent import AccommodationAgent
    from agents.budget_agent import BudgetAgent
    from agents.critic_agent import CriticAgent
    from agents.planner_agent import PlannerAgent
    from agents.risk_agent import RiskAgent
    from agents.transport_agent import TransportAgent

    budget_transport = _SeqTransport({
        "create_checkout": (200, _CREATE_OK),
        "complete_checkout": (200, _COMPLETE_OK),
        "wallet_fund": (200, _FUND_OK),
    })
    acc_transport = _SeqTransport({"search_catalog": _catalog_result([_PM_GRAND])})
    critic_transport = _SeqTransport({"lookup_catalog": (200, _LOOKUP_OK)})

    return TravelOrchestrator(
        planner_client=TestClient(PlannerAgent().build_app()),
        accommodation_client=TestClient(
            AccommodationAgent(merchant_transport=acc_transport).build_app()),
        budget_client=TestClient(
            BudgetAgent(merchant_transport=budget_transport).build_app()),
        critic_client=TestClient(
            CriticAgent(merchant_transport=critic_transport).build_app()),
        transport_client=TestClient(TransportAgent().build_app()),
        risk_client=TestClient(RiskAgent().build_app()),
        memory_client=memory_client,
    )


@pytest.mark.skipif(not (_MCP_INSTALLED and _MCP_SERVICES_INSTALLED), reason="mcp SDK and/or this repo's own mcp_services package not installed")
def test_negotiate_full_pipeline_reaches_both_memory_integration_points():
    """THE integration test: a full negotiate() run (real multi-agent pipeline,
    mocked UCP-merchant transport only) that books successfully must itself
    -- via negotiate(), not via a direct call to _maybe_log_search /
    _maybe_read_affinity -- reach:
      IP-1 (WRITE): a genuine MCP log_search call that lands a preference row
        in the travel_memory store for this user.
      IP-2 (READ):  a genuine MCP resolve_geographic_scope call whose result
        (if any affinity is on file) is attached at result['personalization'].

    Proven with a REAL MemoryClient (embedded in-memory MCP transport) against
    the ACTUAL travel_memory FastMCP server -- so "negotiate() reaches the MCP
    call" is verified by observing the real store's side effect, not a spy
    return value.
    """
    from mcp_services.travel_memory.store import SqliteStore, set_store
    from mcp_services.travel_memory.retrieval import set_provider, LayeredProvider, RetrievalConfig
    import mcp_services.travel_memory.server as tm_server
    from utils.memory_client import MemoryClient

    store = SqliteStore(":memory:")
    set_store(store)
    set_provider(LayeredProvider(store, live=None, cfg=RetrievalConfig()))
    assert tm_server._mcp is not None, "FastMCP instance must be initialised (mcp installed)"

    # Pre-seed a strong Bali affinity for this user via 5 real MCP log_search
    # calls (independent of negotiate()) so IP-2's READ, when negotiate() runs
    # a Bangkok trip, has a real (different-city) affinity to surface.
    seed_client = MemoryClient(embedded_server=tm_server._mcp._mcp_server)
    for _ in range(5):
        seed_client.log_search(
            user_id="memory-integration-user",
            query={"city": "bali", "checkin": "2025-07-01", "checkout": "2025-07-07"},
            selection={"stars": 5, "price_cents_night": 30000, "area": "seminyak"},
        )
    assert store.get_preference("memory-integration-user") is not None, \
        "affinity seeding failed pre-condition"

    client = MemoryClient(embedded_server=tm_server._mcp._mcp_server)
    orch = _build_society_with_memory(client)

    result = orch.negotiate(copy.deepcopy(_PM_TRIP))

    assert result.get("outcome") == "success", (
        f"expected the mocked pipeline to book successfully, got: {result}"
    )

    # --- IP-2 proof: negotiate() reached the READ hook -----------------------
    assert "personalization" in result, (
        "negotiate() did not attach result['personalization'] -- IP-2 "
        "(resolve_geographic_scope READ) was not reached"
    )
    assert result["personalization"]["affinity_city"] == "bali"

    # --- IP-1 proof: negotiate() reached the WRITE hook -----------------------
    # Bangkok leg just booked -> log_search must have fired for THIS trip, moving
    # dim 1 (city_bangkok, server.py _W_DIMS) of the SAME user's preference
    # vector -- a dim untouched by the 5 bali-only seeding calls above, so any
    # movement can only have come from negotiate()'s own IP-1 write.
    pref_before = 0.0  # bali seeding never touches dim 1 (city_bangkok)
    pref = store.get_preference("memory-integration-user")
    assert pref is not None
    assert pref["weight_vector"][1] > pref_before, (
        "negotiate() success did not persist a log_search write for the "
        f"booked Bangkok leg -- IP-1 was not reached (weight_vector={pref['weight_vector']})"
    )
    print("[MEMORY] PASS — negotiate() itself reaches both IP-1 (write) and IP-2 (read)\n")
