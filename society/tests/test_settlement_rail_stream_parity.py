"""
test_settlement_rail_stream_parity.py — PR #12 adversarial audit, S1/Finding1.

BUG (CRITICAL, shipped): POST /negotiate_text has TWO call sites into
intent_parser.negotiate_from_text — the streaming worker (_run_text_stream,
taken whenever body["stream"] is truthy) and the blocking worker
(_run_text_negotiate). Only the BLOCKING one forwarded settlement_rail. The
real web UI (web/src/lib/planStream.ts) always POSTs {stream:true} first and
only degrades to {stream:false} on failure, so the Circle USDC opt-in was a
no-op on the ONLY path users actually take.

This test pins BOTH branches: whatever the client opts into must reach
negotiate_from_text identically, streaming or not.
"""
from __future__ import annotations

import os
import sys
import threading

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from unittest.mock import patch

from starlette.testclient import TestClient

from orchestration import server as server_mod

_TEXT = "7 days in bali, beach, $3000"
# Deliberately NOT a plan_ready outcome: _persist_and_sanitize_plan early-returns
# for every other outcome, so neither branch touches the trips store here.
_FAKE_RESULT = {
    "outcome": "success",
    "legs": [],
    "trip_id": "trip-fake-rail",
    "booking_ref": "BK-fake-rail",
}


def _client() -> TestClient:
    """Fresh app per test (lifespan builds _state.orch/_state.executor)."""
    return TestClient(server_mod.build_app(), raise_server_exceptions=True)


def test_blocking_branch_threads_settlement_rail() -> None:
    """{stream:false} (the degraded / direct-caller path) forwards the rail."""
    captured: list[dict] = []

    def _fake(*args, **kwargs):
        captured.append(kwargs)
        return dict(_FAKE_RESULT)

    with _client() as c:
        with patch("utils.intent_parser.negotiate_from_text", side_effect=_fake):
            r = c.post("/negotiate_text", json={
                "text": _TEXT,
                "user_id": "circle-rail-blocking",
                "settlement_rail": "circle_usdc",
            })
    assert r.status_code == 200, r.text
    assert len(captured) == 1, f"expected exactly one parse call, got {len(captured)}"
    assert captured[0].get("settlement_rail") == "circle_usdc", (
        "the BLOCKING /negotiate_text branch dropped settlement_rail: "
        f"{captured[0]!r}"
    )


def test_streaming_branch_threads_settlement_rail() -> None:
    """{stream:true} — the path the real web UI ALWAYS takes first — must
    forward the rail identically to the blocking branch (S1 regression guard)."""
    captured: list[dict] = []
    called = threading.Event()

    def _fake(*args, **kwargs):
        captured.append(kwargs)
        called.set()
        return dict(_FAKE_RESULT)

    with _client() as c:
        with patch("utils.intent_parser.negotiate_from_text", side_effect=_fake):
            r = c.post("/negotiate_text", json={
                "text": _TEXT,
                "user_id": "circle-rail-stream",
                "settlement_rail": "circle_usdc",
                "stream": True,
            })
            assert r.status_code == 200, r.text
            assert r.json().get("stream_id"), f"no stream_id returned: {r.text}"
            # The parse runs on _state.executor — wait for the worker INSIDE the
            # patch scope so the spy is still installed when it fires.
            assert called.wait(timeout=30), (
                "the streaming worker never called negotiate_from_text"
            )

    assert len(captured) == 1, f"expected exactly one parse call, got {len(captured)}"
    assert captured[0].get("settlement_rail") == "circle_usdc", (
        "the STREAMING /negotiate_text branch dropped settlement_rail — the Circle "
        f"toggle is a no-op on the real UI path: {captured[0]!r}"
    )
