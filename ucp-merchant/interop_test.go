package main

import (
	"crypto/ecdsa"
	"crypto/elliptic"
	"crypto/rand"
	"testing"
	"time"
)

// #56 NHI cross-language interop — locks the agent->merchant wire protocol byte-for-byte.
//
// A signature produced by the REAL Python agent signer (society/utils/ucp_signing.py
// sign_request, ES256 / P-256, RFC 9421 over @method @authority @path content-digest)
// MUST verify in this Go merchant's verifier. The fixture below was generated ONCE by
// ucp_signing and is verified here with its matching public JWK injected via the resolver
// and the clock pinned to the signature's `created` (so the freshness window is satisfied
// deterministically, with NO live profile fetch — hence no SSRF/https dev-allowance needed,
// and the test stays in the fast Go unit job).
//
// DIRECTIONALITY (honest scope): a frozen fixture catches GO-side verifier drift (any
// incompatible change to verify() breaks this test loudly). The PYTHON-side wire format is
// locked separately by society/tests/test_ucp_signing.py; if that signer changes, refresh
// this fixture via the command below. Together the two sides pin the contract end-to-end.
// Regenerate the fixture with:
//
//	cd society && python3 -c "import sys;sys.path.insert(0,'.');from utils import ucp_signing as s; \
//	  p=s.load_or_create_key(None); k=s.derive_kid(p); c=1750000000; \
//	  b=b'{\"jsonrpc\":\"2.0\",\"method\":\"create_checkout\"}'; \
//	  h=s.sign_request('POST','https://merchant.example/api/ucp/mcp',b,priv=p,kid=k, \
//	     profile_url='https://agent.example/.well-known/ucp',created=c); \
//	  j=s.public_jwk(p,k); print(k,j['x'],j['y'],h['Signature-Input'],h['Signature'],h['Content-Digest'])"
func TestInteropPythonSignatureVerifiesInGoMerchant(t *testing.T) {
	const (
		kid     = "agent-EHW2BA8AU622"
		profile = "https://agent.example/.well-known/ucp"
		created = int64(1750000000)
		jwkX    = "e-bWn1Fha_615jo5vxs80f7FS5b_jqt-ozVJekMaGIk"
		jwkY    = "f9RZ9PmE-ziC0POcwvQj2q_Vzk0p5Qshwbfgvcna9OU"
		sigIn   = `sig1=("@method" "@authority" "@path" "content-digest");created=1750000000;keyid="agent-EHW2BA8AU622";alg="ecdsa-p256-sha256"`
		sig     = `sig1=:stiApsTNHgMyAGrof+1hbMVXAbBUnj5fYSURCFE8ZEzsdaHjJ8e4qiVICFePxx2ngpEQI0eiCTsKT1arbJX0og==:`
		cdig    = `sha-256=:P1CF6yiLsA3iXglInRG9fPd1EyfL7hytQZRU3vapY78=:`
	)
	body := []byte(`{"jsonrpc":"2.0","method":"create_checkout"}`)

	// Rebuild the Python agent's public key from its published JWK coords.
	pub, err := pubFromJWK(jwkX, jwkY)
	if err != nil {
		t.Fatalf("rebuild Python agent pubkey from JWK: %v", err)
	}
	v := newSigVerifier(func(string, string) (map[string]*ecdsa.PublicKey, []string, error) {
		return map[string]*ecdsa.PublicKey{kid: pub}, nil, nil
	})
	v.now = func() time.Time { return time.Unix(created, 0) }

	headers := map[string]string{
		"signature-input": sigIn,
		"signature":       sig,
		"content-digest":  cdig,
		"ucp-agent":       `profile="` + profile + `"`,
		"content-type":    "application/json",
	}
	present, id, err := v.verify("POST", "merchant.example", "/api/ucp/mcp", "", headers, body)
	if !present {
		t.Fatal("interop: Python signature reported not-present")
	}
	if err != nil {
		t.Fatalf("interop: Python-signed request FAILED Go verification: %v", err)
	}
	if id == nil || id.Keyid != kid {
		t.Fatalf("interop: identity not bound to Python keyid %q: %+v", kid, id)
	}

	// --- Negative controls (audit #56) ---------------------------------------------------
	// The tamper-body case short-circuits at the content-digest check and never reaches the
	// ECDSA verifier, so on its own it does NOT prove forgeries are rejected. The wrong-key /
	// wrong-@authority / wrong-@path cases below DO reach the signature math + base
	// construction — the real cross-language proof that Go rebuilds the signature base
	// byte-identically to Python and rejects anything that doesn't match.
	reject := func(name, method, authority, path string, res KeyResolver, b []byte) {
		vv := newSigVerifier(res)
		vv.now = func() time.Time { return time.Unix(created, 0) }
		if _, _, e := vv.verify(method, authority, path, "", headers, b); e == nil {
			t.Fatalf("interop negative %q: forged/mismatched request unexpectedly VERIFIED", name)
		}
	}
	goodKey := func(string, string) (map[string]*ecdsa.PublicKey, []string, error) {
		return map[string]*ecdsa.PublicKey{kid: pub}, nil, nil
	}
	// (1) tampered body -> content-digest mismatch.
	reject("tampered-body", "POST", "merchant.example", "/api/ucp/mcp", goodKey, []byte("tampered"))
	// (2) wrong signing key -> ECDSA verify must reject (reaches verifyRaw, not the digest gate).
	other, err := ecdsa.GenerateKey(elliptic.P256(), rand.Reader)
	if err != nil {
		t.Fatalf("gen wrong key: %v", err)
	}
	reject("wrong-key", "POST", "merchant.example", "/api/ucp/mcp",
		func(string, string) (map[string]*ecdsa.PublicKey, []string, error) {
			return map[string]*ecdsa.PublicKey{kid: &other.PublicKey}, nil, nil
		}, body)
	// (3) wrong @authority -> signature base differs from what Python signed -> ECDSA reject
	//     (digest still matches since body is unchanged; this isolates base construction).
	reject("wrong-authority", "POST", "evil.example", "/api/ucp/mcp", goodKey, body)
	// (4) wrong @path -> base mismatch -> ECDSA reject.
	reject("wrong-path", "POST", "merchant.example", "/api/ucp/mcp/EVIL", goodKey, body)
}
