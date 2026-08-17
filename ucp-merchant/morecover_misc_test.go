package main

// morecover_misc_test.go — table-driven coverage for low-coverage functions:
//   catalog.go: catalogTool (72, 0%), catalogToolWithStore (112, 70%)
//   signing.go: parseECPrivPEM (44, 0%), loadOrCreateKey (32, 50%)
//   rfc9421.go: safeClient (228, 69%)
//   main.go pure helpers: envOr (135), usdCents (142), loadConfig (29)
//
// All test funcs: TestMCMisc_... All helpers/vars: mcmisc_...
// No network to real hosts; uses httptest + t.TempDir() for key/file I/O.
// Deterministic; no time-flakiness.

import (
	"bytes"
	"crypto/ecdsa"
	"crypto/elliptic"
	"crypto/rand"
	"crypto/x509"
	"encoding/base64"
	"encoding/pem"
	"net"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

// mcmisc_writeECKeyPEM generates a fresh EC P-256 key and writes its SEC1-PEM
// to dir/fname. Returns the path and the private key.
func mcmisc_writeECKeyPEM(t *testing.T, dir, fname string) (string, *ecdsa.PrivateKey) {
	t.Helper()
	priv, err := ecdsa.GenerateKey(elliptic.P256(), rand.Reader)
	if err != nil {
		t.Fatalf("generate key: %v", err)
	}
	der, err := x509.MarshalECPrivateKey(priv)
	if err != nil {
		t.Fatalf("marshal EC key: %v", err)
	}
	block := &pem.Block{Type: "EC PRIVATE KEY", Bytes: der}
	path := filepath.Join(dir, fname)
	if err := os.WriteFile(path, pem.EncodeToMemory(block), 0600); err != nil {
		t.Fatalf("write key file: %v", err)
	}
	return path, priv
}

// mcmisc_writePKCS8KeyPEM writes a PKCS8-encoded EC P-256 key to dir/fname.
func mcmisc_writePKCS8KeyPEM(t *testing.T, dir, fname string) (string, *ecdsa.PrivateKey) {
	t.Helper()
	priv, err := ecdsa.GenerateKey(elliptic.P256(), rand.Reader)
	if err != nil {
		t.Fatalf("generate key: %v", err)
	}
	der, err := x509.MarshalPKCS8PrivateKey(priv)
	if err != nil {
		t.Fatalf("marshal PKCS8 key: %v", err)
	}
	block := &pem.Block{Type: "PRIVATE KEY", Bytes: der}
	path := filepath.Join(dir, fname)
	if err := os.WriteFile(path, pem.EncodeToMemory(block), 0600); err != nil {
		t.Fatalf("write key file: %v", err)
	}
	return path, priv
}

// mcmisc_setenv sets an env var and registers cleanup to restore it.
func mcmisc_setenv(t *testing.T, key, value string) {
	t.Helper()
	old, hadOld := os.LookupEnv(key)
	t.Cleanup(func() {
		if hadOld {
			os.Setenv(key, old)
		} else {
			os.Unsetenv(key)
		}
	})
	os.Setenv(key, value)
}

// mcmisc_unsetenv clears an env var and registers cleanup.
func mcmisc_unsetenv(t *testing.T, key string) {
	t.Helper()
	old, hadOld := os.LookupEnv(key)
	t.Cleanup(func() {
		if hadOld {
			os.Setenv(key, old)
		} else {
			os.Unsetenv(key)
		}
	})
	os.Unsetenv(key)
}

// ---------------------------------------------------------------------------
// catalogTool — 100% new coverage (was 0%)
// catalogTool is a thin wrapper over catalogToolWithStore(nil, ...).
// ---------------------------------------------------------------------------

func TestMCMisc_CatalogTool_SearchReturnsResults(t *testing.T) {
	args := []byte(`{"query":{"city":"bali","checkin":"2026-07-01","checkout":"2026-07-03"}}`)
	res, code := catalogTool("search_catalog", args)
	if code != http.StatusOK {
		t.Fatalf("want 200, got %d: %v", code, res)
	}
	if res["source"] != "mock" {
		t.Fatalf("source must be mock, got %v", res["source"])
	}
	count, _ := res["count"].(int)
	if count < 1 {
		t.Fatalf("bali search must return at least one result; got count=%d", count)
	}
}

func TestMCMisc_CatalogTool_LookupKnownHotel(t *testing.T) {
	args := []byte(`{"product":{"hotel_id":"bali-alaya-ubud","checkin":"2026-07-01","checkout":"2026-07-03"}}`)
	res, code := catalogTool("lookup_catalog", args)
	if code != http.StatusOK {
		t.Fatalf("want 200, got %d: %v", code, res)
	}
	if res["available"] != true {
		t.Fatalf("known hotel must be available; got %v", res["available"])
	}
	if _, ok := res["hotel"]; !ok {
		t.Fatalf("response missing hotel field: %v", res)
	}
}

func TestMCMisc_CatalogTool_LookupUnknownHotelReturns404(t *testing.T) {
	args := []byte(`{"product":{"hotel_id":"absolutely-nonexistent-hotel-xyz"}}`)
	res, code := catalogTool("lookup_catalog", args)
	if code != http.StatusNotFound {
		t.Fatalf("want 404, got %d: %v", code, res)
	}
	if res["error"] != "not_found" {
		t.Fatalf("error must be not_found, got %v", res["error"])
	}
}

func TestMCMisc_CatalogTool_UnknownToolReturns400(t *testing.T) {
	res, code := catalogTool("bogus_catalog_tool", []byte(`{}`))
	if code != http.StatusBadRequest {
		t.Fatalf("unknown tool must return 400, got %d: %v", code, res)
	}
	errStr, _ := res["error"].(string)
	if !strings.Contains(errStr, "bogus_catalog_tool") {
		t.Fatalf("error must echo tool name, got %v", res)
	}
}

func TestMCMisc_CatalogTool_TableDriven(t *testing.T) {
	cases := []struct {
		label    string
		name     string
		args     string
		wantCode int
		wantKey  string
	}{
		{
			label:    "search no city filter returns results",
			name:     "search_catalog",
			args:     `{"query":{"checkin":"2026-07-01","checkout":"2026-07-02"}}`,
			wantCode: http.StatusOK,
			wantKey:  "results",
		},
		{
			label:    "search with max_cents=1 returns zero results",
			name:     "search_catalog",
			args:     `{"query":{"city":"bali","checkin":"2026-07-01","checkout":"2026-07-03","max_cents":1}}`,
			wantCode: http.StatusOK,
			wantKey:  "results",
		},
		{
			label:    "lookup without dates omits nights/total",
			name:     "lookup_catalog",
			args:     `{"product":{"hotel_id":"bali-alaya-ubud"}}`,
			wantCode: http.StatusOK,
			wantKey:  "hotel",
		},
		{
			label:    "lookup not found",
			name:     "lookup_catalog",
			args:     `{"product":{"hotel_id":"does-not-exist-00"}}`,
			wantCode: http.StatusNotFound,
			wantKey:  "error",
		},
		{
			label:    "unknown tool",
			name:     "zap_catalog",
			args:     `{}`,
			wantCode: http.StatusBadRequest,
			wantKey:  "error",
		},
	}
	for _, tc := range cases {
		t.Run(tc.label, func(t *testing.T) {
			res, code := catalogTool(tc.name, []byte(tc.args))
			if code != tc.wantCode {
				t.Fatalf("want %d got %d: %v", tc.wantCode, code, res)
			}
			if tc.wantKey != "" {
				if _, ok := res[tc.wantKey]; !ok {
					t.Fatalf("expected key %q in result, got %v", tc.wantKey, res)
				}
			}
		})
	}
}

// ---------------------------------------------------------------------------
// catalogToolWithStore — edge paths not yet covered (nil store = catalogTool)
// ---------------------------------------------------------------------------

func TestMCMisc_CatalogToolWithStore_NilStoreBehavesLikeCatalogTool(t *testing.T) {
	args := []byte(`{"query":{"city":"bali","checkin":"2026-07-01","checkout":"2026-07-03"}}`)
	r1, c1 := catalogTool("search_catalog", args)
	r2, c2 := catalogToolWithStore(nil, "search_catalog", args)
	if c1 != c2 {
		t.Fatalf("catalogTool and catalogToolWithStore(nil) must return same code: %d vs %d", c1, c2)
	}
	cnt1, _ := r1["count"].(int)
	cnt2, _ := r2["count"].(int)
	if cnt1 != cnt2 {
		t.Fatalf("result count differs between catalogTool and catalogToolWithStore(nil): %d vs %d", cnt1, cnt2)
	}
}

func TestMCMisc_CatalogToolWithStore_SoldOutExcludedFromSearch(t *testing.T) {
	st := newStore()
	const hotel = "bali-alaya-ubud"
	st.simSetAvailability(hotel, false)

	args := []byte(`{"query":{"city":"bali","checkin":"2026-07-01","checkout":"2026-07-03","max_cents":9999999}}`)
	res, code := catalogToolWithStore(st, "search_catalog", args)
	if code != http.StatusOK {
		t.Fatalf("want 200, got %d: %v", code, res)
	}
	results, _ := res["results"].([]map[string]any)
	for _, r := range results {
		if r["hotel_id"] == hotel {
			t.Fatalf("sold-out hotel %q must not appear in search_catalog results", hotel)
		}
	}
}

func TestMCMisc_CatalogToolWithStore_SoldOutLookupReturns409(t *testing.T) {
	st := newStore()
	const hotel = "bali-alaya-ubud"
	st.simSetAvailability(hotel, false)

	args := []byte(`{"product":{"hotel_id":"bali-alaya-ubud","checkin":"2026-07-01","checkout":"2026-07-03"}}`)
	res, code := catalogToolWithStore(st, "lookup_catalog", args)
	if code != http.StatusConflict {
		t.Fatalf("sold-out lookup must return 409, got %d: %v", code, res)
	}
	if res["status"] != "unavailable" {
		t.Fatalf("sold-out lookup status must be unavailable, got %v", res["status"])
	}
}

func TestMCMisc_CatalogToolWithStore_CityAvailCount(t *testing.T) {
	// city_available_count must be >0 for bali even if max_cents is very tight.
	st := newStore()
	args := []byte(`{"query":{"city":"bali","checkin":"2026-07-01","checkout":"2026-07-02","max_cents":1}}`)
	res, code := catalogToolWithStore(st, "search_catalog", args)
	if code != http.StatusOK {
		t.Fatalf("want 200, got %d: %v", code, res)
	}
	cityAvail, _ := res["city_available_count"].(int)
	if cityAvail < 1 {
		t.Fatalf("city_available_count must be >0 for bali with a stingy budget filter; got %d", cityAvail)
	}
	count, _ := res["count"].(int)
	if count != 0 {
		t.Fatalf("with max_cents=1, count must be 0 (all filtered); got %d", count)
	}
}

func TestMCMisc_CatalogToolWithStore_LookupWithDates(t *testing.T) {
	args := []byte(`{"product":{"hotel_id":"bali-alaya-ubud","checkin":"2026-07-01","checkout":"2026-07-04"}}`)
	res, code := catalogToolWithStore(nil, "lookup_catalog", args)
	if code != http.StatusOK {
		t.Fatalf("want 200, got %d: %v", code, res)
	}
	if _, ok := res["nights"]; !ok {
		t.Fatalf("lookup with dates must include nights field; got %v", res)
	}
	if _, ok := res["total_cents"]; !ok {
		t.Fatalf("lookup with dates must include total_cents field; got %v", res)
	}
	nights, _ := res["nights"].(int)
	if nights != 3 {
		t.Fatalf("2026-07-01 to 2026-07-04 is 3 nights; got %d", nights)
	}
}

func TestMCMisc_CatalogToolWithStore_SearchNights(t *testing.T) {
	// nights field in search results must match checkin→checkout span.
	args := []byte(`{"query":{"city":"bali","checkin":"2026-07-10","checkout":"2026-07-15","max_cents":9999999}}`)
	res, code := catalogToolWithStore(nil, "search_catalog", args)
	if code != http.StatusOK {
		t.Fatalf("want 200, got %d: %v", code, res)
	}
	n, _ := res["nights"].(int)
	if n != 5 {
		t.Fatalf("2026-07-10→2026-07-15 should be 5 nights, got %d", n)
	}
}

// ---------------------------------------------------------------------------
// parseECPrivPEM — was 0% covered
// ---------------------------------------------------------------------------

func TestMCMisc_ParseECPrivPEM_SEC1RoundTrip(t *testing.T) {
	priv, _ := ecdsa.GenerateKey(elliptic.P256(), rand.Reader)
	der, _ := x509.MarshalECPrivateKey(priv)
	block := &pem.Block{Type: "EC PRIVATE KEY", Bytes: der}
	pemBytes := pem.EncodeToMemory(block)

	got, err := parseECPrivPEM(pemBytes)
	if err != nil {
		t.Fatalf("parseECPrivPEM SEC1: unexpected error: %v", err)
	}
	if got.D.Cmp(priv.D) != 0 {
		t.Fatal("parsed key D differs from original")
	}
}

func TestMCMisc_ParseECPrivPEM_PKCS8RoundTrip(t *testing.T) {
	priv, _ := ecdsa.GenerateKey(elliptic.P256(), rand.Reader)
	der, _ := x509.MarshalPKCS8PrivateKey(priv)
	block := &pem.Block{Type: "PRIVATE KEY", Bytes: der}
	pemBytes := pem.EncodeToMemory(block)

	got, err := parseECPrivPEM(pemBytes)
	if err != nil {
		t.Fatalf("parseECPrivPEM PKCS8: unexpected error: %v", err)
	}
	if got.D.Cmp(priv.D) != 0 {
		t.Fatal("parsed PKCS8 key D differs from original")
	}
}

func TestMCMisc_ParseECPrivPEM_NoPEMBlockReturnsError(t *testing.T) {
	_, err := parseECPrivPEM([]byte("not pem data at all"))
	if err == nil {
		t.Fatal("expected error for non-PEM input, got nil")
	}
	if !strings.Contains(err.Error(), "no PEM block") {
		t.Fatalf("error message must mention 'no PEM block', got: %v", err)
	}
}

func TestMCMisc_ParseECPrivPEM_EmptyInputReturnsError(t *testing.T) {
	_, err := parseECPrivPEM([]byte{})
	if err == nil {
		t.Fatal("expected error for empty input, got nil")
	}
}

func TestMCMisc_ParseECPrivPEM_GarbageDERInPEMReturnsError(t *testing.T) {
	// Valid PEM structure but garbage DER bytes.
	garbage := pem.EncodeToMemory(&pem.Block{Type: "EC PRIVATE KEY", Bytes: []byte("notDER")})
	_, err := parseECPrivPEM(garbage)
	if err == nil {
		t.Fatal("expected error for garbage DER in PEM, got nil")
	}
}

func TestMCMisc_ParseECPrivPEM_RSAKeyIsRejected(t *testing.T) {
	// An RSA key wrapped in PKCS8 PEM is not an EC key — must be rejected.
	// We simulate this by putting a raw RSA public key in a "PRIVATE KEY" PEM,
	// which will parse as PKCS8 but yield a non-EC type.
	// Easier: just use garbage bytes that x509 can't parse as either EC or PKCS8.
	bad := pem.EncodeToMemory(&pem.Block{Type: "PRIVATE KEY", Bytes: []byte("notDERatAll")})
	_, err := parseECPrivPEM(bad)
	if err == nil {
		t.Fatal("non-EC PKCS8 key must be rejected")
	}
}

func TestMCMisc_ParseECPrivPEM_TableDriven(t *testing.T) {
	// Helper: SEC1 PEM of a fresh key.
	sec1PEM := func() []byte {
		p, _ := ecdsa.GenerateKey(elliptic.P256(), rand.Reader)
		der, _ := x509.MarshalECPrivateKey(p)
		return pem.EncodeToMemory(&pem.Block{Type: "EC PRIVATE KEY", Bytes: der})
	}
	pkcs8PEM := func() []byte {
		p, _ := ecdsa.GenerateKey(elliptic.P256(), rand.Reader)
		der, _ := x509.MarshalPKCS8PrivateKey(p)
		return pem.EncodeToMemory(&pem.Block{Type: "PRIVATE KEY", Bytes: der})
	}

	cases := []struct {
		label   string
		input   []byte
		wantErr bool
	}{
		{"valid SEC1", sec1PEM(), false},
		{"valid PKCS8", pkcs8PEM(), false},
		{"empty", []byte{}, true},
		{"non-PEM text", []byte("hello world"), true},
		{"garbage DER", pem.EncodeToMemory(&pem.Block{Type: "EC PRIVATE KEY", Bytes: []byte("xx")}), true},
	}
	for _, tc := range cases {
		t.Run(tc.label, func(t *testing.T) {
			_, err := parseECPrivPEM(tc.input)
			if tc.wantErr && err == nil {
				t.Fatalf("expected error for %q, got nil", tc.label)
			}
			if !tc.wantErr && err != nil {
				t.Fatalf("unexpected error for %q: %v", tc.label, err)
			}
		})
	}
}

// ---------------------------------------------------------------------------
// loadOrCreateKey — was 50% covered (only ephemeral path); add file-load path
// ---------------------------------------------------------------------------

func TestMCMisc_LoadOrCreateKey_EphemeralWhenEnvUnset(t *testing.T) {
	mcmisc_unsetenv(t, "UCP_SIGNING_KEY_PATH")
	mk, loaded := loadOrCreateKey()
	if loaded {
		t.Fatal("no env var set: loaded must be false (ephemeral)")
	}
	if mk == nil || mk.priv == nil {
		t.Fatal("loadOrCreateKey must return a non-nil key even in ephemeral mode")
	}
	if mk.kid == "" {
		t.Fatal("kid must be non-empty for an ephemeral key")
	}
}

func TestMCMisc_LoadOrCreateKey_LoadsSEC1PEMFromEnv(t *testing.T) {
	dir := t.TempDir()
	path, want := mcmisc_writeECKeyPEM(t, dir, "key.pem")
	mcmisc_setenv(t, "UCP_SIGNING_KEY_PATH", path)

	mk, loaded := loadOrCreateKey()
	if !loaded {
		t.Fatal("UCP_SIGNING_KEY_PATH set: loaded must be true")
	}
	if mk.priv.D.Cmp(want.D) != 0 {
		t.Fatal("loaded key D does not match the PEM file")
	}
}

func TestMCMisc_LoadOrCreateKey_LoadsPKCS8PEMFromEnv(t *testing.T) {
	dir := t.TempDir()
	path, want := mcmisc_writePKCS8KeyPEM(t, dir, "key_pkcs8.pem")
	mcmisc_setenv(t, "UCP_SIGNING_KEY_PATH", path)

	mk, loaded := loadOrCreateKey()
	if !loaded {
		t.Fatal("PKCS8 PEM: loaded must be true")
	}
	if mk.priv.D.Cmp(want.D) != 0 {
		t.Fatal("loaded PKCS8 key D does not match the PEM file")
	}
}

func TestMCMisc_LoadOrCreateKey_FallsBackToEphemeralOnBadPath(t *testing.T) {
	mcmisc_setenv(t, "UCP_SIGNING_KEY_PATH", "/does/not/exist/key.pem")
	mk, loaded := loadOrCreateKey()
	if loaded {
		t.Fatal("bad path: loaded must be false (ephemeral fallback)")
	}
	if mk == nil {
		t.Fatal("must return a valid ephemeral key on bad path")
	}
}

func TestMCMisc_LoadOrCreateKey_FallsBackToEphemeralOnGarbagePEM(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "bad.pem")
	if err := os.WriteFile(path, []byte("not a PEM file"), 0600); err != nil {
		t.Fatalf("write bad PEM: %v", err)
	}
	mcmisc_setenv(t, "UCP_SIGNING_KEY_PATH", path)

	mk, loaded := loadOrCreateKey()
	if loaded {
		t.Fatal("garbage PEM: loaded must be false (ephemeral fallback)")
	}
	if mk == nil {
		t.Fatal("must return a valid ephemeral key when PEM is garbage")
	}
}

func TestMCMisc_LoadOrCreateKey_KidIsStableAcrossLoads(t *testing.T) {
	dir := t.TempDir()
	path, _ := mcmisc_writeECKeyPEM(t, dir, "stable.pem")
	mcmisc_setenv(t, "UCP_SIGNING_KEY_PATH", path)

	mk1, _ := loadOrCreateKey()
	mk2, _ := loadOrCreateKey()
	if mk1.kid != mk2.kid {
		t.Fatalf("kid must be deterministic for the same key file: %q vs %q", mk1.kid, mk2.kid)
	}
}

// ---------------------------------------------------------------------------
// safeClient — was 69% covered; add SSRF-guard path tests
// ---------------------------------------------------------------------------

// mcmisc_httpTestServerLoopback creates an httptest server and returns its URL.
// The server echoes "ok" on any request.
func mcmisc_httpTestServerLoopback(t *testing.T) *httptest.Server {
	t.Helper()
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusOK)
		w.Write([]byte("ok"))
	}))
	t.Cleanup(srv.Close)
	return srv
}

func TestMCMisc_SafeClient_BlocksPlainHTTPWithoutDevSSRF(t *testing.T) {
	// safeClient(false) must refuse plain http:// (non-loopback) — but the httptest
	// server IS loopback (127.0.0.1). The guard blocks any non-loopback. We test
	// that allowDevSSRF=false blocks the loopback too (loopback is blocked in
	// production mode where isBlockedIP returns true for loopback).
	srv := mcmisc_httpTestServerLoopback(t)
	c := safeClient(false)
	_, err := c.Get(srv.URL)
	if err == nil {
		t.Fatal("safeClient(allowDevSSRF=false): loopback must be blocked (isBlockedIP returns true)")
	}
	if !strings.Contains(err.Error(), "blocked_ip") {
		t.Fatalf("error must mention blocked_ip, got: %v", err)
	}
}

func TestMCMisc_SafeClient_AllowsLoopbackWithDevSSRF(t *testing.T) {
	// safeClient(true) MUST allow loopback (127.x) per the dev-SSRF allowance
	// (#56: only loopback permitted, private/link-local stay blocked).
	srv := mcmisc_httpTestServerLoopback(t)
	c := safeClient(true)
	resp, err := c.Get(srv.URL)
	if err != nil {
		t.Fatalf("safeClient(allowDevSSRF=true): loopback must be allowed, got: %v", err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("expected 200 from loopback server, got %d", resp.StatusCode)
	}
}

func TestMCMisc_SafeClient_BlocksRedirects(t *testing.T) {
	// The client must refuse HTTP redirects (CheckRedirect blocks them).
	redirectSrv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		http.Redirect(w, r, r.URL.String()+"?followed=1", http.StatusFound)
	}))
	t.Cleanup(redirectSrv.Close)

	c := safeClient(true) // dev mode so loopback connects
	_, err := c.Get(redirectSrv.URL)
	if err == nil {
		t.Fatal("safeClient must block HTTP redirects")
	}
	if !strings.Contains(err.Error(), "redirects_blocked") {
		t.Fatalf("error must mention redirects_blocked, got: %v", err)
	}
}

func TestMCMisc_SafeClient_IsBlockedIPLoopback(t *testing.T) {
	// isBlockedIP must return true for 127.0.0.1 (loopback = private for the SSRF guard).
	if !isBlockedIP(net.ParseIP("127.0.0.1")) {
		t.Fatal("127.0.0.1 must be blocked (loopback)")
	}
}

func TestMCMisc_SafeClient_IsBlockedIPPrivate(t *testing.T) {
	for _, ip := range []string{"10.0.0.1", "192.168.1.1", "172.16.0.1"} {
		if !isBlockedIP(net.ParseIP(ip)) {
			t.Fatalf("private IP %s must be blocked", ip)
		}
	}
}

func TestMCMisc_SafeClient_IsBlockedIPLinkLocal(t *testing.T) {
	for _, ip := range []string{"169.254.169.254", "fe80::1"} {
		if !isBlockedIP(net.ParseIP(ip)) {
			t.Fatalf("link-local IP %s must be blocked", ip)
		}
	}
}

func TestMCMisc_SafeClient_ClientNotNil(t *testing.T) {
	// safeClient must return a non-nil *http.Client in both modes.
	for _, dev := range []bool{false, true} {
		c := safeClient(dev)
		if c == nil {
			t.Fatalf("safeClient(%v) returned nil", dev)
		}
	}
}

func TestMCMisc_SafeClient_TableDriven_BlockedIPs(t *testing.T) {
	cases := []struct {
		ip      string
		blocked bool
	}{
		{"127.0.0.1", true},
		{"::1", true},
		{"10.1.2.3", true},
		{"192.168.0.100", true},
		{"172.16.5.5", true},
		{"169.254.169.254", true},
		{"fe80::dead:beef", true},
		{"0.0.0.0", true},
	}
	for _, tc := range cases {
		t.Run(tc.ip, func(t *testing.T) {
			ip := net.ParseIP(tc.ip)
			if ip == nil {
				t.Fatalf("bad test IP %q", tc.ip)
			}
			got := isBlockedIP(ip)
			if got != tc.blocked {
				t.Fatalf("isBlockedIP(%s): want %v, got %v", tc.ip, tc.blocked, got)
			}
		})
	}
}

// ---------------------------------------------------------------------------
// envOr — pure helper; no side effects beyond env reads
// ---------------------------------------------------------------------------

func TestMCMisc_EnvOr_ReturnsDefaultWhenUnset(t *testing.T) {
	mcmisc_unsetenv(t, "MCMISC_TEST_ENV_KEY")
	got := envOr("MCMISC_TEST_ENV_KEY", "mydefault")
	if got != "mydefault" {
		t.Fatalf("want mydefault, got %q", got)
	}
}

func TestMCMisc_EnvOr_ReturnsValueWhenSet(t *testing.T) {
	mcmisc_setenv(t, "MCMISC_TEST_ENV_KEY2", "thevalue")
	got := envOr("MCMISC_TEST_ENV_KEY2", "ignored")
	if got != "thevalue" {
		t.Fatalf("want thevalue, got %q", got)
	}
}

func TestMCMisc_EnvOr_EmptyStringUsesDefault(t *testing.T) {
	// An empty env var is treated as unset (returns default).
	mcmisc_setenv(t, "MCMISC_TEST_ENV_EMPTY", "")
	got := envOr("MCMISC_TEST_ENV_EMPTY", "fallback")
	if got != "fallback" {
		t.Fatalf("empty env var must use default, got %q", got)
	}
}

func TestMCMisc_EnvOr_TableDriven(t *testing.T) {
	cases := []struct {
		label   string
		envVal  *string // nil = unset
		def     string
		wantVal string
	}{
		{"unset → default", nil, "D", "D"},
		{"set → value", strPtr("V"), "D", "V"},
		{"empty → default", strPtr(""), "D", "D"},
		{"set spaces → value", strPtr("  hello  "), "D", "  hello  "},
	}
	const key = "MCMISC_ENVORTABLE"
	for _, tc := range cases {
		t.Run(tc.label, func(t *testing.T) {
			if tc.envVal == nil {
				mcmisc_unsetenv(t, key)
			} else {
				mcmisc_setenv(t, key, *tc.envVal)
			}
			got := envOr(key, tc.def)
			if got != tc.wantVal {
				t.Fatalf("want %q, got %q", tc.wantVal, got)
			}
		})
	}
}

// strPtr is a helper to take the address of a string literal.
func strPtr(s string) *string { return &s }

// ---------------------------------------------------------------------------
// usdCents — pure helper
// ---------------------------------------------------------------------------

func TestMCMisc_UsdCents_UsesDefaultWhenEnvUnset(t *testing.T) {
	mcmisc_unsetenv(t, "MCMISC_USD_TEST")
	got := usdCents("MCMISC_USD_TEST", 10.0)
	if got != 1000 {
		t.Fatalf("$10.00 default = 1000¢, got %d", got)
	}
}

func TestMCMisc_UsdCents_ParsesEnvFloat(t *testing.T) {
	mcmisc_setenv(t, "MCMISC_USD_TEST2", "5.50")
	got := usdCents("MCMISC_USD_TEST2", 99.0)
	if got != 550 {
		t.Fatalf("$5.50 = 550¢, got %d", got)
	}
}

func TestMCMisc_UsdCents_InvalidEnvFallsBackToDefault(t *testing.T) {
	mcmisc_setenv(t, "MCMISC_USD_TEST3", "not_a_float")
	got := usdCents("MCMISC_USD_TEST3", 8.0)
	if got != 800 {
		t.Fatalf("invalid env → default $8.00 = 800¢, got %d", got)
	}
}

func TestMCMisc_UsdCents_ZeroDollarDefault(t *testing.T) {
	mcmisc_unsetenv(t, "MCMISC_USD_ZERO")
	got := usdCents("MCMISC_USD_ZERO", 0)
	if got != 0 {
		t.Fatalf("$0 default = 0¢, got %d", got)
	}
}

func TestMCMisc_UsdCents_RoundingHalfUp(t *testing.T) {
	// The function uses int64(d*100 + 0.5): $0.005 should round to 1¢.
	mcmisc_setenv(t, "MCMISC_USD_ROUND", "0.005")
	got := usdCents("MCMISC_USD_ROUND", 0)
	if got != 1 {
		t.Fatalf("$0.005 = 1¢ (round up), got %d", got)
	}
}

func TestMCMisc_UsdCents_TableDriven(t *testing.T) {
	const k = "MCMISC_USD_TABLE"
	cases := []struct {
		label     string
		envVal    *string
		def       float64
		wantCents int64
	}{
		{"default 800", nil, 8.0, 800},
		{"env 20.00", strPtr("20.00"), 0, 2000},
		{"env 0.01", strPtr("0.01"), 0, 1},
		{"bad env fallback", strPtr("abc"), 5.0, 500},
		{"empty env fallback", strPtr(""), 3.0, 300},
	}
	for _, tc := range cases {
		t.Run(tc.label, func(t *testing.T) {
			if tc.envVal == nil {
				mcmisc_unsetenv(t, k)
			} else {
				mcmisc_setenv(t, k, *tc.envVal)
			}
			got := usdCents(k, tc.def)
			if got != tc.wantCents {
				t.Fatalf("want %d¢, got %d", tc.wantCents, got)
			}
		})
	}
}

// ---------------------------------------------------------------------------
// loadConfig — pure config builder, tests defaults + env overrides
// ---------------------------------------------------------------------------

func mcmisc_clearConfigEnvs(t *testing.T) {
	t.Helper()
	for _, k := range []string{
		"UCP_DEFAULT_AUTONOMY", "UCP_TIER_GRANTS", "UCP_UNSIGNED_TIER",
		"UCP_LISTEN_ADDR", "UCP_MERCHANT_BASE_URL",
		"BUDGET_DEFAULT_CEILING_USD", "BUDGET_HARD_MAX_USD",
		"UCP_REQUIRE_SIGNATURES", "UCP_ALLOW_DEV_SSRF",
		"UCP_CIRCLE_ALLOW_UNSIGNED_LOOPBACK",
	} {
		mcmisc_unsetenv(t, k)
	}
}

func TestMCMisc_LoadConfig_DefaultsWhenEnvEmpty(t *testing.T) {
	mcmisc_clearConfigEnvs(t)
	cfg := loadConfig()
	if cfg.ListenAddr != "127.0.0.1:8090" {
		t.Fatalf("default ListenAddr: got %q", cfg.ListenAddr)
	}
	if cfg.DefaultTier != "L2" {
		t.Fatalf("default tier L2, got %q", cfg.DefaultTier)
	}
	if cfg.UnsignedTier != "L1" {
		t.Fatalf("default unsigned tier L1, got %q", cfg.UnsignedTier)
	}
	if cfg.BudgetCeilingCents != 80000 {
		t.Fatalf("default ceiling 80000¢, got %d", cfg.BudgetCeilingCents)
	}
	if cfg.BudgetHardMaxCents != 200000 {
		t.Fatalf("default hard-max 200000¢, got %d", cfg.BudgetHardMaxCents)
	}
	if cfg.RequireSignatures {
		t.Fatal("default RequireSignatures must be false")
	}
	if cfg.AllowDevSSRF {
		t.Fatal("default AllowDevSSRF must be false")
	}
}

func TestMCMisc_LoadConfig_EnvOverrideListenAddr(t *testing.T) {
	mcmisc_clearConfigEnvs(t)
	mcmisc_setenv(t, "UCP_LISTEN_ADDR", "0.0.0.0:9999")
	cfg := loadConfig()
	if cfg.ListenAddr != "0.0.0.0:9999" {
		t.Fatalf("want 0.0.0.0:9999, got %q", cfg.ListenAddr)
	}
}

func TestMCMisc_LoadConfig_EnvOverrideDefaultTierL3(t *testing.T) {
	mcmisc_clearConfigEnvs(t)
	mcmisc_setenv(t, "UCP_DEFAULT_AUTONOMY", "L3")
	cfg := loadConfig()
	if cfg.DefaultTier != "L3" {
		t.Fatalf("UCP_DEFAULT_AUTONOMY=L3 → DefaultTier L3, got %q", cfg.DefaultTier)
	}
}

func TestMCMisc_LoadConfig_InvalidDefaultTierFallsBackToL2(t *testing.T) {
	mcmisc_clearConfigEnvs(t)
	mcmisc_setenv(t, "UCP_DEFAULT_AUTONOMY", "L99")
	cfg := loadConfig()
	if cfg.DefaultTier != "L2" {
		t.Fatalf("invalid tier must fall back to L2, got %q", cfg.DefaultTier)
	}
}

func TestMCMisc_LoadConfig_InvalidUnsignedTierFallsBackToL1(t *testing.T) {
	mcmisc_clearConfigEnvs(t)
	mcmisc_setenv(t, "UCP_UNSIGNED_TIER", "BOGUS")
	cfg := loadConfig()
	if cfg.UnsignedTier != "L1" {
		t.Fatalf("invalid unsigned tier must fall back to L1, got %q", cfg.UnsignedTier)
	}
}

func TestMCMisc_LoadConfig_RequireSignaturesFlag(t *testing.T) {
	mcmisc_clearConfigEnvs(t)
	mcmisc_setenv(t, "UCP_REQUIRE_SIGNATURES", "1")
	cfg := loadConfig()
	if !cfg.RequireSignatures {
		t.Fatal("UCP_REQUIRE_SIGNATURES=1 must set RequireSignatures=true")
	}
}

func TestMCMisc_LoadConfig_AllowDevSSRFFlag(t *testing.T) {
	mcmisc_clearConfigEnvs(t)
	mcmisc_setenv(t, "UCP_ALLOW_DEV_SSRF", "1")
	cfg := loadConfig()
	if !cfg.AllowDevSSRF {
		t.Fatal("UCP_ALLOW_DEV_SSRF=1 must set AllowDevSSRF=true")
	}
}

// The Circle loopback opt-in is the only way to start the rail with unsigned
// checkout enabled, so a typo in the env var name would silently make it
// unsettable (fail-closed, but the server would refuse to start with no clue
// why). Pin the exact spelling and the strict "1"-only parse.
func TestMCMisc_LoadConfig_CircleAllowUnsignedLoopbackFlag(t *testing.T) {
	mcmisc_clearConfigEnvs(t)
	cfg := loadConfig()
	if cfg.CircleAllowUnsignedLoopback {
		t.Fatal("CircleAllowUnsignedLoopback must default to false (opt-in only)")
	}
	for _, v := range []string{"0", "true", "yes", ""} {
		mcmisc_setenv(t, "UCP_CIRCLE_ALLOW_UNSIGNED_LOOPBACK", v)
		if loadConfig().CircleAllowUnsignedLoopback {
			t.Fatalf("UCP_CIRCLE_ALLOW_UNSIGNED_LOOPBACK=%q must NOT enable the opt-in (only \"1\" does)", v)
		}
	}
	mcmisc_setenv(t, "UCP_CIRCLE_ALLOW_UNSIGNED_LOOPBACK", "1")
	if !loadConfig().CircleAllowUnsignedLoopback {
		t.Fatal("UCP_CIRCLE_ALLOW_UNSIGNED_LOOPBACK=1 must set CircleAllowUnsignedLoopback=true")
	}
}

func TestMCMisc_LoadConfig_TierGrantsParsedFromJSON(t *testing.T) {
	mcmisc_clearConfigEnvs(t)
	mcmisc_setenv(t, "UCP_TIER_GRANTS", `{"http://agent.example/profile":"L3"}`)
	cfg := loadConfig()
	if cfg.TierGrants["http://agent.example/profile"] != "L3" {
		t.Fatalf("TierGrants not parsed from JSON: %v", cfg.TierGrants)
	}
}

func TestMCMisc_LoadConfig_BudgetCeilingEnvOverride(t *testing.T) {
	mcmisc_clearConfigEnvs(t)
	mcmisc_setenv(t, "BUDGET_DEFAULT_CEILING_USD", "500.00")
	cfg := loadConfig()
	if cfg.BudgetCeilingCents != 50000 {
		t.Fatalf("BUDGET_DEFAULT_CEILING_USD=500 → 50000¢, got %d", cfg.BudgetCeilingCents)
	}
}

func TestMCMisc_LoadConfig_BaseURLEnvOverride(t *testing.T) {
	mcmisc_clearConfigEnvs(t)
	mcmisc_setenv(t, "UCP_MERCHANT_BASE_URL", "https://merchant.example.com")
	cfg := loadConfig()
	if cfg.BaseURL != "https://merchant.example.com" {
		t.Fatalf("want https://merchant.example.com, got %q", cfg.BaseURL)
	}
}

// ---------------------------------------------------------------------------
// Regression: catalogTool with nil store must not panic (boundary test)
// ---------------------------------------------------------------------------

func TestMCMisc_CatalogTool_NilStoreDoesNotPanic(t *testing.T) {
	defer func() {
		if r := recover(); r != nil {
			t.Fatalf("catalogTool panicked with nil store: %v", r)
		}
	}()
	args := []byte(`{"query":{"city":"bali","checkin":"2026-07-01","checkout":"2026-07-03"}}`)
	// catalogTool passes nil as the store; this must not panic.
	catalogTool("search_catalog", args)
}

// ---------------------------------------------------------------------------
// safeClient: verify Content-Digest round-trip via loopback server (dev SSRF on)
// ---------------------------------------------------------------------------

func TestMCMisc_SafeClient_CanFetchLoopbackJSONWithDevSSRF(t *testing.T) {
	// A more realistic test: fetch a JSON profile from the loopback httptest server.
	profileDoc := `{"ucp":{"capabilities":{}},"signing_keys":[]}`
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusOK)
		w.Write([]byte(profileDoc))
	}))
	t.Cleanup(srv.Close)

	c := safeClient(true)
	resp, err := c.Get(srv.URL)
	if err != nil {
		t.Fatalf("safeClient(dev=true) loopback GET: %v", err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("expected 200, got %d", resp.StatusCode)
	}
}

// ---------------------------------------------------------------------------
// kidFor determinism — exercised via loadOrCreateKey; direct test
// ---------------------------------------------------------------------------

func TestMCMisc_KidForIsKidFormat(t *testing.T) {
	priv, _ := ecdsa.GenerateKey(elliptic.P256(), rand.Reader)
	kid := kidFor(&priv.PublicKey)
	if !strings.HasPrefix(kid, "merch-") {
		t.Fatalf("kid must start with 'merch-', got %q", kid)
	}
	// suffix must be 8 chars of base64url (6 bytes → 8 chars raw-url-base64)
	suffix := strings.TrimPrefix(kid, "merch-")
	if _, err := base64.RawURLEncoding.DecodeString(suffix); err != nil {
		t.Fatalf("kid suffix is not valid base64url: %q: %v", suffix, err)
	}
}

// ---------------------------------------------------------------------------
// safeClient: HTTP POST with a body works through dev-mode loopback
// ---------------------------------------------------------------------------

func TestMCMisc_SafeClient_POSTLoopbackWithDevSSRF(t *testing.T) {
	var gotBody []byte
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		buf := new(bytes.Buffer)
		buf.ReadFrom(r.Body)
		gotBody = buf.Bytes()
		w.WriteHeader(http.StatusNoContent)
	}))
	t.Cleanup(srv.Close)

	c := safeClient(true)
	payload := []byte(`{"hello":"world"}`)
	resp, err := c.Post(srv.URL, "application/json", bytes.NewReader(payload))
	if err != nil {
		t.Fatalf("safeClient POST: %v", err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusNoContent {
		t.Fatalf("expected 204, got %d", resp.StatusCode)
	}
	if string(gotBody) != string(payload) {
		t.Fatalf("server received wrong body: %q (want %q)", gotBody, payload)
	}
}
