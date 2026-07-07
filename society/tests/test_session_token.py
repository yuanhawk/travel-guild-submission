"""test_session_token.py — reusable demo-login session token store tests.

Covers:
- issue_session_token / verify_session round-trip success (right user_id)
- verify_session fails for a WRONG user_id (the actual security property this
  module exists to add — a valid token minted for user A must never verify
  for a claimed user_id B)
- expiry: a token past its TTL fails to verify (time injected, no real sleeping)
- unknown/never-issued token fails to verify
- missing/empty token fails to verify
- reusability: a successful verify does NOT consume the token (unlike
  telegram_link.py's single-use tokens) — a second verify for the same user
  still succeeds
- sweep actually removes expired entries (exercised via issue_session_token,
  which calls _sweep_expired at the top, mirroring test_telegram_link.py's
  time-injection style)
"""
from __future__ import annotations

from utils.session_token import (
    _SESSION_TTL_SECONDS,
    issue_session_token,
    verify_session,
)


def test_round_trip_success():
    token = issue_session_token("demo-mei")
    assert verify_session(token, "demo-mei") is True


def test_wrong_user_id_fails():
    """The core security property: a token minted for one user must not verify
    for a different claimed user_id."""
    token = issue_session_token("demo-mei")
    assert verify_session(token, "demo-alex") is False


def test_missing_token_fails():
    assert verify_session(None, "demo-mei") is False
    assert verify_session("", "demo-mei") is False


def test_unknown_token_fails():
    assert verify_session("this-token-was-never-issued", "demo-mei") is False


def test_token_is_reusable_not_consumed():
    """Sessions are reusable across many requests — unlike telegram_link.py's
    single-use link tokens, a successful verify must NOT pop the token."""
    token = issue_session_token("demo-priya")
    assert verify_session(token, "demo-priya") is True
    assert verify_session(token, "demo-priya") is True  # still valid, not consumed
    assert verify_session(token, "demo-priya") is True


def test_expiry_via_injected_time(monkeypatch):
    """A token past its TTL must fail to verify. Time is injected (module-level
    `time` import in session_token.py) rather than sleeping 8 real hours."""
    import utils.session_token as session_token

    fake_now = [1_000_000.0]
    monkeypatch.setattr(session_token.time, "time", lambda: fake_now[0])

    token = session_token.issue_session_token("demo-lena")
    fake_now[0] += _SESSION_TTL_SECONDS + 1
    assert session_token.verify_session(token, "demo-lena") is False


def test_expiry_boundary_still_valid_at_ttl(monkeypatch):
    """A token verified AT (not past) the expiry instant is still valid — the
    check is `time.time() > expiry`, not `>=`."""
    import utils.session_token as session_token

    fake_now = [2_000_000.0]
    monkeypatch.setattr(session_token.time, "time", lambda: fake_now[0])

    token = session_token.issue_session_token("demo-jack")
    fake_now[0] += _SESSION_TTL_SECONDS  # exactly at expiry, not past it
    assert session_token.verify_session(token, "demo-jack") is True


def test_sweep_removes_expired_entries(monkeypatch):
    """Abandoned/expired sessions must not accumulate unboundedly — the lazy
    sweep (called at issue time) evicts them."""
    import utils.session_token as session_token

    # bug fix (adversarial-audit follow-up): _SESSIONS is a module-level GLOBAL
    # shared by every test file in the same process (xdist loadscope groups
    # whole test files per worker) -- a bare .clear() silently wiped tokens
    # OTHER test files had already issued at their own module-import time
    # (e.g. test_reconsider_leg_endpoint.py's _OWNER_TOKEN), the exact same
    # leak class fixed in test_confirm_endpoint.py's restart-simulation test.
    # monkeypatch.setattr swaps in a fresh dict and auto-restores the ORIGINAL
    # object at teardown -- every consumer (verify_session/issue_session_token)
    # does a fresh module-attribute lookup each call, never a captured
    # reference, so swapping the object (not mutating it in place) is safe.
    monkeypatch.setattr(session_token, "_SESSIONS", {})
    fake_now = [3_000_000.0]
    monkeypatch.setattr(session_token.time, "time", lambda: fake_now[0])

    stale = session_token.issue_session_token("demo-mei")
    assert stale in session_token._SESSIONS

    fake_now[0] += _SESSION_TTL_SECONDS + 1  # expire the stale token
    # Issuing a new token triggers _sweep_expired() before insertion.
    fresh = session_token.issue_session_token("demo-alex")

    assert stale not in session_token._SESSIONS
    assert fresh in session_token._SESSIONS


def test_tokens_are_unguessable_and_url_safe():
    """Tokens must be long, random (secrets.token_urlsafe) — not sequential ids."""
    tokens = {issue_session_token("demo-jack") for _ in range(20)}
    assert len(tokens) == 20, "tokens must be unique/random, not colliding"
    for t in tokens:
        assert len(t) >= 24
        assert all(c.isalnum() or c in "-_" for c in t)
