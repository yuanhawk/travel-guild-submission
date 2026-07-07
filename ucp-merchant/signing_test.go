package main

import (
	"encoding/base64"
	"strings"
	"testing"
)

func TestSignVerifyRoundTrip(t *testing.T) {
	m, _ := loadOrCreateKey()
	jwk := m.publicJWK()
	pub, err := pubFromJWK(jwk["x"].(string), jwk["y"].(string))
	if err != nil {
		t.Fatalf("pubFromJWK: %v", err)
	}
	msg := []byte("@method: POST\n@path: /api/ucp/mcp\ncontent-digest: sha-256=:abc:")
	sig := m.signRaw(msg)
	if len(sig) != 64 {
		t.Fatalf("want 64-byte r||s sig, got %d", len(sig))
	}
	if !verifyRaw(pub, msg, sig) {
		t.Fatal("round-trip verify failed")
	}
	if verifyRaw(pub, []byte("tampered base"), sig) {
		t.Fatal("verify accepted a tampered message")
	}
}

func TestPublicJWKShape(t *testing.T) {
	m, _ := loadOrCreateKey()
	jwk := m.publicJWK()
	for _, k := range []string{"kid", "kty", "crv", "x", "y", "use", "alg"} {
		if _, ok := jwk[k]; !ok {
			t.Fatalf("jwk missing field %q", k)
		}
	}
	if jwk["kty"] != "EC" || jwk["crv"] != "P-256" || jwk["alg"] != "ES256" {
		t.Fatalf("unexpected jwk crypto fields: %+v", jwk)
	}
	// x and y must be 32-byte base64url coords.
	for _, c := range []string{"x", "y"} {
		b, err := base64.RawURLEncoding.DecodeString(jwk[c].(string))
		if err != nil || len(b) != 32 {
			t.Fatalf("coord %s not 32-byte base64url (len=%d err=%v)", c, len(b), err)
		}
	}
}

func TestContentDigestFormat(t *testing.T) {
	d := contentDigest([]byte("{}"))
	if !strings.HasPrefix(d, "sha-256=:") || !strings.HasSuffix(d, ":") {
		t.Fatalf("bad Content-Digest format: %s", d)
	}
}

func TestSignatureBase(t *testing.T) {
	vals := map[string]string{"@method": "POST", "@path": "/x", "content-digest": "sha-256=:z:"}
	base := signatureBase([]string{"@method", "@path", "content-digest"},
		func(c string) string { return vals[c] },
		`("@method" "@path" "content-digest");keyid="k1";alg="ecdsa-p256-sha256"`)
	if !strings.Contains(base, "\"@method\": POST\n") || !strings.Contains(base, "\"@signature-params\": (") {
		t.Fatalf("unexpected signature base:\n%s", base)
	}
}
