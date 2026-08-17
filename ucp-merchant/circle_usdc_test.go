package main

import (
	"crypto/rand"
	"crypto/rsa"
	"crypto/sha256"
	"crypto/x509"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"encoding/pem"
	"fmt"
	"net/http"
	"net/http/httptest"
	"regexp"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
)

// testBudgetHardMaxCents mirrors the default BUDGET_HARD_MAX_USD ($2000) used
// elsewhere in this repo — large enough not to interfere with the small test
// amounts below, so tests below the cap and above it are both exercisable.
const testBudgetHardMaxCents = 200000

// testEntitySecretHex is a fixed 32-byte (64 hex char) string standing in for
// a real Circle entity secret — never a real credential, purely a fixture.
const testEntitySecretHex = "cc99c27aa8929a4979fb4bd03c8cb774b9305b4364a368418338d52a69d351e2"

func mustTestRSAKey(t *testing.T) *rsa.PrivateKey {
	t.Helper()
	key, err := rsa.GenerateKey(rand.Reader, 2048)
	if err != nil {
		t.Fatalf("generating test RSA key: %v", err)
	}
	return key
}

func pemEncodePKIX(t *testing.T, pub *rsa.PublicKey) string {
	t.Helper()
	der, err := x509.MarshalPKIXPublicKey(pub)
	if err != nil {
		t.Fatalf("marshal PKIX: %v", err)
	}
	return string(pem.EncodeToMemory(&pem.Block{Type: "PUBLIC KEY", Bytes: der}))
}

func init() {
	if len(testEntitySecretHex) != circleEntitySecretBytes*2 {
		panic(fmt.Sprintf("testEntitySecretHex is %d hex chars, want %d (32 bytes)",
			len(testEntitySecretHex), circleEntitySecretBytes*2))
	}
}

// entityCiphertext round-trips through RSA-OAEP/SHA-256 against a locally
// generated keypair — this is independently verifiable without any live
// Circle credentials (see circle_usdc.go's module-header honesty note on
// what is and isn't verified by this test suite).
func TestEntityCiphertextRoundTrips(t *testing.T) {
	key := mustTestRSAKey(t)
	ct, err := entityCiphertext(testEntitySecretHex, &key.PublicKey)
	if err != nil {
		t.Fatalf("entityCiphertext: %v", err)
	}
	raw, err := base64.StdEncoding.DecodeString(ct)
	if err != nil {
		t.Fatalf("ciphertext not valid base64: %v", err)
	}
	plain, err := rsa.DecryptOAEP(sha256.New(), rand.Reader, key, raw, nil)
	if err != nil {
		t.Fatalf("decrypting ciphertext: %v", err)
	}
	want, _ := hex.DecodeString(testEntitySecretHex)
	if string(plain) != string(want) {
		t.Fatalf("round-tripped secret mismatch: got %x want %x", plain, want)
	}

	// Fresh ciphertext per call — never reused (OAEP is randomized).
	ct2, err := entityCiphertext(testEntitySecretHex, &key.PublicKey)
	if err != nil {
		t.Fatalf("entityCiphertext (2nd call): %v", err)
	}
	if ct2 == ct {
		t.Fatal("ciphertext reused across calls — expected fresh randomized OAEP output each time")
	}
}

func TestEntityCiphertextRejectsBadHex(t *testing.T) {
	key := mustTestRSAKey(t)
	if _, err := entityCiphertext("not-hex-zz", &key.PublicKey); err == nil {
		t.Fatal("expected error for non-hex entity secret, got nil")
	}
}

func TestEntityCiphertextRejectsWrongLength(t *testing.T) {
	key := mustTestRSAKey(t)
	// 16 bytes, not 32 — even length (32 hex chars), valid hex, wrong length.
	// (An odd-length string fails earlier in hex.DecodeString with a
	// different error, which would defeat the point of this test.)
	const sixteenBytes = "00112233445566778899aabbccddeeff"
	if len(sixteenBytes)%2 != 0 {
		t.Fatalf("test fixture itself is odd-length (%d chars) — fix the literal", len(sixteenBytes))
	}
	if _, err := entityCiphertext(sixteenBytes, &key.PublicKey); err == nil {
		t.Fatal("expected error for wrong-length entity secret, got nil")
	}
}

func TestParseRSAPublicKeyPEM(t *testing.T) {
	key := mustTestRSAKey(t)
	pemStr := pemEncodePKIX(t, &key.PublicKey)
	pub, err := parseRSAPublicKeyPEM(pemStr)
	if err != nil {
		t.Fatalf("parseRSAPublicKeyPEM (PKIX): %v", err)
	}
	if pub.N.Cmp(key.PublicKey.N) != 0 {
		t.Fatal("parsed modulus does not match original key")
	}

	if _, err := parseRSAPublicKeyPEM("not a pem block"); err == nil {
		t.Fatal("expected error for garbage input, got nil")
	}
}

// Fail-honest: with no Circle credentials configured, circleSettle must not
// make any HTTP call and must not fabricate a transaction id.
func TestCircleSettleFailsHonestlyWhenNotConfigured(t *testing.T) {
	origClient, origBase := circleHTTPClient, circleBaseURL
	defer func() { circleHTTPClient, circleBaseURL = origClient, origBase }()

	var called int32
	circleHTTPClient = &http.Client{Transport: roundTripFunc(func(*http.Request) (*http.Response, error) {
		atomic.AddInt32(&called, 1)
		t.Error("HTTP call made despite unconfigured credentials")
		return nil, fmt.Errorf("unexpected HTTP call in test")
	})}

	st := newStore()
	cfg := circleConfig{} // all fields empty
	rec, attempted, err := st.circleSettle(cfg, "BR-1", 5000)
	if attempted {
		t.Fatal("expected attempted=false when Circle is not configured")
	}
	if err != nil {
		t.Fatalf("expected nil error for the not-configured short-circuit, got %v", err)
	}
	if rec != (circleSettlement{}) {
		t.Fatalf("expected zero-value settlement, got %+v", rec)
	}
	if atomic.LoadInt32(&called) != 0 {
		t.Fatal("HTTP client was invoked despite missing credentials")
	}
}

// mockCircleServer wires up a full mock of the three Circle endpoints this
// module calls, returning the mux plus counters/hooks tests can inspect.
// Handler assertions use t.Errorf (not t.Fatalf) + early return because these
// run on the httptest.Server's own per-request goroutines, where FailNow is
// unsafe per the testing package's own documentation.
type mockCircleServer struct {
	pubPEM         string
	transferCalls  int32
	transferAmount string // last-seen amounts[0], for assertions
}

func newMockCircleServer(t *testing.T, key *rsa.PrivateKey) (*mockCircleServer, *httptest.Server) {
	t.Helper()
	m := &mockCircleServer{pubPEM: pemEncodePKIX(t, &key.PublicKey)}
	mux := http.NewServeMux()
	mux.HandleFunc("/config/entity/publicKey", func(w http.ResponseWriter, r *http.Request) {
		if got := r.Header.Get("Authorization"); got != "Bearer test-api-key" {
			t.Errorf("unexpected Authorization header: %q", got)
			w.WriteHeader(http.StatusUnauthorized)
			return
		}
		writeJSON(w, http.StatusOK, map[string]any{
			"data": map[string]string{"publicKey": m.pubPEM},
		})
	})
	mux.HandleFunc("/wallets/dst-wallet", func(w http.ResponseWriter, r *http.Request) {
		writeJSON(w, http.StatusOK, map[string]any{
			"data": map[string]any{
				"wallet": map[string]string{"id": "dst-wallet", "address": "0xdstAddr000000000000000000000000000000"},
			},
		})
	})
	mux.HandleFunc("/wallets/src-wallet/balances", func(w http.ResponseWriter, r *http.Request) {
		writeJSON(w, http.StatusOK, map[string]any{
			"data": map[string]any{
				"tokenBalances": []map[string]any{
					{"token": map[string]string{"id": "mock-usdc-token-id", "symbol": "USDC"}, "amount": "20"},
				},
			},
		})
	})
	mux.HandleFunc("/developer/transactions/transfer", func(w http.ResponseWriter, r *http.Request) {
		atomic.AddInt32(&m.transferCalls, 1)
		var body circleTransferRequest
		if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
			t.Errorf("decoding transfer request: %v", err)
			w.WriteHeader(http.StatusBadRequest)
			return
		}
		if body.WalletID != "src-wallet" || body.DestinationAddress != "0xdstAddr000000000000000000000000000000" {
			t.Errorf("unexpected wallet fields: %+v", body)
		}
		if body.TokenID != "mock-usdc-token-id" {
			t.Errorf("unexpected tokenId: %q", body.TokenID)
		}
		if !idemKeyPattern.MatchString(body.IdempotencyKey) {
			t.Errorf("idempotency key not UUID-shaped: %q", body.IdempotencyKey)
		}
		if body.EntitySecretCipher == "" {
			t.Error("missing entitySecretCiphertext in request")
		}
		if len(body.Amounts) > 0 {
			m.transferAmount = body.Amounts[0]
		}
		writeJSON(w, http.StatusOK, map[string]any{
			// Real transaction ids observed from live Circle calls are UUIDs;
			// txn-live-<n> here is just a distinguishable mock value.
			"data": map[string]string{"id": fmt.Sprintf("txn-live-%d", m.transferCalls), "state": "PENDING"},
		})
	})
	return m, httptest.NewServer(mux)
}

// Full happy path against a mocked Circle API (publicKey + transfer endpoints).
// No live Circle credentials required — see module header for what this proves
// vs. what has been separately verified live (see module header).
func TestCircleSettleHappyPathAgainstMockServer(t *testing.T) {
	key := mustTestRSAKey(t)
	mock, srv := newMockCircleServer(t, key)
	defer srv.Close()

	origClient, origBase := circleHTTPClient, circleBaseURL
	circleHTTPClient = srv.Client()
	circleBaseURL = srv.URL
	defer func() { circleHTTPClient, circleBaseURL = origClient, origBase }()

	st := newStore()
	cfg := circleConfig{
		APIKey:           "test-api-key",
		EntitySecretHex:  testEntitySecretHex,
		SourceWalletID:   "src-wallet",
		MerchantWalletID: "dst-wallet",
	}

	rec, attempted, err := st.circleSettle(cfg, "BR-42", 5000)
	if !attempted {
		t.Fatal("expected attempted=true when configured")
	}
	if err != nil {
		t.Fatalf("circleSettle: %v", err)
	}
	if rec.Status != "PENDING" || rec.TransactionID == "" {
		t.Fatalf("unexpected settlement record: %+v", rec)
	}
	if mock.transferAmount != "50.00" {
		t.Fatalf("unexpected amount sent to Circle: %q (want 50.00 for 5000 cents)", mock.transferAmount)
	}

	// Idempotent replay: a second call for the same booking_ref must NOT hit
	// the network again — same discipline as alipaySettle, applied to a real rail.
	rec2, attempted2, err2 := st.circleSettle(cfg, "BR-42", 5000)
	if err2 != nil || !attempted2 {
		t.Fatalf("replay: attempted=%v err=%v", attempted2, err2)
	}
	if rec2 != rec {
		t.Fatalf("replay returned a different record: %+v != %+v", rec2, rec)
	}
	if atomic.LoadInt32(&mock.transferCalls) != 1 {
		t.Fatalf("expected exactly 1 transfer call (idempotent replay), got %d", mock.transferCalls)
	}

	recs, total := st.circleSummary()
	if len(recs) != 1 || total != 5000 {
		t.Fatalf("summary wrong: count=%d total=%d", len(recs), total)
	}
}

// A booking_ref that was already settled must NOT silently "succeed" if
// re-submitted with a different total_cents — that would report success for
// a request never actually executed.
func TestCircleSettleRejectsAmountMismatchOnReplay(t *testing.T) {
	key := mustTestRSAKey(t)
	_, srv := newMockCircleServer(t, key)
	defer srv.Close()

	origClient, origBase := circleHTTPClient, circleBaseURL
	circleHTTPClient = srv.Client()
	circleBaseURL = srv.URL
	defer func() { circleHTTPClient, circleBaseURL = origClient, origBase }()

	st := newStore()
	cfg := circleConfig{
		APIKey: "test-api-key", EntitySecretHex: testEntitySecretHex,
		SourceWalletID: "src-wallet", MerchantWalletID: "dst-wallet",
	}
	if _, attempted, err := st.circleSettle(cfg, "BR-mismatch", 5000); !attempted || err != nil {
		t.Fatalf("initial settle: attempted=%v err=%v", attempted, err)
	}
	_, attempted, err := st.circleSettle(cfg, "BR-mismatch", 9999)
	if !attempted {
		t.Fatal("expected attempted=true (this is a config error, not an unconfigured short-circuit)")
	}
	if err != errCircleAmountMismatch {
		t.Fatalf("expected errCircleAmountMismatch, got %v", err)
	}
}

// Concurrent settlement attempts for the SAME booking_ref must result in
// exactly one live transfer call — the store mutex alone does not guarantee
// this since it's released before the network round trip (see circleSettle's
// in-flight-marker comment).
func TestCircleSettleConcurrentSameBookingRefSingleTransfer(t *testing.T) {
	key := mustTestRSAKey(t)
	mock, srv := newMockCircleServer(t, key)
	defer srv.Close()

	origClient, origBase := circleHTTPClient, circleBaseURL
	circleHTTPClient = srv.Client()
	circleBaseURL = srv.URL
	defer func() { circleHTTPClient, circleBaseURL = origClient, origBase }()

	st := newStore()
	cfg := circleConfig{
		APIKey: "test-api-key", EntitySecretHex: testEntitySecretHex,
		SourceWalletID: "src-wallet", MerchantWalletID: "dst-wallet",
	}

	const n = 8
	var wg sync.WaitGroup
	results := make([]circleSettlement, n)
	errs := make([]error, n)
	for i := 0; i < n; i++ {
		wg.Add(1)
		go func(i int) {
			defer wg.Done()
			rec, _, err := st.circleSettle(cfg, "BR-concurrent", 5000)
			results[i], errs[i] = rec, err
		}(i)
	}
	wg.Wait()

	for i, err := range errs {
		if err != nil {
			t.Fatalf("goroutine %d: unexpected error: %v", i, err)
		}
	}
	first := results[0]
	for i, rec := range results {
		if rec != first {
			t.Fatalf("goroutine %d got a different settlement record: %+v != %+v", i, rec, first)
		}
	}
	if atomic.LoadInt32(&mock.transferCalls) != 1 {
		t.Fatalf("expected exactly 1 transfer call across %d concurrent settles, got %d", n, mock.transferCalls)
	}
}

// This is the test that proves the rail is genuinely agent-driven, not a human
// hitting an admin endpoint: a simulated agent runs a normal
// create_checkout -> update_checkout(consent) -> complete_checkout flow
// through dispatchTool ITSELF — the exact function /api/ucp/mcp calls in
// production (mcp.go) — and a real (mocked) Circle transfer fires as a DIRECT
// CONSEQUENCE of the booking completing, carrying the booking's own real
// total_cents. No admin endpoint, no hand-typed booking_ref, no manual
// trigger, and no hand-rolled substitute for the production wiring.
func TestCompleteCheckoutWithCircleSettlementRailEndToEnd(t *testing.T) {
	key := mustTestRSAKey(t)
	mock, srv := newMockCircleServer(t, key)
	defer srv.Close()

	origClient, origBase := circleHTTPClient, circleBaseURL
	circleHTTPClient = srv.Client()
	circleBaseURL = srv.URL
	defer func() { circleHTTPClient, circleBaseURL = origClient, origBase }()

	t.Setenv("CIRCLE_API_KEY", "test-api-key")
	t.Setenv("CIRCLE_ENTITY_SECRET", testEntitySecretHex)
	t.Setenv("CIRCLE_SOURCE_WALLET_ID", "src-wallet")
	t.Setenv("CIRCLE_MERCHANT_WALLET_ID", "dst-wallet")

	st := newStore()
	d, code := dispatchTool(testCfg, st, "L2", "agentA", toolCallParams{
		Name: "create_checkout",
		Arguments: coArgs(map[string]any{
			"user_id":         "u1",
			"line_items":      []map[string]any{li("bali-alaya-ubud", "2026-07-01", "2026-07-03")},
			"settlement_rail": "circle_usdc",
		}),
	})
	if code != http.StatusOK || d["status"] != "incomplete" {
		t.Fatalf("create_checkout: %v", d)
	}
	cid := d["id"].(string)
	wantTotal := d["total_cents"].(int64)

	dispatchTool(testCfg, st, "L2", "agentA", toolCallParams{
		Name:      "update_checkout",
		Arguments: coArgs(map[string]any{"id": cid, "buyer_consent": true}),
	})

	// THIS call is the one that matters: dispatchTool's own "complete_checkout"
	// case is what calls maybeCircleSettle in production (mcp.go), after
	// checkoutTool has released st.mu. Nothing in this test stands in for that
	// wiring — it runs for real.
	resp, code2 := dispatchTool(testCfg, st, "L2", "agentA", toolCallParams{
		Name:      "complete_checkout",
		Arguments: coArgs(map[string]any{"id": cid}),
	})
	if code2 != http.StatusOK || resp["status"] != "complete" {
		t.Fatalf("complete_checkout: %v", resp)
	}

	settlement, ok := resp["circle_settlement"].(map[string]any)
	if !ok {
		t.Fatalf("expected circle_settlement in response, got %v", resp)
	}
	if settlement["transaction_id"] == "" || settlement["transaction_id"] == nil {
		t.Fatalf("expected a real transaction_id, got %v", settlement)
	}
	if settlement["rail"] != "circle_usdc_testnet" {
		t.Fatalf("expected rail=circle_usdc_testnet, got %v", settlement["rail"])
	}
	if atomic.LoadInt32(&mock.transferCalls) != 1 {
		t.Fatalf("expected exactly 1 real transfer call triggered by checkout completion, got %d", mock.transferCalls)
	}
	wantAmount := fmt.Sprintf("%d.%02d", wantTotal/100, wantTotal%100)
	if mock.transferAmount != wantAmount {
		t.Fatalf("Circle was sent amount %q, want %q (the booking's own total_cents=%d)",
			mock.transferAmount, wantAmount, wantTotal)
	}

	// A booking that does NOT opt into settlement_rail must be completely
	// unaffected — no Circle call, no circle_settlement field. This is the
	// "byte-identical when not opted in" guarantee the rest of this codebase
	// holds for its additive features (wallet, alipay). Also driven through
	// dispatchTool for the same reason as above.
	d2, _ := dispatchTool(testCfg, st, "L2", "agentA", toolCallParams{
		Name: "create_checkout",
		Arguments: coArgs(map[string]any{
			"user_id":    "u1",
			"line_items": []map[string]any{li("bali-alaya-ubud", "2026-07-01", "2026-07-03")},
		}),
	})
	cid2 := d2["id"].(string)
	dispatchTool(testCfg, st, "L2", "agentA", toolCallParams{
		Name:      "update_checkout",
		Arguments: coArgs(map[string]any{"id": cid2, "buyer_consent": true}),
	})
	resp2, _ := dispatchTool(testCfg, st, "L2", "agentA", toolCallParams{
		Name:      "complete_checkout",
		Arguments: coArgs(map[string]any{"id": cid2}),
	})
	if _, has := resp2["settlement_rail"]; has {
		t.Fatalf("booking without opt-in must not carry settlement_rail: %v", resp2)
	}
	if _, has := resp2["circle_settlement"]; has {
		t.Fatalf("booking without opt-in must not carry circle_settlement: %v", resp2)
	}
	if atomic.LoadInt32(&mock.transferCalls) != 1 {
		t.Fatalf("non-opted-in booking must not trigger a Circle call; transfer count changed to %d", mock.transferCalls)
	}
}

func TestFetchWalletAddressHandlesUpstreamFailures(t *testing.T) {
	mux := http.NewServeMux()
	mux.HandleFunc("/wallets/bad-status", func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusInternalServerError)
	})
	mux.HandleFunc("/wallets/no-address", func(w http.ResponseWriter, r *http.Request) {
		writeJSON(w, http.StatusOK, map[string]any{"data": map[string]any{"wallet": map[string]string{"id": "no-address"}}})
	})
	mux.HandleFunc("/wallets/malformed", func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusOK)
		_, _ = w.Write([]byte("not json"))
	})
	srv := httptest.NewServer(mux)
	defer srv.Close()

	origClient, origBase := circleHTTPClient, circleBaseURL
	circleHTTPClient = srv.Client()
	circleBaseURL = srv.URL
	defer func() { circleHTTPClient, circleBaseURL = origClient, origBase }()

	cfg := circleConfig{APIKey: "test-api-key"}
	for _, id := range []string{"bad-status", "no-address", "malformed"} {
		if _, err := fetchWalletAddress(cfg, id); err == nil {
			t.Errorf("fetchWalletAddress(%q): expected error, got nil", id)
		}
	}
}

func TestResolveUSDCTokenIDNoBalanceIsAnError(t *testing.T) {
	mux := http.NewServeMux()
	mux.HandleFunc("/wallets/empty-wallet/balances", func(w http.ResponseWriter, r *http.Request) {
		writeJSON(w, http.StatusOK, map[string]any{"data": map[string]any{"tokenBalances": []map[string]any{}}})
	})
	srv := httptest.NewServer(mux)
	defer srv.Close()

	origClient, origBase := circleHTTPClient, circleBaseURL
	circleHTTPClient = srv.Client()
	circleBaseURL = srv.URL
	defer func() { circleHTTPClient, circleBaseURL = origClient, origBase }()

	cfg := circleConfig{APIKey: "test-api-key", SourceWalletID: "empty-wallet"}
	if _, err := resolveUSDCTokenID(cfg); err == nil {
		t.Fatal("expected error when source wallet has no USDC balance, got nil")
	}
}

// Upstream error bodies (which can contain wallet IDs and other operational
// detail) must never be echoed verbatim to the HTTP caller.
func TestCircleAdminHandlerDoesNotLeakUpstreamErrorBody(t *testing.T) {
	const secretMarker = "wallet-id-should-not-leak-to-client"
	mux := http.NewServeMux()
	mux.HandleFunc("/config/entity/publicKey", func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusInternalServerError)
		_, _ = w.Write([]byte(secretMarker))
	})
	srv := httptest.NewServer(mux)
	defer srv.Close()

	origClient, origBase := circleHTTPClient, circleBaseURL
	circleHTTPClient = srv.Client()
	circleBaseURL = srv.URL
	defer func() { circleHTTPClient, circleBaseURL = origClient, origBase }()

	t.Setenv("CIRCLE_API_KEY", "test-api-key")
	t.Setenv("CIRCLE_ENTITY_SECRET", testEntitySecretHex)
	t.Setenv("CIRCLE_SOURCE_WALLET_ID", "src-wallet")
	t.Setenv("CIRCLE_MERCHANT_WALLET_ID", "dst-wallet")

	st := newStore()
	h := circleAdminHandler(st, testBudgetHardMaxCents)
	w := httptest.NewRecorder()
	h(w, httptest.NewRequest("POST", "/admin/circle/settle",
		strings.NewReader(`{"booking_ref":"BR-1","total_cents":5000}`)))
	if w.Code != http.StatusBadGateway {
		t.Fatalf("expected 502, got %d", w.Code)
	}
	if strings.Contains(w.Body.String(), secretMarker) {
		t.Fatalf("upstream error body leaked to client: %s", w.Body.String())
	}
}

func TestCircleAdminHandlerUnconfiguredReturns501(t *testing.T) {
	origClient, origBase := circleHTTPClient, circleBaseURL
	circleHTTPClient = &http.Client{Transport: roundTripFunc(func(*http.Request) (*http.Response, error) {
		t.Error("HTTP call made despite unconfigured credentials")
		return nil, fmt.Errorf("unexpected HTTP call in test")
	})}
	defer func() { circleHTTPClient, circleBaseURL = origClient, origBase }()

	// Ensure env vars are actually unset for this test's config load.
	t.Setenv("CIRCLE_API_KEY", "")
	t.Setenv("CIRCLE_ENTITY_SECRET", "")
	t.Setenv("CIRCLE_SOURCE_WALLET_ID", "")
	t.Setenv("CIRCLE_MERCHANT_WALLET_ID", "")

	st := newStore()
	h := circleAdminHandler(st, testBudgetHardMaxCents)
	w := httptest.NewRecorder()
	h(w, httptest.NewRequest("POST", "/admin/circle/settle",
		strings.NewReader(`{"booking_ref":"BR-1","total_cents":5000}`)))
	if w.Code != http.StatusNotImplemented {
		t.Fatalf("expected 501, got %d: %s", w.Code, w.Body.String())
	}
	var resp map[string]string
	if err := json.Unmarshal(w.Body.Bytes(), &resp); err != nil {
		t.Fatalf("response json: %v", err)
	}
	if resp["error"] != "CIRCLE_NOT_CONFIGURED" {
		t.Fatalf("unexpected error field: %v", resp)
	}
}

func TestCircleAdminHandlerRejectsMalformedInput(t *testing.T) {
	st := newStore()
	h := circleAdminHandler(st, testBudgetHardMaxCents)
	w := httptest.NewRecorder()
	h(w, httptest.NewRequest("POST", "/admin/circle/settle", strings.NewReader(`{"booking_ref":""}`)))
	if w.Code != http.StatusBadRequest {
		t.Fatalf("expected 400, got %d", w.Code)
	}
}

// The rail must not be a way to move money past this server's own configured
// budget ceiling — the same one enforced elsewhere in checkout.go.
func TestCircleAdminHandlerRejectsAmountAboveBudgetHardMax(t *testing.T) {
	st := newStore()
	h := circleAdminHandler(st, 1000) // hard max = $10.00
	w := httptest.NewRecorder()
	h(w, httptest.NewRequest("POST", "/admin/circle/settle",
		strings.NewReader(`{"booking_ref":"BR-big","total_cents":5000}`))) // $50.00
	if w.Code != http.StatusBadRequest {
		t.Fatalf("expected 400 for over-budget amount, got %d: %s", w.Code, w.Body.String())
	}
	var resp map[string]string
	if err := json.Unmarshal(w.Body.Bytes(), &resp); err != nil {
		t.Fatalf("response json: %v", err)
	}
	if resp["error"] != "exceeds_budget_hard_max" {
		t.Fatalf("unexpected error field: %v", resp)
	}
}

// An in-budget amount must actually succeed through the FULL handler (not
// just via circleSettle called directly) — the budget check must not be a
// false-negative that also blocks legitimate requests.
func TestCircleAdminHandlerSucceedsWithinBudget(t *testing.T) {
	key := mustTestRSAKey(t)
	_, srv := newMockCircleServer(t, key)
	defer srv.Close()

	origClient, origBase := circleHTTPClient, circleBaseURL
	circleHTTPClient = srv.Client()
	circleBaseURL = srv.URL
	defer func() { circleHTTPClient, circleBaseURL = origClient, origBase }()

	t.Setenv("CIRCLE_API_KEY", "test-api-key")
	t.Setenv("CIRCLE_ENTITY_SECRET", testEntitySecretHex)
	t.Setenv("CIRCLE_SOURCE_WALLET_ID", "src-wallet")
	t.Setenv("CIRCLE_MERCHANT_WALLET_ID", "dst-wallet")

	st := newStore()
	h := circleAdminHandler(st, testBudgetHardMaxCents)
	w := httptest.NewRecorder()
	h(w, httptest.NewRequest("POST", "/admin/circle/settle",
		strings.NewReader(`{"booking_ref":"BR-inbudget","total_cents":5000}`)))
	if w.Code != http.StatusOK {
		t.Fatalf("expected 200 for in-budget amount, got %d: %s", w.Code, w.Body.String())
	}
}

// A booking_ref replayed with a different total_cents must return 409 through
// the FULL handler, not just when circleSettle is called directly.
func TestCircleAdminHandlerReturnsConflictOnAmountMismatch(t *testing.T) {
	key := mustTestRSAKey(t)
	_, srv := newMockCircleServer(t, key)
	defer srv.Close()

	origClient, origBase := circleHTTPClient, circleBaseURL
	circleHTTPClient = srv.Client()
	circleBaseURL = srv.URL
	defer func() { circleHTTPClient, circleBaseURL = origClient, origBase }()

	t.Setenv("CIRCLE_API_KEY", "test-api-key")
	t.Setenv("CIRCLE_ENTITY_SECRET", testEntitySecretHex)
	t.Setenv("CIRCLE_SOURCE_WALLET_ID", "src-wallet")
	t.Setenv("CIRCLE_MERCHANT_WALLET_ID", "dst-wallet")

	st := newStore()
	h := circleAdminHandler(st, testBudgetHardMaxCents)
	w1 := httptest.NewRecorder()
	h(w1, httptest.NewRequest("POST", "/admin/circle/settle",
		strings.NewReader(`{"booking_ref":"BR-dup","total_cents":5000}`)))
	if w1.Code != http.StatusOK {
		t.Fatalf("initial settle: HTTP %d: %s", w1.Code, w1.Body.String())
	}
	w2 := httptest.NewRecorder()
	h(w2, httptest.NewRequest("POST", "/admin/circle/settle",
		strings.NewReader(`{"booking_ref":"BR-dup","total_cents":9999}`)))
	if w2.Code != http.StatusConflict {
		t.Fatalf("expected 409 for amount-mismatch replay, got %d: %s", w2.Code, w2.Body.String())
	}
}

// A non-2xx from Circle's transfer endpoint specifically (not the publicKey
// endpoint, which TestCircleAdminHandlerDoesNotLeakUpstreamErrorBody already
// covers) must also 502 without leaking the upstream body.
func TestCircleAdminHandlerTransferFailureReturns502(t *testing.T) {
	const secretMarker = "wallet-id-should-not-leak-transfer-path"
	key := mustTestRSAKey(t)
	pubPEM := pemEncodePKIX(t, &key.PublicKey)
	mux := http.NewServeMux()
	mux.HandleFunc("/config/entity/publicKey", func(w http.ResponseWriter, r *http.Request) {
		writeJSON(w, http.StatusOK, map[string]any{"data": map[string]string{"publicKey": pubPEM}})
	})
	mux.HandleFunc("/wallets/dst-wallet", func(w http.ResponseWriter, r *http.Request) {
		writeJSON(w, http.StatusOK, map[string]any{
			"data": map[string]any{"wallet": map[string]string{"address": "0xdstAddr000000000000000000000000000000"}},
		})
	})
	mux.HandleFunc("/wallets/src-wallet/balances", func(w http.ResponseWriter, r *http.Request) {
		writeJSON(w, http.StatusOK, map[string]any{
			"data": map[string]any{"tokenBalances": []map[string]any{
				{"token": map[string]string{"id": "mock-usdc-token-id", "symbol": "USDC"}, "amount": "20"},
			}},
		})
	})
	mux.HandleFunc("/developer/transactions/transfer", func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusBadRequest)
		_, _ = w.Write([]byte(secretMarker))
	})
	srv := httptest.NewServer(mux)
	defer srv.Close()

	origClient, origBase := circleHTTPClient, circleBaseURL
	circleHTTPClient = srv.Client()
	circleBaseURL = srv.URL
	defer func() { circleHTTPClient, circleBaseURL = origClient, origBase }()

	t.Setenv("CIRCLE_API_KEY", "test-api-key")
	t.Setenv("CIRCLE_ENTITY_SECRET", testEntitySecretHex)
	t.Setenv("CIRCLE_SOURCE_WALLET_ID", "src-wallet")
	t.Setenv("CIRCLE_MERCHANT_WALLET_ID", "dst-wallet")

	st := newStore()
	h := circleAdminHandler(st, testBudgetHardMaxCents)
	w := httptest.NewRecorder()
	h(w, httptest.NewRequest("POST", "/admin/circle/settle",
		strings.NewReader(`{"booking_ref":"BR-transfer-fail","total_cents":5000}`)))
	if w.Code != http.StatusBadGateway {
		t.Fatalf("expected 502, got %d", w.Code)
	}
	if strings.Contains(w.Body.String(), secretMarker) {
		t.Fatalf("upstream transfer-endpoint error body leaked to client: %s", w.Body.String())
	}
}

func TestResolveUSDCTokenIDBalancesNon200(t *testing.T) {
	mux := http.NewServeMux()
	mux.HandleFunc("/wallets/bad-wallet/balances", func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusInternalServerError)
	})
	srv := httptest.NewServer(mux)
	defer srv.Close()

	origClient, origBase := circleHTTPClient, circleBaseURL
	circleHTTPClient = srv.Client()
	circleBaseURL = srv.URL
	defer func() { circleHTTPClient, circleBaseURL = origClient, origBase }()

	cfg := circleConfig{APIKey: "test-api-key", SourceWalletID: "bad-wallet"}
	if _, err := resolveUSDCTokenID(cfg); err == nil {
		t.Fatal("expected error on non-200 balances response, got nil")
	}
}

func TestCheckCircleStartupSafety(t *testing.T) {
	unconfigured := circleConfig{}
	configured := circleConfig{
		APIKey: "k", EntitySecretHex: "s", SourceWalletID: "src", MerchantWalletID: "dst",
	}
	cases := []struct {
		name      string
		cfg       config
		circleCfg circleConfig
		wantErr   bool
	}{
		{"not configured, nothing else matters", config{}, unconfigured, false},
		{"configured, no admin token", config{AdminToken: ""}, configured, true},
		{"configured, admin token, signatures required", config{AdminToken: "t", RequireSignatures: true, UnsignedTier: "L2"}, configured, false},
		{"configured, admin token, unsigned floor L1 (safe default)", config{AdminToken: "t", RequireSignatures: false, UnsignedTier: "L1"}, configured, false},
		{"configured, admin token, unsigned floor L2 (VULNERABLE — demo setting)", config{AdminToken: "t", RequireSignatures: false, UnsignedTier: "L2"}, configured, true},
		{"configured, admin token, unsigned floor L3 (VULNERABLE)", config{AdminToken: "t", RequireSignatures: false, UnsignedTier: "L3"}, configured, true},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			err := checkCircleStartupSafety(c.cfg, c.circleCfg)
			if c.wantErr && err == nil {
				t.Fatal("expected an error, got nil")
			}
			if !c.wantErr && err != nil {
				t.Fatalf("expected no error, got %v", err)
			}
		})
	}
}

func TestCircleAdminHandlerGetSummaryCarriesHonestLabels(t *testing.T) {
	st := newStore()
	h := circleAdminHandler(st, testBudgetHardMaxCents)
	w := httptest.NewRecorder()
	h(w, httptest.NewRequest("GET", "/admin/circle/settle", nil))
	if w.Code != http.StatusOK {
		t.Fatalf("GET summary HTTP %d", w.Code)
	}
	var resp map[string]any
	if err := json.Unmarshal(w.Body.Bytes(), &resp); err != nil {
		t.Fatalf("response json: %v", err)
	}
	if resp["rail"] != "circle_usdc_testnet" {
		t.Fatalf("expected rail=circle_usdc_testnet, got %v", resp["rail"])
	}
	if resp["network"] != circleNetwork {
		t.Fatalf("expected network=%v, got %v", circleNetwork, resp["network"])
	}
}

func TestDeterministicUUIDStableAndShaped(t *testing.T) {
	a := deterministicUUID("BR-1")
	b := deterministicUUID("BR-1")
	c := deterministicUUID("BR-2")
	if a != b {
		t.Fatal("deterministicUUID not stable for the same seed")
	}
	if a == c {
		t.Fatal("deterministicUUID collided across different seeds")
	}
	if !idemKeyPattern.MatchString(a) {
		t.Fatalf("deterministicUUID not UUID-shaped: %q", a)
	}
	// Version 4, variant 10xx per RFC 4122 — Circle documents idempotencyKey as UUIDv4.
	if a[14] != '4' {
		t.Fatalf("deterministicUUID version nibble not 4: %q", a)
	}
	if v := a[19]; v != '8' && v != '9' && v != 'a' && v != 'b' {
		t.Fatalf("deterministicUUID variant nibble not 10xx: %q", a)
	}
}

var idemKeyPattern = regexp.MustCompile(`^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$`)

// roundTripFunc adapts a plain function to http.RoundTripper, for stubbing
// circleHTTPClient in tests without spinning up a server.
type roundTripFunc func(*http.Request) (*http.Response, error)

func (f roundTripFunc) RoundTrip(r *http.Request) (*http.Response, error) { return f(r) }
