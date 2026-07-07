"""test_telegram_link.py — account-linking deep-link token store tests.

Covers:
- generate_link_token / resolve_link_token round-trip success
- single-use: a second resolve of the same token fails
- expiry: a token past its TTL fails to resolve (time injected, no real sleeping)
- unknown token never existed → None (same as expired, no side channel)
"""
from __future__ import annotations

from utils.telegram_link import (
    _TOKEN_TTL_SECONDS,
    generate_link_token,
    resolve_link_token,
)


def test_round_trip_success():
    token = generate_link_token("demo-mei")
    assert resolve_link_token(token) == "demo-mei"


def test_single_use_second_resolve_fails():
    token = generate_link_token("demo-mei")
    assert resolve_link_token(token) == "demo-mei"
    # Second resolve of the SAME token must fail — single-use.
    assert resolve_link_token(token) is None


def test_unknown_token_returns_none():
    assert resolve_link_token("this-token-was-never-issued") is None


def test_expiry_via_injected_time(monkeypatch):
    """A token past its TTL must fail to resolve. Time is injected (module-level
    `time` import in telegram_link.py) rather than sleeping 15 real minutes."""
    import utils.telegram_link as telegram_link

    fake_now = [1_000_000.0]
    monkeypatch.setattr(telegram_link.time, "time", lambda: fake_now[0])

    token = telegram_link.generate_link_token("demo-alex")
    # Advance past the TTL.
    fake_now[0] += _TOKEN_TTL_SECONDS + 1
    assert telegram_link.resolve_link_token(token) is None


def test_expiry_boundary_still_valid_at_ttl(monkeypatch):
    """A token resolved AT (not past) the expiry instant is still valid — the
    resolve check is `time.time() > expiry`, not `>=`."""
    import utils.telegram_link as telegram_link

    fake_now = [2_000_000.0]
    monkeypatch.setattr(telegram_link.time, "time", lambda: fake_now[0])

    token = telegram_link.generate_link_token("demo-priya")
    fake_now[0] += _TOKEN_TTL_SECONDS  # exactly at expiry, not past it
    assert telegram_link.resolve_link_token(token) == "demo-priya"


def test_token_is_unguessable_and_url_safe():
    """Tokens must be long, random (secrets.token_urlsafe) — not sequential ids."""
    tokens = {generate_link_token("demo-jack") for _ in range(20)}
    assert len(tokens) == 20, "tokens must be unique/random, not colliding"
    for t in tokens:
        assert len(t) >= 24
        # url-safe alphabet only
        assert all(c.isalnum() or c in "-_" for c in t)


def test_expired_token_not_resolvable_twice_either():
    """Even an expired-but-not-yet-cleaned-up token must be popped on first
    resolve attempt (no double-resolve window)."""
    import time as real_time
    import utils.telegram_link as telegram_link

    token = telegram_link.generate_link_token("demo-lena")
    # Force it to look expired without waiting: directly mutate the stored expiry.
    user_id, _ = telegram_link._LINK_TOKENS[token]
    telegram_link._LINK_TOKENS[token] = (user_id, real_time.time() - 1)

    assert telegram_link.resolve_link_token(token) is None
    # And it's gone now — not resolvable a second time either.
    assert telegram_link.resolve_link_token(token) is None
