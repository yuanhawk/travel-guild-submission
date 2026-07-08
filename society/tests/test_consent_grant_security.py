"""
test_consent_grant_security.py — regression tests for the server-signed
Fraud consent-grant fix (closes the forgeable-consent-token gap found in a
security audit: fraud_agent.py's validate_consent_token() is an intentionally
PURE/unsigned string parser — see its module docstring — so a client could
otherwise just type "consent:{cid}:{band}:{nonce}" and hand it straight to
POST /negotiate).

THE FIX
  - utils/consent_grant.py: HMAC-signed grants, bound to
    (counterparty_id, risk_band, session_token), with an expiry.
  - server.py POST /consent: session-gated endpoint that mints a grant for the
    counterparty's CURRENT observed risk_band.
  - server.py POST /negotiate: _filter_verified_consent_tokens() drops any
    consent_tokens entry that isn't a signature-valid grant for the session
    making the request, BEFORE trip_request ever reaches orch.negotiate() (and
    therefore before it can reach fraud.vet / the Critic's re-check).

WHAT THIS FILE DOES NOT TOUCH
  fraud_agent.py's validate_consent_token()/vet_counterparty() stay byte-
  identical and untested here — test_inv_fraud_consent_override.py already
  covers that pure-function contract exhaustively and deliberately accepts
  hand-built unsigned tokens (that is correct — this fix's job is to make sure
  fraud_agent.py never SEES a hand-built token that arrived over HTTP).
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from starlette.testclient import TestClient

from orchestration import server
from utils.consent_grant import mint_consent_grant, verify_consent_grant
from utils.session_token import issue_session_token

BLOCKED_CP = "carrier-skylark-budget-air"   # seeded score 28 -> blocked
CLEAR_CP = "carrier-garuda-indonesia"       # seeded score 82 -> clear


# ─────────────────────────────────────────────────────────────────────────────
# PART A — utils/consent_grant.py: mint/verify contract (unit level)
# ─────────────────────────────────────────────────────────────────────────────

def test_mint_and_verify_roundtrip():
    token = mint_consent_grant(BLOCKED_CP, "blocked", "session-abc")
    assert verify_consent_grant(
        token, counterparty_id=BLOCKED_CP, risk_band="blocked", session_token="session-abc",
    ) is True


def test_verify_rejects_wrong_session():
    token = mint_consent_grant(BLOCKED_CP, "blocked", "session-abc")
    assert verify_consent_grant(
        token, counterparty_id=BLOCKED_CP, risk_band="blocked", session_token="a-different-session",
    ) is False


def test_verify_rejects_wrong_counterparty():
    token = mint_consent_grant(BLOCKED_CP, "blocked", "session-abc")
    assert verify_consent_grant(
        token, counterparty_id="carrier-garuda-indonesia", risk_band="blocked", session_token="session-abc",
    ) is False


def test_verify_rejects_wrong_band():
    token = mint_consent_grant(BLOCKED_CP, "blocked", "session-abc")
    assert verify_consent_grant(
        token, counterparty_id=BLOCKED_CP, risk_band="unknown", session_token="session-abc",
    ) is False


def test_verify_rejects_tampered_signature():
    token = mint_consent_grant(BLOCKED_CP, "blocked", "session-abc")
    scheme, cid, band, nonce = token.split(":")
    sig, expiry = nonce.split(".")
    tampered = f"{scheme}:{cid}:{band}:{sig[:-1]}{'0' if sig[-1] != '0' else '1'}.{expiry}"
    assert verify_consent_grant(
        tampered, counterparty_id=BLOCKED_CP, risk_band="blocked", session_token="session-abc",
    ) is False


def test_verify_rejects_expired_grant():
    import time
    scheme_cid_band = f"consent:{BLOCKED_CP}:blocked"
    from utils.consent_grant import _sign  # noqa: SLF001 — white-box expiry test
    expired_ts = int(time.time()) - 10
    sig = _sign(BLOCKED_CP, "blocked", "session-abc", expired_ts)
    token = f"{scheme_cid_band}:{sig}.{expired_ts}"
    assert verify_consent_grant(
        token, counterparty_id=BLOCKED_CP, risk_band="blocked", session_token="session-abc",
    ) is False


def test_verify_rejects_a_hand_typed_token():
    """The exact attack this fix closes: a plausible-looking but unsigned token."""
    forged = f"consent:{BLOCKED_CP}:blocked:whatever-i-typed"
    assert verify_consent_grant(
        forged, counterparty_id=BLOCKED_CP, risk_band="blocked", session_token="session-abc",
    ) is False


# ─────────────────────────────────────────────────────────────────────────────
# PART B — server._filter_verified_consent_tokens (the /negotiate boundary)
# ─────────────────────────────────────────────────────────────────────────────

def test_filter_drops_a_forged_token():
    raw = {BLOCKED_CP: f"consent:{BLOCKED_CP}:blocked:hand-typed-nonce"}
    out = server._filter_verified_consent_tokens(raw, "session-abc")
    assert out == {}


def test_filter_keeps_a_server_minted_grant_for_the_right_session():
    token = mint_consent_grant(BLOCKED_CP, "blocked", "session-abc")
    raw = {BLOCKED_CP: token}
    out = server._filter_verified_consent_tokens(raw, "session-abc")
    assert out == {BLOCKED_CP: token}


def test_filter_drops_a_server_minted_grant_replayed_by_another_session():
    token = mint_consent_grant(BLOCKED_CP, "blocked", "session-abc")
    raw = {BLOCKED_CP: token}
    out = server._filter_verified_consent_tokens(raw, "someone-elses-session")
    assert out == {}


def test_filter_returns_empty_without_a_session_token():
    token = mint_consent_grant(BLOCKED_CP, "blocked", "session-abc")
    raw = {BLOCKED_CP: token}
    out = server._filter_verified_consent_tokens(raw, "")
    assert out == {}


def test_filter_tolerates_non_dict_and_malformed_input():
    assert server._filter_verified_consent_tokens(None, "session-abc") == {}
    assert server._filter_verified_consent_tokens("not-a-dict", "session-abc") == {}
    assert server._filter_verified_consent_tokens({123: "x"}, "session-abc") == {}
    assert server._filter_verified_consent_tokens({BLOCKED_CP: 123}, "session-abc") == {}
    assert server._filter_verified_consent_tokens({BLOCKED_CP: "not:four:fields"}, "session-abc") == {}


# ─────────────────────────────────────────────────────────────────────────────
# PART C — POST /consent (session-gated minting endpoint, ASGI)
# ─────────────────────────────────────────────────────────────────────────────

def _client() -> TestClient:
    return TestClient(server.build_app())


def test_consent_endpoint_requires_a_valid_session():
    with _client() as c:
        r = c.post("/consent", json={
            "user_id": "demo-mei", "session_token": "not-a-real-token",
            "counterparty_id": BLOCKED_CP,
        })
        assert r.status_code == 401


def test_consent_endpoint_requires_user_id_and_counterparty_id():
    with _client() as c:
        r = c.post("/consent", json={"session_token": "x"})
        assert r.status_code == 400


def test_consent_endpoint_refuses_when_no_consent_is_required():
    token = issue_session_token("demo-mei")
    with _client() as c:
        r = c.post("/consent", json={
            "user_id": "demo-mei", "session_token": token, "counterparty_id": CLEAR_CP,
        })
        assert r.status_code == 400
        assert r.json()["error"] == "no_consent_required"


def test_consent_endpoint_mints_a_verifiable_grant_for_a_blocked_counterparty():
    token = issue_session_token("demo-mei")
    with _client() as c:
        r = c.post("/consent", json={
            "user_id": "demo-mei", "session_token": token, "counterparty_id": BLOCKED_CP,
        })
        assert r.status_code == 200
        body = r.json()
        assert body["risk_band"] == "blocked"
        grant = body["consent_token"]
        assert verify_consent_grant(
            grant, counterparty_id=BLOCKED_CP, risk_band="blocked", session_token=token,
        ) is True
        # And the server's own /negotiate-boundary filter accepts exactly this grant.
        filtered = server._filter_verified_consent_tokens({BLOCKED_CP: grant}, token)
        assert filtered == {BLOCKED_CP: grant}


# ─────────────────────────────────────────────────────────────────────────────
# PART D — full HTTP round trip: /negotiate strips a forged token from
# trip_request before it ever reaches the orchestrator.
# ─────────────────────────────────────────────────────────────────────────────

def test_negotiate_strips_forged_consent_token_before_orchestrator_sees_it():
    """
    Monkeypatch _state.orch.negotiate to capture the trip_request it actually
    receives, then POST /negotiate with a hand-forged consent token. The
    captured trip_request's consent_tokens must be empty — the forgery never
    reaches the orchestrator (and therefore never reaches fraud.vet/Critic).
    """
    with _client() as c:
        captured: dict = {}
        real_negotiate = server._state.orch.negotiate

        def _spy(trip_request, **kwargs):
            captured["trip_request"] = trip_request
            return real_negotiate(trip_request, **kwargs)

        server._state.orch.negotiate = _spy
        try:
            r = c.post("/negotiate", json={
                "user_id": "demo-mei",
                "total_budget_cents": 100000,
                "today": "2026-10-01",
                "legs": [{"city": "bali", "checkin": "2026-10-01",
                          "checkout": "2026-10-04", "adults": 1, "vibe": "beach"}],
                "consent_tokens": {BLOCKED_CP: f"consent:{BLOCKED_CP}:blocked:hand-typed"},
            })
            assert r.status_code == 200
            # Drain enough of the stream to guarantee the worker thread ran and
            # called orch.negotiate() at least once.
            stream_id = r.json()["stream_id"]
            with c.stream("GET", f"/stream/{stream_id}") as resp:
                for raw_line in resp.iter_lines():
                    if raw_line.startswith("data:"):
                        import json as _json
                        ev = _json.loads(raw_line[len("data:"):].strip())
                        if ev.get("type") in (server.SENTINEL_TYPE, "timeout"):
                            break
        finally:
            server._state.orch.negotiate = real_negotiate

        assert "trip_request" in captured, "orch.negotiate was never called"
        assert captured["trip_request"].get("consent_tokens") == {}, (
            "a hand-forged consent_token reached the orchestrator unfiltered — "
            "the /negotiate boundary filter regressed"
        )


def test_negotiate_forwards_a_genuine_server_minted_grant():
    """The mirror image: a grant this process itself minted for this session
    DOES survive the filter and reach the orchestrator."""
    session = issue_session_token("demo-mei")
    grant = mint_consent_grant(BLOCKED_CP, "blocked", session)

    with _client() as c:
        captured: dict = {}
        real_negotiate = server._state.orch.negotiate

        def _spy(trip_request, **kwargs):
            captured["trip_request"] = trip_request
            return real_negotiate(trip_request, **kwargs)

        server._state.orch.negotiate = _spy
        try:
            r = c.post("/negotiate", json={
                "user_id": "demo-mei",
                "session_token": session,
                "total_budget_cents": 100000,
                "today": "2026-10-01",
                "legs": [{"city": "bali", "checkin": "2026-10-01",
                          "checkout": "2026-10-04", "adults": 1, "vibe": "beach"}],
                "consent_tokens": {BLOCKED_CP: grant},
            })
            assert r.status_code == 200
            stream_id = r.json()["stream_id"]
            with c.stream("GET", f"/stream/{stream_id}") as resp:
                for raw_line in resp.iter_lines():
                    if raw_line.startswith("data:"):
                        import json as _json
                        ev = _json.loads(raw_line[len("data:"):].strip())
                        if ev.get("type") in (server.SENTINEL_TYPE, "timeout"):
                            break
        finally:
            server._state.orch.negotiate = real_negotiate

        assert "trip_request" in captured, "orch.negotiate was never called"
        assert captured["trip_request"].get("consent_tokens") == {BLOCKED_CP: grant}


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-v"]))
