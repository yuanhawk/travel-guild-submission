"""telegram_link.py — account-linking deep-link tokens for the Telegram "Connect"
flow (Preferences → Connect Telegram → t.me/<bot>?start=<token>).

Purpose: bridge a logged-in web session (user_id) to an inbound Telegram chat_id
WITHOUT ever asking the user to paste a chat_id by hand. The web app requests a
token (GET /telegram/link), opens `https://t.me/<bot>?start=<token>` in a new tab,
and the bot's /start handler (telegram_companion.py) resolves the token back to
the user_id and persists chat_id into that user's prefs.

STORAGE — DELIBERATELY IN-MEMORY, SINGLE-PROCESS (honest tradeoff):
  This is a demo/single-process deployment. Tokens live in a plain dict and do
  NOT survive a process restart — a token issued right before a restart simply
  goes invalid (the user re-clicks "Connect Telegram" and gets a new one). This
  is an acceptable tradeoff for a "click connect, tap Telegram, done within
  minutes" flow, and deliberately avoids adding a new SQLite table / persistent
  store for a value that only needs to live for 15 minutes.

SECURITY:
  - Tokens are generated via `secrets.token_urlsafe` (cryptographically random,
    unguessable) — never `random`.
  - SINGLE-USE: resolve_link_token pops the entry on every call (success or
    expired), so a token can never be resolved twice.
  - EXPIRES after _TOKEN_TTL_SECONDS (15 min, named constant).
  - resolve_link_token returns None uniformly for "never existed" and "existed
    but expired" — no side channel that would let a caller distinguish the two.
"""
from __future__ import annotations

import secrets
import time

_LINK_TOKENS: dict[str, tuple[str, float]] = {}   # token -> (user_id, expiry_unix_ts)
_TOKEN_TTL_SECONDS = 900   # 15 minutes


def _sweep_expired() -> None:
    """Evict every expired-but-never-resolved entry. Single-use tokens are
    already popped on resolve, but an ABANDONED token (never tapped) would
    otherwise sit in _LINK_TOKENS forever — unbounded growth. Called lazily at
    generation time so there is no background thread/timer to manage."""
    now = time.time()
    for token in [t for t, (_, exp) in _LINK_TOKENS.items() if exp < now]:
        del _LINK_TOKENS[token]


def generate_link_token(user_id: str) -> str:
    """Issue a new single-use, short-lived token for user_id. Cryptographically
    random (secrets.token_urlsafe) — unguessable, not a sequential/predictable id."""
    _sweep_expired()
    token = secrets.token_urlsafe(24)
    _LINK_TOKENS[token] = (user_id, time.time() + _TOKEN_TTL_SECONDS)
    return token


def resolve_link_token(token: str) -> str | None:
    """Resolve a token to the user_id that requested it.

    SINGLE-USE: the token is popped from the store on every call, whether it
    resolves successfully or not — a token can never be resolved twice, and an
    expired-but-not-yet-cleaned-up token is not left resolvable.

    Returns None uniformly for "token never existed" and "token existed but
    expired" (no timing/enumeration side channel).
    """
    entry = _LINK_TOKENS.pop(token, None)
    if entry is None:
        return None
    user_id, expiry = entry
    if time.time() > expiry:
        return None
    return user_id
