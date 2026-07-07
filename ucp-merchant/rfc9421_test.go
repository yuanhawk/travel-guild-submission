package main

import (
	"crypto/ecdsa"
	"encoding/base64"
	"fmt"
	"net"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync/atomic"
	"testing"
	"time"
)

// signReq builds a valid RFC 9421 signature covering @method, @authority, @path,
// and content-digest (RFC9421-004 requires all four). The authority parameter is
// the bare host:port (or host) — must match r.Host in the verifier call.
func signReq(mk *merchantKey, profile, method, authority, path string, body []byte, created int64) map[string]string {
	h := map[string]string{
		"content-type":   "application/json",
		"content-digest": contentDigest(body),
		"ucp-agent":      `profile="` + profile + `"`,
	}
	comps := []string{"@method", "@authority", "@path", "content-digest"}
	raw := fmt.Sprintf(`("@method" "@authority" "@path" "content-digest");keyid="%s";created=%d;alg="ecdsa-p256-sha256"`, mk.kid, created)
	rc := func(c string) string {
		switch c {
		case "@method":
			return strings.ToUpper(method)
		case "@authority":
			return strings.ToLower(authority)
		case "@path":
			return path
		default:
			return h[c]
		}
	}
	sig := mk.signRaw([]byte(signatureBase(comps, rc, raw)))
	h["signature-input"] = "sig1=" + raw
	h["signature"] = "sig1=:" + base64.StdEncoding.EncodeToString(sig) + ":"
	return h
}

func testVerifier(mk *merchantKey, base time.Time, caps []string) *sigVerifier {
	jwk := mk.publicJWK()
	pub, _ := pubFromJWK(jwk["x"].(string), jwk["y"].(string))
	v := newSigVerifier(func(string, string) (map[string]*ecdsa.PublicKey, []string, error) {
		return map[string]*ecdsa.PublicKey{mk.kid: pub}, caps, nil
	})
	v.now = func() time.Time { return base }
	return v
}

func TestVerifierValidAndReplay(t *testing.T) {
	mk, _ := loadOrCreateKey()
	base := time.Unix(1_800_000_000, 0)
	v := testVerifier(mk, base, []string{"dev.ucp.shopping.catalog"})
	body := []byte(`{"jsonrpc":"2.0"}`)
	h := signReq(mk, "https://agent.test/.well-known/ucp", "POST", "merchant.example", "/api/ucp/mcp", body, base.Unix())

	present, id, err := v.verify("POST", "merchant.example", "/api/ucp/mcp", "", h, body)
	if !present || err != nil || id == nil || id.ProfileURL != "https://agent.test/.well-known/ucp" {
		t.Fatalf("valid sig: present=%v id=%v err=%v", present, id, err)
	}
	if _, _, err := v.verify("POST", "merchant.example", "/api/ucp/mcp", "", h, body); err == nil {
		t.Fatal("replayed signature accepted")
	}
}

func TestVerifierRejections(t *testing.T) {
	mk, _ := loadOrCreateKey()
	base := time.Unix(1_800_000_000, 0)
	body := []byte(`{}`)

	// Stale created.
	hs := signReq(mk, "https://a/.well-known/ucp", "POST", "m", "/p", body, base.Unix()-10000)
	if _, _, err := testVerifier(mk, base, nil).verify("POST", "m", "/p", "", hs, body); err == nil {
		t.Fatal("stale signature accepted")
	}
	// Absent -> present=false.
	if present, _, _ := testVerifier(mk, base, nil).verify("POST", "m", "/p", "", map[string]string{}, body); present {
		t.Fatal("absent signature should be present=false")
	}
	// Digest mismatch.
	hd := signReq(mk, "https://a/.well-known/ucp", "POST", "m", "/p", body, base.Unix())
	if _, _, err := testVerifier(mk, base, nil).verify("POST", "m", "/p", "", hd, []byte("tampered")); err == nil {
		t.Fatal("digest mismatch accepted")
	}
	// Disallowed alg.
	ha := signReq(mk, "https://a/.well-known/ucp", "POST", "m", "/p", body, base.Unix())
	ha["signature-input"] = strings.Replace(ha["signature-input"], "ecdsa-p256-sha256", "rsa-pss-sha512", 1)
	if _, _, err := testVerifier(mk, base, nil).verify("POST", "m", "/p", "", ha, body); err == nil {
		t.Fatal("disallowed alg accepted")
	}
}

// L4(a): a resolver that successfully returns a non-empty keyset, but one that
// does NOT contain the request's kid, must be rejected as key_not_found (not
// mistaken for profile_unreachable or silently accepted).
func TestVerifierKeyNotFoundRejected(t *testing.T) {
	mk, _ := loadOrCreateKey()
	other, _ := loadOrCreateKey() // a second, unrelated keypair with a different kid
	base := time.Unix(1_800_000_000, 0)
	body := []byte(`{}`)
	h := signReq(mk, "https://a/.well-known/ucp", "POST", "m", "/p", body, base.Unix())

	otherJWK := other.publicJWK()
	otherPub, _ := pubFromJWK(otherJWK["x"].(string), otherJWK["y"].(string))
	v := newSigVerifier(func(string, string) (map[string]*ecdsa.PublicKey, []string, error) {
		// Non-empty keyset that simply lacks mk.kid — e.g. the profile was
		// fetched fine but only advertises an unrelated kid.
		return map[string]*ecdsa.PublicKey{other.kid: otherPub}, nil, nil
	})
	v.now = func() time.Time { return base }

	if _, _, err := v.verify("POST", "m", "/p", "", h, body); err == nil || err.Error() != "key_not_found" {
		t.Fatalf("expected key_not_found, got %v", err)
	}
}

// L4(b): a signature whose "created" is in the future beyond the allowed skew
// must be rejected as stale, mirroring the existing past-side staleness check.
func TestVerifierFutureSkewRejected(t *testing.T) {
	mk, _ := loadOrCreateKey()
	base := time.Unix(1_800_000_000, 0)
	body := []byte(`{}`)
	v := testVerifier(mk, base, nil)

	future := base.Unix() + 2*v.skewSec
	h := signReq(mk, "https://a/.well-known/ucp", "POST", "m", "/p", body, future)
	if _, _, err := v.verify("POST", "m", "/p", "", h, body); err == nil || err.Error() != "signature_stale" {
		t.Fatalf("expected signature_stale for future-skewed created, got %v", err)
	}
}

// L2 regression: httpKeyResolver must not keep serving a cached keyset that
// lacks the requested kid for the full cache TTL — it should transparently
// refetch (bypassing the cache) once when the caller's kid is missing, so a
// key rotated in after the first fetch becomes usable without waiting ~ttl.
func TestHTTPKeyResolverRefetchesOnKeyMiss(t *testing.T) {
	mk, _ := loadOrCreateKey()
	jwk := mk.publicJWK()

	var fetches int32
	docV1 := `{"ucp":{"capabilities":{}},"signing_keys":[]}`
	docV2 := fmt.Sprintf(`{"ucp":{"capabilities":{}},"signing_keys":[{"kid":%q,"kty":"EC","crv":"P-256","x":%q,"y":%q}]}`,
		mk.kid, jwk["x"], jwk["y"])

	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		n := atomic.AddInt32(&fetches, 1)
		w.Header().Set("Content-Type", "application/json")
		if n == 1 {
			_, _ = w.Write([]byte(docV1)) // no keys yet
		} else {
			_, _ = w.Write([]byte(docV2)) // key rotated in
		}
	}))
	defer srv.Close()

	// allowDevSSRF=true: permits the loopback http profile server used by this test
	// (mirrors the pattern in interop_live_test.go's live-loop fetch test).
	resolve := httpKeyResolver(true)

	// First lookup: profile has no keys yet -> populates cache with an empty set.
	keys, _, err := resolve(srv.URL, mk.kid)
	if err != nil {
		t.Fatalf("first resolve: %v", err)
	}
	if _, ok := keys[mk.kid]; ok {
		t.Fatal("kid should not be present yet on first fetch")
	}
	if got := atomic.LoadInt32(&fetches); got != 1 {
		t.Fatalf("expected 1 fetch, got %d", got)
	}

	// Second lookup for the SAME kid: the cached entry (still within ttl) lacks
	// it, so this must trigger a real refetch rather than trusting the cache.
	keys, _, err = resolve(srv.URL, mk.kid)
	if err != nil {
		t.Fatalf("second resolve: %v", err)
	}
	if _, ok := keys[mk.kid]; !ok {
		t.Fatal("rotated-in kid should be picked up after refetch-on-miss")
	}
	if got := atomic.LoadInt32(&fetches); got != 2 {
		t.Fatalf("expected refetch-on-miss to hit the network again (2 fetches total), got %d", got)
	}

	// Third lookup for the same kid: now cached and present -> no extra fetch.
	if _, _, err := resolve(srv.URL, mk.kid); err != nil {
		t.Fatalf("third resolve: %v", err)
	}
	if got := atomic.LoadInt32(&fetches); got != 2 {
		t.Fatalf("expected cache hit (still 2 fetches total), got %d", got)
	}
}

func TestParseProfileURL(t *testing.T) {
	if got := parseProfileURL(`profile="https://a.example/.well-known/ucp"`); got != "https://a.example/.well-known/ucp" {
		t.Fatalf("got %q", got)
	}
}

func TestSSRFBlocksPrivateIP(t *testing.T) {
	if !isBlockedIP(net.ParseIP("127.0.0.1")) || !isBlockedIP(net.ParseIP("169.254.169.254")) || !isBlockedIP(net.ParseIP("10.1.2.3")) {
		t.Fatal("private/loopback/link-local IPs must be blocked")
	}
	if isBlockedIP(net.ParseIP("8.8.8.8")) {
		t.Fatal("public IP should not be blocked")
	}
}
