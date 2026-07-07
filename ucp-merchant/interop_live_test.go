package main

import (
	"fmt"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"
)

// #56 LIVE-LOOP demo: the full signed loop over HTTP. A local agent serves its
// /.well-known/ucp profile (its JWK); the merchant's httpKeyResolver FETCHES that
// profile over loopback (permitted only under UCP_ALLOW_DEV_SSRF) and verifies a real
// Python-produced signature against the fetched key. This exercises fetch -> JWK-parse
// -> verify end-to-end (the path the frozen-fixture unit test injects around), and
// doubles as the dev-config demo: agent profile URL + UCP_ALLOW_DEV_SSRF=1.
func TestLiveLoopFetchProfileAndVerifyPythonSig(t *testing.T) {
	const (
		kid     = "agent-EHW2BA8AU622"
		created = int64(1750000000)
		jwkX    = "e-bWn1Fha_615jo5vxs80f7FS5b_jqt-ozVJekMaGIk"
		jwkY    = "f9RZ9PmE-ziC0POcwvQj2q_Vzk0p5Qshwbfgvcna9OU"
		sigIn   = `sig1=("@method" "@authority" "@path" "content-digest");created=1750000000;keyid="agent-EHW2BA8AU622";alg="ecdsa-p256-sha256"`
		sig     = `sig1=:stiApsTNHgMyAGrof+1hbMVXAbBUnj5fYSURCFE8ZEzsdaHjJ8e4qiVICFePxx2ngpEQI0eiCTsKT1arbJX0og==:`
		cdig    = `sha-256=:P1CF6yiLsA3iXglInRG9fPd1EyfL7hytQZRU3vapY78=:`
	)
	body := []byte(`{"jsonrpc":"2.0","method":"create_checkout"}`)

	// Local agent profile server (loopback http) publishing the agent's signing JWK.
	profileDoc := fmt.Sprintf(
		`{"ucp":{"capabilities":{}},"signing_keys":[{"kid":%q,"kty":"EC","crv":"P-256","x":%q,"y":%q}]}`,
		kid, jwkX, jwkY)
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(profileDoc))
	}))
	defer srv.Close()
	profileURL := srv.URL + "/.well-known/ucp" // http://127.0.0.1:PORT/...

	// PRODUCTION GUARD (OFF): the loopback http profile is refused at the https-only scheme
	// guard before dialing. (The dial-time IP block for a loopback/private *https* URL is
	// covered separately by TestSSRFBlocksPrivateIP.)
	if _, _, err := httpKeyResolver(false)(profileURL, kid); err == nil || !strings.Contains(err.Error(), "profile_url_not_https") {
		t.Fatalf("OFF: loopback http profile must be refused with profile_url_not_https; got err=%v", err)
	}

	// DEV/CI: with UCP_ALLOW_DEV_SSRF on, the merchant fetches the profile + verifies.
	v := newSigVerifier(httpKeyResolver(true))
	v.now = func() time.Time { return time.Unix(created, 0) }
	headers := map[string]string{
		"signature-input": sigIn,
		"signature":       sig,
		"content-digest":  cdig,
		"ucp-agent":       `profile="` + profileURL + `"`,
		"content-type":    "application/json",
	}
	present, id, err := v.verify("POST", "merchant.example", "/api/ucp/mcp", "", headers, body)
	if !present || err != nil {
		t.Fatalf("live-loop: fetch+verify of Python signature failed: present=%v err=%v", present, err)
	}
	if id == nil || id.Keyid != kid || !strings.HasPrefix(id.ProfileURL, "http") {
		t.Fatalf("live-loop: identity not bound to fetched profile/key: %+v", id)
	}
}
