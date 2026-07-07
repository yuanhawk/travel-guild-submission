"""
ucp_signing.py — #53 NHI stretch: client-side RFC 9421 HTTP Message Signature
signing for the Python UCP client, byte-compatible with the Go merchant verifier
(ucp-merchant/rfc9421.go).

The merchant already VERIFIES signed requests + derives server-side trust tiers
from the verified signer (DESIGN §13). This is the missing CLIENT half: the Python
agent signs its merchant calls so the agent->merchant seam carries a verifiable
ES256 identity instead of running anonymously at the unsigned tier floor.

Byte-compat contract (mirrors rfc9421.go / signing.go exactly):
  - alg            : "ecdsa-p256-sha256" (ES256 over EC P-256, SHA-256)
  - signature base : for each covered component  `"<c>": <value>\n`, then
                     `"@signature-params": <raw-params>`  (NO trailing newline)
  - content-digest : `sha-256=:` + base64-STD(sha256(body)) + `:`
  - signature      : raw r||s (32+32 bytes) base64-STD, wrapped `<label>=:<b64>:`
  - JWK coords     : 32-byte big-endian, base64url UNPADDED (Go base64.RawURLEncoding)

OFF BY DEFAULT: signed_headers() returns {} unless BOTH env vars are set
(UCP_CLIENT_SIGNING_KEY_PATH + UCP_AGENT_PROFILE_URL) → unsigned requests stay
byte-identical to today (backward-compatible; the merchant floors unsigned callers
at its configured UnsignedTier). Signing is purely ADDITIVE — it only attaches
headers; it never alters the request body or the deterministic itinerary path.
"""
from __future__ import annotations

import base64
import hashlib
import logging
import os
from urllib.parse import urlsplit

logger = logging.getLogger(__name__)

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature

class SigningError(RuntimeError):
    pass


_ALG = "ecdsa-p256-sha256"
_COMPONENTS = ("@method", "@authority", "@path", "content-digest")


def _b64u(b: bytes) -> str:
    """Unpadded base64url — matches Go base64.RawURLEncoding (JWK coords)."""
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def load_or_create_key(path: str | None):
    """Load an EC P-256 private key from a PEM file, or generate + persist one
    (0600). A None/empty path generates an ephemeral in-memory key.

    KMS SEAM (build-early / activate-late): the at-rest signing key on disk is
    the NHI long-lived-key edge. The KMS path plugs in HERE — when
    `UCP_KMS_KEY_ID` is set, replace the on-disk load with either (a) a KMS
    `Decrypt` of a KMS-wrapped PEM into memory, or (b) a KMS asymmetric `Sign`
    signer so the key never leaves KMS. Default (no env) keeps this on-disk
    behaviour unchanged. The live KMS call is intentionally NOT scaffolded
    here (unverifiable without KMS creds + the `alibabacloud_kms` SDK); see
    ALICLOUD-PROOF.md §3 for the activation status and design."""
    passphrase_env = os.environ.get("UCP_KEY_PASSPHRASE", "")
    if path and os.path.exists(path):
        with open(path, "rb") as fh:
            key_data = fh.read()
        return serialization.load_pem_private_key(
            key_data,
            password=passphrase_env.encode() if passphrase_env else None,
        )
    key = ec.generate_private_key(ec.SECP256R1())
    if path:
        if passphrase_env:
            encryption = serialization.BestAvailableEncryption(passphrase_env.encode())
        else:
            encryption = serialization.NoEncryption()  # backwards compat when env var not set
        key_pem = key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=encryption,
        )
        with open(path, "wb") as fh:
            fh.write(key_pem)
        os.chmod(path, 0o600)
    return key


def _coord(n: int) -> str:
    return _b64u(n.to_bytes(32, "big"))


def public_jwk(priv, kid: str) -> dict:
    """Public EC P-256 JWK for the agent's `.well-known/ucp` signing_keys array."""
    nums = priv.public_key().public_numbers()
    return {"kid": kid, "kty": "EC", "crv": "P-256",
            "x": _coord(nums.x), "y": _coord(nums.y), "use": "sig", "alg": "ES256"}


def derive_kid(priv) -> str:
    """Stable key id derived from the public key (sha256(X||Y)). NOTE: this is the
    agent's OWN kid convention — the merchant looks it up by string match against
    the agent's published JWK, so it need not match the merchant's own kid scheme."""
    nums = priv.public_key().public_numbers()
    h = hashlib.sha256(nums.x.to_bytes(32, "big") + nums.y.to_bytes(32, "big")).digest()
    return "agent-" + _b64u(h)[:12]


def agent_profile_document(priv, kid: str) -> dict:
    """The document the agent serves at its profile URL so the merchant's
    httpKeyResolver can fetch + verify this key."""
    return {"signing_keys": [public_jwk(priv, kid)]}


def content_digest(body: bytes) -> str:
    return "sha-256=:" + base64.b64encode(hashlib.sha256(body).digest()).decode() + ":"


def _signature_base(resolved: dict, raw_params: str) -> str:
    lines = [f'"{c}": {resolved[c]}' for c in _COMPONENTS]
    lines.append(f'"@signature-params": {raw_params}')
    return "\n".join(lines)


def _es256_raw(priv, msg: bytes) -> bytes:
    """ES256 signature as raw r||s (64 bytes) — matches Go merchantKey.signRaw."""
    der = priv.sign(msg, ec.ECDSA(hashes.SHA256()))
    r, s = decode_dss_signature(der)
    return r.to_bytes(32, "big") + s.to_bytes(32, "big")


def sign_request(method: str, url: str, body: bytes, *, priv, kid: str,
                 profile_url: str, created: int, label: str = "sig1") -> dict:
    """Build the RFC 9421 signing headers for a single request. Deterministic
    given (method, url, body, priv, kid, profile_url, created)."""
    parts = urlsplit(url)
    digest = content_digest(body)
    resolved = {
        "@method": method.upper(),
        "@authority": parts.netloc.lower(),
        "@path": parts.path or "/",
        "content-digest": digest,
    }
    comp_list = " ".join(f'"{c}"' for c in _COMPONENTS)
    raw_params = f'({comp_list});created={created};keyid="{kid}";alg="{_ALG}"'
    sig = _es256_raw(priv, _signature_base(resolved, raw_params).encode())
    return {
        "Content-Digest": digest,
        "Signature-Input": f"{label}={raw_params}",
        "Signature": f"{label}=:{base64.b64encode(sig).decode()}:",
        "Ucp-Agent": f'profile="{profile_url}"',
    }


def signed_headers(method: str, url: str, body: bytes, *, created: int | None = None) -> dict:
    """Return RFC 9421 signing headers IF a signing key is configured, else {}
    (unsigned → byte-identical / backward-compatible). The wall-clock `created`
    is read ONLY here at the network edge — never on the var-0 itinerary path."""
    key_path = os.environ.get("UCP_CLIENT_SIGNING_KEY_PATH")
    profile_url = os.environ.get("UCP_AGENT_PROFILE_URL")
    if not key_path or not profile_url:
        return {}  # not configured → unsigned, byte-identical to today
    # FAIL-CONSERVATIVE (audit NIT#1): a misconfigured/corrupt key or a signing
    # error must NEVER crash the (money-path) request — degrade to UNSIGNED so the
    # merchant floors the caller at its unsigned tier, never an exception into checkout.
    try:
        priv = load_or_create_key(key_path)
        kid = os.environ.get("UCP_CLIENT_KID") or derive_kid(priv)
        if created is None:
            import time
            created = int(time.time())
        return sign_request(method, url, body, priv=priv, kid=kid,
                            profile_url=profile_url, created=created)
    except Exception as exc:  # noqa: BLE001 — signing is additive; never break the request
        # VULN-001-signing strict mode: re-raise so the caller fails closed.
        # Default OFF (UCP_CLIENT_SIGNING_STRICT unset) — keeps existing fail-open
        # / fail-conservative behavior (degrade to unsigned, never crash the money path).
        if os.environ.get("UCP_CLIENT_SIGNING_STRICT") == "1":
            raise
        logger.warning("ucp_signing: signing failed (sending UNSIGNED): %s", exc)
        return {}


# ---------------------------------------------------------------------------
# #161 — canonical merchant end-user identity mapper.
# ---------------------------------------------------------------------------

def merchant_checkout_owner(*, user_id: str | None, owner_token: str | None) -> str:
    """Canonical end-user id threaded to the Go merchant's checkoutSession.UserID.

    MIRRORS server.py _authorize_trip_action's tier decision so the merchant binds
    to the SAME identity the orchestrator already verifies:
      tier-1 logged-in  -> the real user_id (also the L3 device-key id).
      tier-2 anonymous  -> a STABLE, one-way per-trip pseudo-id derived from the
                           ownership SECRET (owner_token). NOT the secret itself,
                           NOT reconstructible from public trip contents (unlike
                           wallet_session_id / idempotency_key). "anon:" + a
                           truncated SHA-256 of the token.
      legacy (no owner_token, anon) -> "" -> merchant fail-open, matching
                           _authorize_trip_action's documented tier-2 rollout
                           transition (server.py _authorize_trip_action).

    Ordering (user_id first, then owner_token) is deliberate: it mirrors
    _authorize_trip_action's tier-1-first ordering exactly, so the merchant
    identity can never disagree with the gate that authorizes the action.
    """
    uid = (user_id or "").strip()
    if uid:
        return uid
    tok = (owner_token or "").strip()
    if tok:
        return "anon:" + hashlib.sha256(tok.encode("utf-8")).hexdigest()[:24]
    return ""
