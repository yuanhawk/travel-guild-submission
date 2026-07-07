"""
test_inv_server_sse_happypath.py — Server SSE happy-path + var-0 invariant test.

Money-safety invariant tested:
  A POST /negotiate with a bookable trip MUST result in a well-formed SSE stream
  that:
    (a) starts with a negotiate_started event (the orchestrator announced it began);
    (b) ends with a negotiate_finished terminal event carrying a structured result
        with a non-empty outcome field (no silent drop/hang/truncation);
    (c) the result carries a non-empty booking_ref and a positive total_cents on a
        success outcome (the charged amount is present and non-zero — money-safety);
    (d) var-0: two identical requests through the SAME server app instance produce
        the SAME terminal outcome AND the SAME total_cents (deterministic charge).

Architecture: Starlette TestClient + server.build_app() (self-contained
_LocalCatalogTransport in-process, LLM-off). No live network, no paid APIs.

Reference:
  - server.py: build_app(), negotiate(), stream() routes
  - e2e/tests/demo-flow.spec.js: "structured negotiate -> SSE renders a successful
    booking" (the JS does this via the browser)
  - tests/test_stream_backpressure.py: TestClient + server.build_app() pattern
  - tests/test_composition_wiring.py: society wiring; _LocalCatalogTransport mirrors
    _build_orch() in server.py
"""
from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import pytest
from starlette.testclient import TestClient

from orchestration import server

# ---------------------------------------------------------------------------
# Shared fixture: one server app instance for the whole module.
# server.build_app() spins up the full society (_build_orch) via _lifespan;
# re-using it across tests is safe because each test mints a fresh stream_id.
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def app_client():
    """Start the Starlette app (lifespan) once for all tests in this module."""
    app = server.build_app()
    with TestClient(app, raise_server_exceptions=True) as client:
        yield client


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _drain_sse(client: TestClient, stream_id: str) -> list[dict]:
    """
    Drain the SSE stream for stream_id, collecting all events until either:
      - a 'negotiate_finished' event is received (the terminal sentinel), or
      - a 'timeout' event is received (server-side STREAM_TIMEOUT_S elapsed), or
      - the stream closes.

    Returns the list of parsed event dicts in order.
    """
    events: list[dict] = []
    with client.stream("GET", f"/stream/{stream_id}") as resp:
        assert resp.status_code == 200, (
            f"GET /stream/{stream_id} returned {resp.status_code}"
        )
        for raw_line in resp.iter_lines():
            if not raw_line.startswith("data:"):
                continue
            payload = raw_line[len("data:"):].strip()
            if not payload:
                continue
            ev = json.loads(payload)
            events.append(ev)
            if ev.get("type") in (server.SENTINEL_TYPE, "timeout"):
                break
    return events


# ---------------------------------------------------------------------------
# The bookable trip fixture (Bali 3 nights, comfortable budget, no gates).
# Bali: no visa/health/compliance gate fires; cheap catalog hotels available.
# today=fixed so the var-0 window is deterministic (not wall-clock).
# ---------------------------------------------------------------------------
_BOOKABLE_TRIP = {
    "user_id": "inv-sse-happypath",
    "total_budget_cents": 100000,   # $1000; cheapest Bali hotel ~$22/night -> fits
    "today": "2026-10-01",
    "legs": [
        {
            "city": "bali",
            "checkin": "2026-10-01",
            "checkout": "2026-10-04",
            "adults": 1,
            "vibe": "beach",
        }
    ],
}


# ---------------------------------------------------------------------------
# Test 1: negotiate -> stream_id returned, stream drains to negotiate_finished
# ---------------------------------------------------------------------------

def test_sse_happypath_terminal_event(app_client: TestClient) -> None:
    """
    POST /negotiate with a bookable trip MUST return a stream_id; GET /stream
    MUST produce an SSE sequence that:
      - contains at least two events (not an empty/immediate close);
      - starts with negotiate_started (announced to the client);
      - ends with negotiate_finished (terminal sentinel emitted, not dropped);
      - the terminal event carries a 'result' dict with a non-None 'outcome';
      - on a success outcome the result carries a positive total_cents
        (the amount charged is disclosed, not zero or absent -- money safety).
    """
    r = app_client.post("/negotiate", json=_BOOKABLE_TRIP)
    assert r.status_code == 200, f"POST /negotiate failed: {r.status_code} {r.text}"
    body = r.json()
    assert "stream_id" in body, f"negotiate response missing stream_id: {body}"
    assert body.get("status") == "negotiating", (
        f"Expected status='negotiating', got {body.get('status')!r}"
    )

    events = _drain_sse(app_client, body["stream_id"])

    # Must not be empty — at minimum negotiate_started + negotiate_finished.
    assert len(events) >= 2, (
        f"Expected at least 2 SSE events, got {len(events)}: {[e.get('type') for e in events]}"
    )

    # First event must be negotiate_started (orchestrator announced it began).
    assert events[0].get("type") == "negotiate_started", (
        f"First SSE event must be 'negotiate_started', got {events[0].get('type')!r}; "
        f"all types: {[e.get('type') for e in events]}"
    )

    # Last event must be the terminal negotiate_finished (no truncation/hang).
    assert events[-1].get("type") == server.SENTINEL_TYPE, (
        f"Last SSE event must be '{server.SENTINEL_TYPE}', got {events[-1].get('type')!r}; "
        f"all types: {[e.get('type') for e in events]}"
    )

    terminal = events[-1]
    result = terminal.get("result")
    assert isinstance(result, dict), (
        f"negotiate_finished event must carry a 'result' dict; got: {result!r}"
    )
    outcome = result.get("outcome")
    assert outcome in ("success", "cannot_satisfy"), (
        f"result.outcome must be one of 'success'/'cannot_satisfy'; got {outcome!r}"
    )

    # Money-safety: on a success the charged amount must be present and positive.
    if outcome == "success":
        total_cents = result.get("total_cents")
        assert isinstance(total_cents, (int, float)) and total_cents > 0, (
            f"Success result must carry a positive total_cents (charge disclosed); "
            f"got total_cents={total_cents!r}"
        )
        booking_ref = result.get("booking_ref")
        assert booking_ref and str(booking_ref).strip(), (
            f"Success result must carry a non-empty booking_ref; got {booking_ref!r}"
        )
        print(
            f"[SSE-HAPPYPATH] PASS  outcome=success  "
            f"total_cents={total_cents}  booking_ref={booking_ref!r}  "
            f"events={len(events)}"
        )
    else:
        # cannot_satisfy is also a legitimate terminal; the invariant only requires
        # that the stream reaches a terminal event (no hang / silent drop).
        print(
            f"[SSE-HAPPYPATH] PASS  outcome={outcome}  "
            f"(cannot_satisfy is a legitimate terminal)  events={len(events)}"
        )


# ---------------------------------------------------------------------------
# Test 2: var-0 — two identical requests produce identical terminal outcomes
# ---------------------------------------------------------------------------

def test_sse_var0_identical_requests_same_terminal_outcome(
    app_client: TestClient,
) -> None:
    """
    var-0 money-safety invariant: two POST /negotiate calls with IDENTICAL trip
    payloads (same user_id, budget, dates, city) MUST produce the SAME terminal
    outcome AND the SAME total_cents through the SSE stream.

    This proves the server SSE path is deterministic: no wall-clock, no random
    charge-amount drift, no outcome flip between identical bookings.

    booking_ref nonce values are explicitly NOT compared (they contain the
    checkout-sequence counter which legitimately increments per-call).
    """

    def _negotiate_and_drain(trip: dict) -> dict:
        r = app_client.post("/negotiate", json=trip)
        assert r.status_code == 200, f"POST /negotiate failed: {r.status_code}"
        stream_id = r.json()["stream_id"]
        events = _drain_sse(app_client, stream_id)
        terminal = next(
            (e for e in events if e.get("type") == server.SENTINEL_TYPE), None
        )
        assert terminal is not None, (
            f"No negotiate_finished event in stream; "
            f"types: {[e.get('type') for e in events]}"
        )
        return terminal["result"]

    res1 = _negotiate_and_drain(_BOOKABLE_TRIP)
    res2 = _negotiate_and_drain(_BOOKABLE_TRIP)

    # Outcome must be identical (success or cannot_satisfy — not a flip).
    assert res1.get("outcome") == res2.get("outcome"), (
        f"var-0 violated: outcome changed between identical runs; "
        f"run-1={res1.get('outcome')!r}  run-2={res2.get('outcome')!r}"
    )

    # For success: the CHARGED AMOUNT must be byte-identical (no drift).
    if res1.get("outcome") == "success":
        assert res1.get("total_cents") == res2.get("total_cents"), (
            f"var-0 violated: total_cents changed between identical runs; "
            f"run-1={res1.get('total_cents')}  run-2={res2.get('total_cents')}"
        )
        # The selected hotel must also be the same (deterministic planner/DP).
        hotel1 = (res1.get("legs") or [{}])[0].get("hotel_id")
        hotel2 = (res2.get("legs") or [{}])[0].get("hotel_id")
        assert hotel1 == hotel2, (
            f"var-0 violated: hotel selection changed between identical runs; "
            f"run-1={hotel1!r}  run-2={hotel2!r}"
        )
        print(
            f"[VAR-0] PASS  outcome=success  "
            f"total_cents={res1.get('total_cents')}  hotel_id={hotel1!r}"
        )
    else:
        print(f"[VAR-0] PASS  outcome={res1.get('outcome')!r} (deterministic cannot_satisfy)")


# ---------------------------------------------------------------------------
# HIGH regression: spurious 120s "timeout" while the negotiation is still
# genuinely running (queued behind orch_lock / the shared executor, or a slow
# LLM-on step).
#
# STREAM_TIMEOUT_S is a PER-EVENT wait. Before the fix, a single silent wait
# (no events at all — e.g. a request still queued behind a long negotiation)
# produced an immediate, terminal client-visible "timeout" frame, even though
# the negotiation's worker kept running to completion and would persist its
# result into a now-orphaned queue nobody reads. The fix tolerates
# STREAM_MAX_SILENT_WAITS-1 consecutive silent waits with a non-fatal
# "keep_alive" frame before declaring a genuine terminal "timeout".
# ---------------------------------------------------------------------------

def test_sse_stream_emits_keep_alive_before_terminal_timeout():
    """A stream_id with a queue that NEVER receives an event (simulating a
    negotiation queued/still-running with no events yet) must emit
    (STREAM_MAX_SILENT_WAITS - 1) non-fatal 'keep_alive' frames before the
    genuine terminal 'timeout' frame — not an immediate timeout on the very
    first silent wait."""
    import asyncio
    import time as _time

    app = server.build_app()
    orig_timeout = server.STREAM_TIMEOUT_S
    orig_max_waits = server.STREAM_MAX_SILENT_WAITS
    server.STREAM_TIMEOUT_S = 0.05          # tiny, for a fast test
    server.STREAM_MAX_SILENT_WAITS = 3
    try:
        with TestClient(app) as client:
            stream_id = "test-keepalive-orphan-stream-0001"
            server._state.queues[stream_id] = asyncio.Queue()
            server._state.queue_ts[stream_id] = _time.time()

            events = []
            with client.stream("GET", f"/stream/{stream_id}") as resp:
                assert resp.status_code == 200
                for raw_line in resp.iter_lines():
                    if not raw_line.startswith("data:"):
                        continue
                    payload = raw_line[len("data:"):].strip()
                    if not payload:
                        continue
                    ev = json.loads(payload)
                    events.append(ev)
                    if ev.get("type") == "timeout":
                        break

            types = [e.get("type") for e in events]
            expected = ["keep_alive"] * (server.STREAM_MAX_SILENT_WAITS - 1) + ["timeout"]
            assert types == expected, (
                f"REGRESSION: expected {server.STREAM_MAX_SILENT_WAITS - 1} "
                f"keep_alive frame(s) then one terminal timeout, got {types!r} "
                f"— a still-running (merely queued) negotiation would be "
                f"reported as failed too early."
            )
            # The queue must be cleaned up after the genuine terminal timeout.
            assert stream_id not in server._state.queues
    finally:
        server.STREAM_TIMEOUT_S = orig_timeout
        server.STREAM_MAX_SILENT_WAITS = orig_max_waits


def test_sse_stream_recovers_after_a_keep_alive_if_an_event_arrives():
    """Sanity counterpart: if a REAL event arrives after one or more silent
    waits (keep_alive frames), the stream must deliver it normally and reset
    the silent-wait counter — the fix must not force extra keep_alives once
    genuine data starts flowing."""
    import asyncio
    import threading
    import time as _time

    app = server.build_app()
    orig_timeout = server.STREAM_TIMEOUT_S
    orig_max_waits = server.STREAM_MAX_SILENT_WAITS
    server.STREAM_TIMEOUT_S = 0.05
    server.STREAM_MAX_SILENT_WAITS = 3
    try:
        with TestClient(app) as client:
            stream_id = "test-keepalive-recovers-stream-0001"
            q = asyncio.Queue()
            server._state.queues[stream_id] = q
            server._state.queue_ts[stream_id] = _time.time()

            def _push_after_delay():
                # Wait long enough for exactly one silent wait (keep_alive) to
                # fire, then push a real terminal event onto the SAME loop the
                # portal is running on.
                _time.sleep(server.STREAM_TIMEOUT_S * 1.5)
                server._state.loop.call_soon_threadsafe(
                    q.put_nowait,
                    {"type": server.SENTINEL_TYPE, "result": {"outcome": "success"}},
                )

            threading.Thread(target=_push_after_delay).start()

            events = []
            with client.stream("GET", f"/stream/{stream_id}") as resp:
                assert resp.status_code == 200
                for raw_line in resp.iter_lines():
                    if not raw_line.startswith("data:"):
                        continue
                    payload = raw_line[len("data:"):].strip()
                    if not payload:
                        continue
                    ev = json.loads(payload)
                    events.append(ev)
                    if ev.get("type") in (server.SENTINEL_TYPE, "timeout"):
                        break

            types = [e.get("type") for e in events]
            assert server.SENTINEL_TYPE in types, (
                f"real event never delivered — got {types!r}"
            )
            assert "timeout" not in types, (
                f"a real event arrived but the stream still declared a "
                f"terminal timeout — got {types!r}"
            )
            assert types[0] == "keep_alive", (
                "expected the first silent wait to produce a keep_alive frame"
            )
    finally:
        server.STREAM_TIMEOUT_S = orig_timeout
        server.STREAM_MAX_SILENT_WAITS = orig_max_waits


if __name__ == "__main__":
    import pytest as _pytest
    _pytest.main([__file__, "-v", "-p", "no:xdist", "-p", "no:cacheprovider"])
