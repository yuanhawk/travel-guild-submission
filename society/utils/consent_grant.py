"""consent_grant.py — server-issued, HMAC-signed grants for the Fraud
consent-override token (closes the forgeable-consent-token gap).

CONTEXT
───────
fraud_agent.py's `validate_consent_token()` is a deliberately PURE,
deterministic string parser — no secrets, no session state, no wall-clock (see
its own module docstring). That is intentional: it lets fraud_agent.py's
exhaustive invariant suite (test_inv_fraud_consent_override.py) exercise the
BLOCKED→committable flip with hand-built "consent:{cid}:{band}:{nonce}"
strings, and it keeps the gate itself byte-identical/deterministic. This
module does NOT change that contract.

What WAS missing: nothing upstream of fraud_agent.py ever checked that a
consent_token handed to it over the network actually came from a real,
authenticated human decision. A client could POST any hand-typed
"consent:{cid}:{band}:{nonce}" string straight to /negotiate and it would
satisfy fraud_agent.py's parser just fine (it only checks shape, binding, and
band-consistency — never authenticity).

THE FIX (at the HTTP boundary, not inside fraud_agent.py)
──────────────────────────────────────────────────────────
POST /consent (server.py) requires a live session_token (verify_session — the
same bar as PUT /preferences / GET /telegram/link) and mints a token via
`mint_consent_grant()` below. server.py's negotiate handler then verifies every
incoming consent_token with `verify_consent_grant()` — bound to the exact
(counterparty_id, risk_band, session_token) — BEFORE it is allowed to reach
fraud.vet / the Critic's re-check. Anything that doesn't verify (including a
hand-typed string) is silently dropped, so fraud_agent.py sees no token at all
and the gate stays closed — exactly as if consent had never been supplied.

SECURITY
────────
  - HMAC-SHA256 over (counterparty_id, risk_band, session_token, expiry) with a
    server-held secret (CONSENT_GRANT_SECRET env var; a random one is
    generated at process start if unset — the same single-process demo
    tradeoff already documented in session_token.py).
  - Grants expire after _GRANT_TTL_SECONDS; an expired signature is rejected.
  - Verification uses hmac.compare_digest (constant-time — no timing leak on
    the signature comparison).
  - The nonce is `{sig_hex}.{expiry_int}` — it contains no ':' so the overall
    token still parses as exactly 4 colon-separated fields, matching
    fraud_agent.py's CONSENT_TOKEN_CONTRACT unchanged.
"""
from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import time

_GRANT_TTL_SECONDS = 15 * 60  # a consent grant is valid for 15 minutes

# Single-process demo secret (mirrors session_token.py's tradeoff): a real
# multi-process deployment should set CONSENT_GRANT_SECRET explicitly so every
# worker shares the same signing key.
_SECRET: str = os.environ.get("CONSENT_GRANT_SECRET") or secrets.token_urlsafe(32)


def _sign(counterparty_id: str, risk_band: str, session_token: str, expiry: int) -> str:
    msg = f"{counterparty_id}|{risk_band}|{session_token}|{expiry}".encode()
    return hmac.new(_SECRET.encode(), msg, hashlib.sha256).hexdigest()


def mint_consent_grant(counterparty_id: str, risk_band: str, session_token: str) -> str:
    """
    Mint a server-signed consent token for a session that has ALREADY been
    authenticated by the caller (verify_session) for the given counterparty's
    CURRENT observed risk_band. Returns the full
    "consent:{counterparty_id}:{risk_band}:{nonce}" string fraud_agent.py's
    parser expects — no format change on that side.
    """
    cid = counterparty_id.strip().lower()
    band = risk_band.strip().lower()
    expiry = int(time.time()) + _GRANT_TTL_SECONDS
    sig = _sign(cid, band, session_token, expiry)
    nonce = f"{sig}.{expiry}"
    return f"consent:{cid}:{band}:{nonce}"


def verify_consent_grant(
    token: str | None,
    *,
    counterparty_id: str,
    risk_band: str,
    session_token: str,
) -> bool:
    """
    True iff `token` is a well-formed, unexpired, signature-valid grant for
    exactly this (counterparty_id, risk_band, session_token). Fail-conservative:
    any parse error, expiry, or signature mismatch → False. NEVER raises.
    """
    if not isinstance(token, str) or not session_token:
        return False
    parts = token.strip().split(":")
    if len(parts) != 4 or parts[0].strip().lower() != "consent":
        return False
    _scheme, tok_cid, tok_band, nonce = parts
    if tok_cid.strip().lower() != counterparty_id.strip().lower():
        return False
    if tok_band.strip().lower() != risk_band.strip().lower():
        return False
    nonce_parts = nonce.split(".")
    if len(nonce_parts) != 2:
        return False
    sig, expiry_str = nonce_parts
    try:
        expiry = int(expiry_str)
    except ValueError:
        return False
    if time.time() > expiry:
        return False
    expected = _sign(counterparty_id.strip().lower(), risk_band.strip().lower(), session_token, expiry)
    return hmac.compare_digest(sig, expected)
