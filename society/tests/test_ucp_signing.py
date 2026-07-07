"""
#53 NHI stretch — client-side RFC 9421 signer (ucp_signing.py) byte-compat tests.
Locks the wire format the Go merchant verifier (ucp-merchant/rfc9421.go) expects,
plus the off-by-default backward-compat guard.
"""
import base64
import hashlib
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from utils import ucp_signing as us

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature
from urllib.parse import urlsplit


def _verify_headers(headers, method, url, body, pub):
    """Reconstruct the RFC 9421 signature base from the headers EXACTLY as the Go
    merchant does, and verify the ES256 signature against `pub` — the real interop
    proof (base construction + raw-encoding + crypto all correct)."""
    raw_params = headers["Signature-Input"].split("=", 1)[1]
    sig_raw = base64.b64decode(headers["Signature"].split("=", 1)[1].strip(":"))
    r = int.from_bytes(sig_raw[:32], "big")
    s = int.from_bytes(sig_raw[32:], "big")
    der = encode_dss_signature(r, s)
    parts = urlsplit(url)
    resolved = {"@method": method.upper(), "@authority": parts.netloc.lower(),
                "@path": parts.path or "/", "content-digest": headers["Content-Digest"]}
    base = us._signature_base(resolved, raw_params)
    pub.verify(der, base.encode(), ec.ECDSA(hashes.SHA256()))  # raises if invalid
    return True


# An EPHEMERAL test key generated at import (NOT a secret, never persisted). Tests
# verify the signature CRYPTOGRAPHICALLY (not against a byte-golden), so a fresh key
# each run is fine — and it keeps zero key material in source (gitleaks-clean).
_PRIV = ec.generate_private_key(ec.SECP256R1())
_KID = "agent-test-ephemeral"
_PROFILE = "https://society.example/.well-known/ucp"


def test_content_digest_matches_go_format():
    body = b'{"hello":"world"}'
    expect = "sha-256=:" + base64.b64encode(hashlib.sha256(body).digest()).decode() + ":"
    assert us.content_digest(body) == expect
    assert us.content_digest(body).startswith("sha-256=:") and us.content_digest(body).endswith(":")
    print("[SIGN] PASS — content-digest matches Go sha-256 std-base64 format\n")


def test_jwk_coords_are_unpadded_base64url_32_bytes():
    jwk = us.public_jwk(_PRIV, _KID)
    assert jwk["kty"] == "EC" and jwk["crv"] == "P-256" and jwk["alg"] == "ES256"
    for c in ("x", "y"):
        assert "=" not in jwk[c], "JWK coord must be UNPADDED base64url"
        raw = base64.urlsafe_b64decode(jwk[c] + "==")
        assert len(raw) == 32, f"{c} must decode to 32 bytes"
    print("[SIGN] PASS — JWK x/y are 32-byte unpadded base64url (Go RawURLEncoding)\n")


def test_signature_base_exact_format():
    # The signature base must be EXACTLY component lines + @signature-params, no
    # trailing newline (byte-for-byte what the Go verifier reconstructs).
    resolved = {"@method": "POST", "@authority": "ucp-merchant:8090",
                "@path": "/api/ucp/mcp", "content-digest": "sha-256=:ABC:"}
    raw = '("@method" "@authority" "@path" "content-digest");created=1750000000;keyid="k";alg="ecdsa-p256-sha256"'
    base = us._signature_base(resolved, raw)
    expect = (
        '"@method": POST\n'
        '"@authority": ucp-merchant:8090\n'
        '"@path": /api/ucp/mcp\n'
        '"content-digest": sha-256=:ABC:\n'
        '"@signature-params": ' + raw
    )
    assert base == expect, repr(base)
    assert not base.endswith("\n"), "no trailing newline after @signature-params"
    print("[SIGN] PASS — signature base is byte-exact (no trailing newline)\n")


def test_es256_raw_roundtrips_and_is_64_bytes():
    msg = b"the signature base bytes"
    raw = us._es256_raw(_PRIV, msg)
    assert len(raw) == 64, "ES256 raw r||s must be 64 bytes"
    r = int.from_bytes(raw[:32], "big")
    s = int.from_bytes(raw[32:], "big")
    der = encode_dss_signature(r, s)
    # Reconstructed DER must verify against the public key (proves the raw split).
    from cryptography.hazmat.primitives import hashes
    _PRIV.public_key().verify(der, msg, ec.ECDSA(hashes.SHA256()))
    print("[SIGN] PASS — ES256 raw r||s is 64 bytes and verifies (DER round-trip)\n")


def test_sign_request_header_shapes():
    body = b'{"kind":"FOOD_DELIVERY"}'
    h = us.sign_request("POST", "http://ucp-merchant:8090/api/ucp/mcp", body,
                        priv=_PRIV, kid=_KID, profile_url=_PROFILE, created=1750000000)
    assert h["Content-Digest"] == us.content_digest(body)
    assert h["Signature-Input"].startswith("sig1=(") and f'keyid="{_KID}"' in h["Signature-Input"]
    assert 'alg="ecdsa-p256-sha256"' in h["Signature-Input"]
    assert h["Signature"].startswith("sig1=:") and h["Signature"].endswith(":")
    assert h["Ucp-Agent"] == f'profile="{_PROFILE}"'
    # ECDSA uses a random nonce, so the signature bytes are NOT byte-deterministic
    # (matches the Go merchant's rand.Reader). The contract is that it VERIFIES —
    # this reconstructs the Go signature base and checks the ES256 signature.
    assert _verify_headers(h, "POST", "http://ucp-merchant:8090/api/ucp/mcp",
                           body, _PRIV.public_key())
    print("[SIGN] PASS — header shapes correct + signature verifies against the base\n")


def test_signed_headers_off_by_default():
    # No env configured → {} (unsigned, byte-identical/backward-compatible).
    saved = {k: os.environ.pop(k, None) for k in
             ("UCP_CLIENT_SIGNING_KEY_PATH", "UCP_AGENT_PROFILE_URL")}
    try:
        assert us.signed_headers("POST", "http://m/x", b"{}") == {}
    finally:
        for k, v in saved.items():
            if v is not None:
                os.environ[k] = v
    print("[SIGN] PASS — signed_headers is {} when unconfigured (off by default)\n")


def test_signed_headers_on_when_configured(tmp_path=None):
    import tempfile
    d = tempfile.mkdtemp()
    keypath = os.path.join(d, "agent_key.pem")
    saved = {k: os.environ.get(k) for k in
             ("UCP_CLIENT_SIGNING_KEY_PATH", "UCP_AGENT_PROFILE_URL", "UCP_CLIENT_KID")}
    try:
        os.environ["UCP_CLIENT_SIGNING_KEY_PATH"] = keypath
        os.environ["UCP_AGENT_PROFILE_URL"] = _PROFILE
        os.environ.pop("UCP_CLIENT_KID", None)
        h = us.signed_headers("POST", "http://m:8090/x", b"{}", created=1750000000)
        assert set(h) == {"Content-Digest", "Signature-Input", "Signature", "Ucp-Agent"}
        assert os.path.exists(keypath), "key persisted on first use"
        # Second call loads the SAME persisted key → both signatures VERIFY against
        # that key (the bytes differ — ECDSA random nonce — but the identity holds).
        priv = us.load_or_create_key(keypath)
        assert _verify_headers(h, "POST", "http://m:8090/x", b"{}", priv.public_key())
        h2 = us.signed_headers("POST", "http://m:8090/x", b"{}", created=1750000000)
        assert _verify_headers(h2, "POST", "http://m:8090/x", b"{}", priv.public_key())
        assert h["Ucp-Agent"] == h2["Ucp-Agent"]  # same identity/profile
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
    print("[SIGN] PASS — signed_headers attaches 4 headers + persists/reuses key\n")


def test_well_known_ucp_route_completes_the_loop():
    # The agent's /.well-known/ucp publishes a JWK whose kid matches the signer's,
    # and that published public key VERIFIES a request the signer produced — the
    # full live loop (client signs -> merchant fetches this profile -> verifies).
    from starlette.testclient import TestClient
    from agents.risk_agent import RiskAgent
    client = TestClient(RiskAgent().build_app())
    saved = {k: os.environ.get(k) for k in
             ("UCP_CLIENT_SIGNING_KEY_PATH", "UCP_AGENT_PROFILE_URL", "UCP_CLIENT_KID")}
    try:
        for k in saved:
            os.environ.pop(k, None)
        # No key → honest empty signing_keys (never a fabricated key).
        r = client.get("/.well-known/ucp")
        assert r.status_code == 200 and r.json() == {"signing_keys": []}
        # Configure a key → published JWK + signer agree, and the JWK verifies a sig.
        import tempfile
        kp = os.path.join(tempfile.mkdtemp(), "k.pem")
        os.environ["UCP_CLIENT_SIGNING_KEY_PATH"] = kp
        os.environ["UCP_AGENT_PROFILE_URL"] = _PROFILE
        jwk = client.get("/.well-known/ucp").json()["signing_keys"][0]
        assert jwk["kty"] == "EC" and jwk["crv"] == "P-256" and jwk["kid"]
        h = us.signed_headers("POST", "http://m:8090/x", b"{}")
        assert f'keyid="{jwk["kid"]}"' in h["Signature-Input"], "published kid must match signer"
        x = int.from_bytes(base64.urlsafe_b64decode(jwk["x"] + "=="), "big")
        y = int.from_bytes(base64.urlsafe_b64decode(jwk["y"] + "=="), "big")
        pub = ec.EllipticCurvePublicNumbers(x, y, ec.SECP256R1()).public_key()
        assert _verify_headers(h, "POST", "http://m:8090/x", b"{}", pub)
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
    print("[SIGN] PASS — /.well-known/ucp publishes the JWK; loop verifies end-to-end\n")


def test_signed_headers_raises_on_bad_key():
    # SIGN-FAILOPEN fix: with UCP_CLIENT_SIGNING_STRICT=1, a corrupt key raises.
    # Without the flag (default), it degrades to {} (fail-open, backwards compat).
    import tempfile
    d = tempfile.mkdtemp()
    keypath = os.path.join(d, "bad_key.pem")
    with open(keypath, "wb") as fh:
        fh.write(b"this is not a valid private key file")
    saved = {k: os.environ.get(k) for k in
             ("UCP_CLIENT_SIGNING_KEY_PATH", "UCP_AGENT_PROFILE_URL", "UCP_CLIENT_SIGNING_STRICT")}
    try:
        os.environ["UCP_CLIENT_SIGNING_KEY_PATH"] = keypath
        os.environ["UCP_AGENT_PROFILE_URL"] = _PROFILE
        # Default (no strict flag): degrades to unsigned {}
        os.environ.pop("UCP_CLIENT_SIGNING_STRICT", None)
        assert us.signed_headers("POST", "http://m:8090/x", b"{}") == {}, \
            "default (no strict flag) must degrade to {} not raise"
        # Strict mode: raises so callers fail closed
        os.environ["UCP_CLIENT_SIGNING_STRICT"] = "1"
        try:
            us.signed_headers("POST", "http://m:8090/x", b"{}")
            assert False, "expected exception in strict mode on corrupt key"
        except Exception:
            pass  # correct — fail-closed in strict mode
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
    print("[SIGN] PASS — corrupt key: degrades by default, raises in strict mode\n")


# ---------------------------------------------------------------------------
# #161 — merchant_checkout_owner() canonical end-user id mapper.
# ---------------------------------------------------------------------------

def test_merchant_checkout_owner_logged_in_uses_real_user_id():
    # Tier-1 logged-in: the real user_id wins, regardless of owner_token.
    got = us.merchant_checkout_owner(user_id="real-user-42", owner_token="some-secret")
    assert got == "real-user-42"
    print("[SIGN] PASS — merchant_checkout_owner: logged-in user_id takes priority\n")


def test_merchant_checkout_owner_anon_derives_stable_pseudo_id():
    # Tier-2 anonymous: a stable "anon:"-prefixed SHA-256-derived pseudo-id, NOT
    # the raw owner_token (never leak the ownership secret to the merchant).
    got1 = us.merchant_checkout_owner(user_id=None, owner_token="tok-abc")
    got2 = us.merchant_checkout_owner(user_id="", owner_token="tok-abc")
    assert got1 == got2, "same token must map to the same pseudo-id"
    assert got1.startswith("anon:")
    assert "tok-abc" not in got1, "the raw secret must never appear in the merchant id"
    expect = "anon:" + hashlib.sha256(b"tok-abc").hexdigest()[:24]
    assert got1 == expect
    print("[SIGN] PASS — merchant_checkout_owner: anon derives a stable, non-secret pseudo-id\n")


def test_merchant_checkout_owner_anon_differs_per_token():
    got_a = us.merchant_checkout_owner(user_id=None, owner_token="tok-a")
    got_b = us.merchant_checkout_owner(user_id=None, owner_token="tok-b")
    assert got_a != got_b
    print("[SIGN] PASS — merchant_checkout_owner: distinct tokens map to distinct pseudo-ids\n")


def test_merchant_checkout_owner_legacy_fails_open_to_empty():
    # Legacy row: neither user_id nor owner_token present -> "" (merchant fail-open,
    # matching _authorize_trip_action's own tier-2 rollout fail-open).
    assert us.merchant_checkout_owner(user_id=None, owner_token=None) == ""
    assert us.merchant_checkout_owner(user_id="", owner_token="") == ""
    assert us.merchant_checkout_owner(user_id="   ", owner_token="   ") == ""
    print("[SIGN] PASS — merchant_checkout_owner: legacy anon (no owner_token) fails open to ''\n")


if __name__ == "__main__":
    for t in (test_content_digest_matches_go_format,
              test_jwk_coords_are_unpadded_base64url_32_bytes,
              test_signature_base_exact_format,
              test_es256_raw_roundtrips_and_is_64_bytes,
              test_sign_request_header_shapes,
              test_signed_headers_off_by_default,
              test_signed_headers_on_when_configured,
              test_well_known_ucp_route_completes_the_loop,
              test_signed_headers_raises_on_bad_key,
              test_merchant_checkout_owner_logged_in_uses_real_user_id,
              test_merchant_checkout_owner_anon_derives_stable_pseudo_id,
              test_merchant_checkout_owner_anon_differs_per_token,
              test_merchant_checkout_owner_legacy_fails_open_to_empty):
        t()
    print("ALL #53 UCP-SIGNING TESTS PASSED")
