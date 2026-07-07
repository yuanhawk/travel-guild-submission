"""
test_m1_memory_identity_followup.py — follow-up on the M1 IDOR fix.

FINDING (informational, non-blocking): merchant_user_id (#161) mirrors
_authorize_trip_action's TIER decision only -- it is NOT session-verified at
plan-creation time for a tier-1 (logged-in) user_id claim (real verification
happens later, at /confirm). That's fine for checkout (re-verified there
before anything irreversible happens) but the travel_memory PERSONALIZATION
channel (_maybe_log_search WRITE / _maybe_read_affinity READ) fires at
negotiate() time with no later gate, so a caller who merely knows a victim's
user_id string could steer that trip's preference-vector write to the
victim's row (display-only, cosmetic -- never ranking/price/booking -- but
still a real cross-user write).

FIX: server.py._memory_verified_user_id() computes a SEPARATE, properly-gated
identity for that one channel: the tier-2 anon hash is already unforgeable so
it always passes through; a tier-1 (named user_id) claim is trusted only when
a live session_token proves it, else "" (denial). Threaded as
trip_request["memory_verified_user_id"]; the orchestrator's
self._memory_user_id (None when absent -> old merchant_user_id-based
fallback, byte-identical for direct/test callers) consumes it, and a "" denial
must NEVER fall through to any untrusted value.

Covers:
  1. _memory_verified_user_id() unit tests (anon passthrough / tier-1 no-token
     denial / tier-1 wrong-token denial / tier-1 valid-token pass).
  2. Orchestrator wiring: explicit "" denial isolates the WRITE/READ call
     sites (never falls through to result/trip_request's untrusted user_id).
  3. Orchestrator wiring: an absent key (direct/test callers) preserves the
     OLD self._merchant_user_id-based behavior, byte-identical.
  4. End-to-end through the REAL POST /negotiate and POST /negotiate_text
     endpoints: the trip_request actually dispatched carries the correctly
     gated memory_verified_user_id for anon / unverified tier-1 / verified
     tier-1 callers.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from unittest.mock import patch

from orchestration.orchestrator import TravelOrchestrator
from orchestration.server import _memory_verified_user_id
from orchestration.store import SqliteDashboardStore, set_store
from utils.session_token import issue_session_token


# ===========================================================================
# 1. _memory_verified_user_id() — pure unit tests
# ===========================================================================

def test_anonymous_claim_passes_merchant_user_id_through():
    """No user_id claimed -> the tier-2 anon hash (or "") is already
    unforgeable, so it always passes through unchanged."""
    got = _memory_verified_user_id({"owner_token": "tok"}, "anon:deadbeef")
    assert got == "anon:deadbeef"


def test_tier1_claim_with_no_session_token_is_denied():
    """A named user_id with NO session_token at all -> denied ("")."""
    got = _memory_verified_user_id({"user_id": "victim"}, "victim")
    assert got == "", "unverified tier-1 claim must be denied, not trusted"


def test_tier1_claim_with_wrong_session_token_is_denied():
    """A session_token that verifies for a DIFFERENT user -> denied."""
    attacker_token = issue_session_token("attacker")
    got = _memory_verified_user_id(
        {"user_id": "victim", "session_token": attacker_token}, "victim"
    )
    assert got == "", "a token bound to a different user must not launder the claim"


def test_tier1_claim_with_valid_session_token_passes():
    """A live session_token that genuinely proves the claimed user_id ->
    the identity is trusted for memory purposes."""
    real_token = issue_session_token("demo-lena")
    got = _memory_verified_user_id(
        {"user_id": "demo-lena", "session_token": real_token}, "demo-lena"
    )
    assert got == "demo-lena"


def test_non_string_session_token_does_not_crash():
    """A malformed (non-string) session_token must degrade to denial, never raise."""
    got = _memory_verified_user_id({"user_id": "victim", "session_token": 12345}, "victim")
    assert got == ""


# ===========================================================================
# 2 & 3. Orchestrator wiring — self._memory_user_id gating semantics
# ===========================================================================

def _success_result(user_id: str = "attacker-supplied") -> dict:
    return {
        "outcome": "success",
        "user_id": user_id,
        "legs": [
            {"leg_id": "leg-0", "city": "bali", "checkin": "2026-10-01",
             "checkout": "2026-10-04", "nights": 3, "total_cents": 45000,
             "star_rating": 5, "area": "seminyak"},
        ],
    }


class _RecordingClient:
    def __init__(self):
        self.log_search_calls: list[dict] = []
        self.resolve_calls: list[dict] = []

    def log_search(self, **kw):
        self.log_search_calls.append(kw)
        return {"status": "ok"}

    def resolve_geographic_scope(self, **kw):
        self.resolve_calls.append(kw)
        return {"status": "ok", "resolved_city": "bali", "affinity_city": None}


def test_explicit_denial_does_not_fall_through_to_untrusted_result_user_id():
    """self._memory_user_id == "" (server-computed denial) must isolate the
    WRITE -- it must NOT fall back to self._merchant_user_id or the untrusted
    result/trip_request user_id an attacker controls."""
    client = _RecordingClient()
    orch = TravelOrchestrator(memory_client=client)
    orch._trip_id = "t-m1-followup"
    orch._merchant_user_id = "victim"   # would be trusted under the OLD (pre-followup) chain
    orch._memory_user_id = ""           # server-side denial (explicit, not absent)
    result = _success_result(user_id="attacker-supplied")
    orch._maybe_log_search(result, {"user_id": "attacker-supplied"})
    assert len(client.log_search_calls) == 1
    assert client.log_search_calls[0]["user_id"] == "", (
        f"an explicit denial must isolate the write to '', never fall through "
        f"to self._merchant_user_id or an untrusted claim: {client.log_search_calls[0]}"
    )


def test_explicit_denial_isolates_the_read_call_site_too():
    client = _RecordingClient()
    orch = TravelOrchestrator(memory_client=client)
    orch._trip_id = "t-m1-followup-read"
    orch._merchant_user_id = "victim"
    orch._memory_user_id = ""
    hint = orch._maybe_read_affinity(
        orch._memory_user_id if orch._memory_user_id is not None else orch._merchant_user_id,
        [{"city": "bali"}],
    )
    assert len(client.resolve_calls) == 1
    assert client.resolve_calls[0]["user_id"] == ""
    assert hint is None  # no affinity on file for the "" bucket


def test_absent_key_falls_back_to_old_merchant_user_id_behavior():
    """Direct/test callers that never set self._memory_user_id (still None,
    the __init__ default) get the byte-identical OLD chain: self._merchant_
    user_id first, then the untrusted result/trip_request fallback."""
    client = _RecordingClient()
    orch = TravelOrchestrator(memory_client=client)
    orch._trip_id = "t-m1-followup-fallback"
    orch._merchant_user_id = "verified-by-old-chain"
    assert orch._memory_user_id is None  # never set -> old behavior applies
    result = _success_result(user_id="ignored")
    orch._maybe_log_search(result, {"user_id": "ignored"})
    assert client.log_search_calls[0]["user_id"] == "verified-by-old-chain"


def test_negotiate_sets_memory_user_id_from_trip_request_including_empty_string():
    """negotiate() itself must read trip_request['memory_verified_user_id']
    verbatim (including a deliberate ""), never coerce it back via `or`."""
    orch = TravelOrchestrator(memory_client=None)
    orch.negotiate({
        "user_id": "attacker-supplied",
        "merchant_user_id": "attacker-supplied",
        "memory_verified_user_id": "",
        "total_budget_cents": 100000,
        "legs": [{"city": "bali", "checkin": "2026-10-01", "checkout": "2026-10-04", "adults": 1}],
    })
    assert orch._memory_user_id == "", (
        "an explicit '' in trip_request must be preserved verbatim, not treated as absent"
    )


def test_negotiate_leaves_memory_user_id_none_when_key_absent():
    orch = TravelOrchestrator(memory_client=None)
    orch.negotiate({
        "user_id": "some-user",
        "total_budget_cents": 100000,
        "legs": [{"city": "bali", "checkin": "2026-10-01", "checkout": "2026-10-04", "adults": 1}],
    })
    assert orch._memory_user_id is None


# ===========================================================================
# 4. End-to-end through the REAL server endpoints
# ===========================================================================

def _client():
    from starlette.testclient import TestClient
    from orchestration.server import build_app
    set_store(SqliteDashboardStore(":memory:"))
    return TestClient(build_app())


def _login(c, user_id: str) -> str:
    r = c.post("/session/login", json={"user_id": user_id})
    assert r.status_code == 200, r.text
    return r.json()["session_token"]


def test_negotiate_endpoint_threads_correctly_gated_identity():
    """POST /negotiate (structured) — capture the body actually handed to the
    executor for three cases: anonymous, unverified tier-1 claim, and a
    genuinely verified tier-1 session."""
    captured: list[dict] = []

    import orchestration.server as server_mod

    with _client() as c:
        with patch.object(server_mod._state, "executor") as fake_executor:
            fake_executor.submit.side_effect = lambda fn, body, *a, **kw: captured.append(body)

            # Case 1: fully anonymous (no user_id, no owner_token claimed).
            r = c.post("/negotiate", json={
                "total_budget_cents": 300000,
                "legs": [{"city": "bali", "checkin": "2026-10-01", "checkout": "2026-10-04", "adults": 1}],
            })
            assert r.status_code == 200, r.text

            # Case 2: a named user_id with NO session_token.
            r = c.post("/negotiate", json={
                "user_id": "demo-lena",
                "total_budget_cents": 300000,
                "legs": [{"city": "bali", "checkin": "2026-10-01", "checkout": "2026-10-04", "adults": 1}],
            })
            assert r.status_code == 200, r.text

            # Case 3: a named user_id WITH a genuinely valid session_token.
            token = _login(c, "demo-lena")
            r = c.post("/negotiate", json={
                "user_id": "demo-lena",
                "session_token": token,
                "total_budget_cents": 300000,
                "legs": [{"city": "bali", "checkin": "2026-10-01", "checkout": "2026-10-04", "adults": 1}],
            })
            assert r.status_code == 200, r.text

    assert len(captured) == 3, f"expected 3 captured negotiate bodies, got {len(captured)}"
    anon_body, unverified_body, verified_body = captured

    # Case 1: anonymous -> memory_verified_user_id mirrors merchant_user_id (safe either way).
    assert anon_body["memory_verified_user_id"] == anon_body["merchant_user_id"]

    # Case 2: unverified tier-1 claim -> DENIED, even though merchant_user_id itself
    # (checkout identity, unaffected by this follow-up) still carries the raw claim.
    assert unverified_body["merchant_user_id"] == "demo-lena"
    assert unverified_body["memory_verified_user_id"] == "", (
        f"an unverified named user_id claim must be denied for memory purposes: {unverified_body}"
    )

    # Case 3: session-verified tier-1 -> trusted.
    assert verified_body["merchant_user_id"] == "demo-lena"
    assert verified_body["memory_verified_user_id"] == "demo-lena"


def test_negotiate_text_endpoint_threads_correctly_gated_identity():
    """POST /negotiate_text — capture the kwargs handed to
    intent_parser.negotiate_from_text for an unverified vs. verified tier-1
    claim (mirrors the structured-endpoint test above)."""
    captured_kwargs: list[dict] = []

    def _fake_negotiate_from_text(*args, **kwargs):
        captured_kwargs.append(kwargs)
        return {"outcome": "success", "legs": [], "trip_id": "trip-fake"}

    with _client() as c:
        with patch("utils.intent_parser.negotiate_from_text", side_effect=_fake_negotiate_from_text):
            r = c.post("/negotiate_text", json={
                "text": "7 days in bali, beach, $3000",
                "user_id": "demo-lena",
            })
            assert r.status_code == 200, r.text

            token = _login(c, "demo-lena")
            r = c.post("/negotiate_text", json={
                "text": "7 days in bali, beach, $3000",
                "user_id": "demo-lena",
                "session_token": token,
            })
            assert r.status_code == 200, r.text

    assert len(captured_kwargs) == 2
    unverified_kwargs, verified_kwargs = captured_kwargs
    assert unverified_kwargs.get("merchant_user_id") == "demo-lena"
    assert unverified_kwargs.get("memory_verified_user_id") == "", (
        f"an unverified named user_id claim must be denied for memory purposes: {unverified_kwargs}"
    )
    assert verified_kwargs.get("memory_verified_user_id") == "demo-lena"
