"""session_token.py — reusable demo-login session tokens for the two endpoints
that act directly AS the claimed user_id (PUT /preferences, GET /telegram/link).

CONTEXT — closes the user_id-trust gap found by the Telegram deep-link security
audit: `GET /telegram/link?user_id=X` (and `PUT /preferences`) trusted the
caller's claimed user_id with ZERO authentication — anyone who knows/guesses a
user_id could act as that user. POST /session/login (server.py) issues a token
here after validating the user_id is a real pre-seeded demo user; the two
endpoints then require verify_session(token, user_id) before proceeding.

STORAGE — DELIBERATELY IN-MEMORY, SINGLE-PROCESS (same honest tradeoff as
telegram_link.py): tokens live in a plain dict and do NOT survive a process
restart. Acceptable for this demo/single-process deployment.

SEMANTICS — DIFFERENT FROM telegram_link.py: this module issues REUSABLE
session tokens (many requests over up to _SESSION_TTL_SECONDS), NOT single-use
link tokens. verify_session does NOT pop/consume the token on a successful
check — do not copy telegram_link.py's pop-on-read pattern here.

SECURITY:
  - Tokens are generated via `secrets.token_urlsafe` (cryptographically random,
    unguessable) — never `random`.
  - EXPIRES after _SESSION_TTL_SECONDS (8 hours, named constant) — a
    reasonable demo session length.
  - verify_session checks token existence, expiry, AND that the token belongs
    to the claimed_user_id — a valid-but-mismatched-user token is rejected,
    which is the actual security property this module exists to provide.
"""
from __future__ import annotations

import secrets
import time

_SESSIONS: dict[str, tuple[str, float]] = {}   # token -> (user_id, expiry_unix_ts)
_SESSION_TTL_SECONDS = 8 * 3600   # 8 hours — a reasonable demo session length


def _sweep_expired() -> None:
    """Evict every expired session. Called lazily at issue time so abandoned
    sessions never accumulate unboundedly (mirrors telegram_link.py's sweep)."""
    now = time.time()
    for token in [t for t, (_, exp) in _SESSIONS.items() if exp < now]:
        del _SESSIONS[token]


def issue_session_token(user_id: str) -> str:
    """Issue a new reusable session token for user_id. Cryptographically random
    (secrets.token_urlsafe) — unguessable, not a sequential/predictable id."""
    _sweep_expired()
    token = secrets.token_urlsafe(24)
    _SESSIONS[token] = (user_id, time.time() + _SESSION_TTL_SECONDS)
    return token


def verify_session(token: str | None, claimed_user_id: str) -> bool:
    """True only if token exists, is unexpired, AND belongs to claimed_user_id.

    Does NOT pop/consume the token (sessions are reusable across many requests,
    unlike telegram_link.py's single-use link tokens — do not copy that
    pop-on-read pattern here).
    """
    if not isinstance(token, str) or not token:
        return False
    entry = _SESSIONS.get(token)
    if entry is None:
        return False
    user_id, expiry = entry
    if time.time() > expiry:
        del _SESSIONS[token]
        return False
    return user_id == claimed_user_id
