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
	"errors"
	"fmt"
	"net/http"
	"net/http/httptest"
	"regexp"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"
)

// testBudgetHardMaxCents mirrors the default BUDGET_HARD_MAX_USD ($2000) used
// elsewhere in this repo — large enough not to interfere with the small test
// amounts below, so tests below the cap and above it are both exercisable.
const testBudgetHardMaxCents = 200000

// testCircleAggregateCapCents mirrors circleDefaultAggregateCapUSD ($10,000).
const testCircleAggregateCapCents = 1000000

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
	pubPEM        string
	transferCalls int32
	// transferAmountMu guards transferAmount — genuinely concurrent transfers
	// for DIFFERENT booking_refs (see
	// TestCircleSettleAggregateCapUnderConcurrentDifferentBookingRefs) can
	// reach this handler on separate goroutines at once, unlike the
	// same-booking_ref concurrency test where the in-flight marker guarantees
	// only one live transfer at a time.
	transferAmountMu sync.Mutex
	transferAmount   string // last-seen amounts[0], for assertions (read only after the writing goroutine(s) have finished)
	// failTransfer, when set, makes the /developer/transactions/transfer
	// handler return 502 instead of recording a success — used to prove a
	// failed attempt releases its aggregate-cap reservation (see
	// TestCircleSettleFailedTransferDoesNotConsumeCap).
	failTransfer atomic.Bool
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
			m.transferAmountMu.Lock()
			m.transferAmount = body.Amounts[0]
			m.transferAmountMu.Unlock()
		}
		if m.failTransfer.Load() {
			w.WriteHeader(http.StatusBadGateway)
			return
		}
		writeJSON(w, http.StatusOK, map[string]any{
			// Real transaction ids observed from live Circle calls are UUIDs;
			// txn-live-<n> here is just a distinguishable mock value. Read via
			// atomic.LoadInt32 (not a plain field read) — concurrent transfers
			// for DIFFERENT booking_refs can hit this handler on separate
			// goroutines at once (see TestCircleSettleAggregateCapUnderConcurrentDifferentBookingRefs).
			"data": map[string]string{"id": fmt.Sprintf("txn-live-%d", atomic.LoadInt32(&m.transferCalls)), "state": "PENDING"},
		})
	})
	mux.HandleFunc("/transactions/", func(w http.ResponseWriter, r *http.Request) {
		// The mock transfer response above always has a hash available on the
		// very first poll — real Circle sometimes needs a few tries (see
		// TestPollTxHashRetriesUntilHashAppears for that case, tested against
		// pollTxHash directly rather than through this shared server).
		writeJSON(w, http.StatusOK, map[string]any{
			"data": map[string]any{
				"state":  "SENT",
				"txHash": "0xmocktxhash000000000000000000000000000000000000000000000000000",
			},
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
		APIKey:            "test-api-key",
		EntitySecretHex:   testEntitySecretHex,
		SourceWalletID:    "src-wallet",
		MerchantWalletID:  "dst-wallet",
		AggregateCapCents: testCircleAggregateCapCents,
	}

	rec, attempted, err := st.circleSettle(cfg, "BR-42", 5000)
	if !attempted {
		t.Fatal("expected attempted=true when configured")
	}
	if err != nil {
		t.Fatalf("circleSettle: %v", err)
	}
	// Status is "SENT" (not the transfer response's initial "PENDING") because
	// the follow-up /transactions/{id} poll's state, when present, is the more
	// current truth — see circleSettle's "status := parsed.Data.State ... if
	// polledState != ''" logic.
	if rec.Status != "SENT" || rec.TransactionID == "" {
		t.Fatalf("unexpected settlement record: %+v", rec)
	}
	if rec.TxHash == "" {
		t.Fatal("expected a tx_hash populated from the /transactions/{id} poll")
	}
	if got, want := blockExplorerURL(rec.TxHash), "https://sepolia.etherscan.io/tx/"+rec.TxHash; got != want {
		t.Fatalf("blockExplorerURL: got %q want %q", got, want)
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
		AggregateCapCents: testCircleAggregateCapCents,
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
		AggregateCapCents: testCircleAggregateCapCents,
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

// Real Circle behavior (per the module header's live-verified note): the
// initial transfer response never carries a tx hash — it shows up only once
// polled, and sometimes not on the very first poll. Simulate a hash that
// only appears on the 3rd attempt.
func TestPollTxHashRetriesUntilHashAppears(t *testing.T) {
	var calls int32
	mux := http.NewServeMux()
	mux.HandleFunc("/transactions/txn-slow", func(w http.ResponseWriter, r *http.Request) {
		n := atomic.AddInt32(&calls, 1)
		if n < 3 {
			writeJSON(w, http.StatusOK, map[string]any{"data": map[string]string{"state": "PENDING", "txHash": ""}})
			return
		}
		writeJSON(w, http.StatusOK, map[string]any{"data": map[string]string{"state": "SENT", "txHash": "0xfoundit"}})
	})
	srv := httptest.NewServer(mux)
	defer srv.Close()

	origClient, origBase := circleHTTPClient, circleBaseURL
	circleHTTPClient = srv.Client()
	circleBaseURL = srv.URL
	defer func() { circleHTTPClient, circleBaseURL = origClient, origBase }()

	origInterval := circlePollInterval
	circlePollInterval = 1 * time.Millisecond // keep the test fast
	defer func() { circlePollInterval = origInterval }()

	cfg := circleConfig{APIKey: "test-api-key"}
	hash, state := pollTxHash(cfg, "txn-slow")
	if hash != "0xfoundit" {
		t.Fatalf("expected hash 0xfoundit, got %q (state=%q, calls=%d)", hash, state, calls)
	}
	if state != "SENT" {
		t.Fatalf("expected final state SENT, got %q", state)
	}
	if calls != 3 {
		t.Fatalf("expected exactly 3 attempts, got %d", calls)
	}
}

// A terminal state with no hash (e.g. the transfer failed after submission)
// must stop polling immediately rather than exhausting all attempts.
func TestPollTxHashStopsOnTerminalStateWithoutHash(t *testing.T) {
	var calls int32
	mux := http.NewServeMux()
	mux.HandleFunc("/transactions/txn-failed", func(w http.ResponseWriter, r *http.Request) {
		atomic.AddInt32(&calls, 1)
		writeJSON(w, http.StatusOK, map[string]any{"data": map[string]string{"state": "FAILED", "txHash": ""}})
	})
	srv := httptest.NewServer(mux)
	defer srv.Close()

	origClient, origBase := circleHTTPClient, circleBaseURL
	circleHTTPClient = srv.Client()
	circleBaseURL = srv.URL
	defer func() { circleHTTPClient, circleBaseURL = origClient, origBase }()

	origInterval := circlePollInterval
	circlePollInterval = 1 * time.Millisecond
	defer func() { circlePollInterval = origInterval }()

	cfg := circleConfig{APIKey: "test-api-key"}
	hash, state := pollTxHash(cfg, "txn-failed")
	if hash != "" {
		t.Fatalf("expected no hash for a failed transaction, got %q", hash)
	}
	if state != "FAILED" {
		t.Fatalf("expected state FAILED, got %q", state)
	}
	if calls != 1 {
		t.Fatalf("expected polling to stop after 1 attempt on a terminal state, got %d calls", calls)
	}
}

// A hash that never appears (and never reaches a terminal state) must not
// hang forever — pollTxHash must give up after circlePollMaxAttempts.
func TestPollTxHashGivesUpAfterMaxAttempts(t *testing.T) {
	var calls int32
	mux := http.NewServeMux()
	mux.HandleFunc("/transactions/txn-stuck", func(w http.ResponseWriter, r *http.Request) {
		atomic.AddInt32(&calls, 1)
		writeJSON(w, http.StatusOK, map[string]any{"data": map[string]string{"state": "PENDING", "txHash": ""}})
	})
	srv := httptest.NewServer(mux)
	defer srv.Close()

	origClient, origBase := circleHTTPClient, circleBaseURL
	circleHTTPClient = srv.Client()
	circleBaseURL = srv.URL
	defer func() { circleHTTPClient, circleBaseURL = origClient, origBase }()

	origInterval := circlePollInterval
	circlePollInterval = 1 * time.Millisecond
	defer func() { circlePollInterval = origInterval }()

	cfg := circleConfig{APIKey: "test-api-key"}
	hash, state := pollTxHash(cfg, "txn-stuck")
	if hash != "" {
		t.Fatalf("expected no hash, got %q", hash)
	}
	if state != "PENDING" {
		t.Fatalf("expected last-seen state PENDING, got %q", state)
	}
	if calls != circlePollMaxAttempts {
		t.Fatalf("expected exactly circlePollMaxAttempts=%d attempts, got %d", circlePollMaxAttempts, calls)
	}
}

func TestPollTxHashNonOKStatusReturnsEmpty(t *testing.T) {
	mux := http.NewServeMux()
	mux.HandleFunc("/transactions/txn-500", func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusInternalServerError)
	})
	srv := httptest.NewServer(mux)
	defer srv.Close()

	origClient, origBase := circleHTTPClient, circleBaseURL
	circleHTTPClient = srv.Client()
	circleBaseURL = srv.URL
	defer func() { circleHTTPClient, circleBaseURL = origClient, origBase }()

	cfg := circleConfig{APIKey: "test-api-key"}
	hash, state := pollTxHash(cfg, "txn-500")
	if hash != "" || state != "" {
		t.Fatalf("expected empty hash/state on non-200, got hash=%q state=%q", hash, state)
	}
}

func TestBlockExplorerURL(t *testing.T) {
	if got := blockExplorerURL(""); got != "" {
		t.Fatalf("expected empty URL for empty tx hash, got %q", got)
	}
	const hash = "0xabc123"
	if got, want := blockExplorerURL(hash), "https://sepolia.etherscan.io/tx/"+hash; got != want {
		t.Fatalf("got %q want %q", got, want)
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

// ===== NEW-4: aggregate spend ceiling ======================================

// TestCircleSettleAggregateCapBlocksSecondBooking proves the reservation
// closes the check-then-increment race window: a second booking that would
// push cumulative outflow past the cap is refused, and the refused attempt
// consumes no headroom of its own.
func TestCircleSettleAggregateCapBlocksSecondBooking(t *testing.T) {
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
		AggregateCapCents: 7500,
	}

	if _, attempted, err := st.circleSettle(cfg, "BR-a", 5000); !attempted || err != nil {
		t.Fatalf("first settle: attempted=%v err=%v", attempted, err)
	}
	_, attempted, err := st.circleSettle(cfg, "BR-b", 5000)
	if !attempted {
		t.Fatal("expected attempted=true (a cap refusal is a config-driven error, not an unconfigured short-circuit)")
	}
	var capErr *circleCapExceededError
	if !errors.As(err, &capErr) {
		t.Fatalf("expected *circleCapExceededError, got %v", err)
	}
	if capErr.SettledCents != 5000 || capErr.CapCents != 7500 {
		t.Fatalf("unexpected error fields: %+v", capErr)
	}
	if atomic.LoadInt32(&mock.transferCalls) != 1 {
		t.Fatalf("expected exactly 1 transfer call, got %d", mock.transferCalls)
	}
	_, total := st.circleSummary()
	if total != 5000 {
		t.Fatalf("expected summary total 5000, got %d", total)
	}
	if got := st.circleCommitted(); got != 5000 {
		t.Fatalf("expected circleCommitted()==5000 (refused attempt reserved nothing), got %d", got)
	}
}

// TestCircleSettleAggregateCapExactBoundaryAllowed proves the cap is
// inclusive: a settlement that exactly fills it succeeds.
func TestCircleSettleAggregateCapExactBoundaryAllowed(t *testing.T) {
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
		AggregateCapCents: 5000,
	}
	if _, attempted, err := st.circleSettle(cfg, "BR-exact", 5000); !attempted || err != nil {
		t.Fatalf("boundary settle should succeed (cap is inclusive): attempted=%v err=%v", attempted, err)
	}
	_, attempted, err := st.circleSettle(cfg, "BR-over", 1)
	var capErr *circleCapExceededError
	if !attempted || !errors.As(err, &capErr) {
		t.Fatalf("expected a cap error for the 1-cent overage: attempted=%v err=%v", attempted, err)
	}
}

// TestCircleSettleAggregateCapNotDoubleCountedOnReplay proves an idempotent
// replay of an already-settled booking_ref consumes no additional headroom.
func TestCircleSettleAggregateCapNotDoubleCountedOnReplay(t *testing.T) {
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
		AggregateCapCents: 10000,
	}
	if _, attempted, err := st.circleSettle(cfg, "BR-a", 6000); !attempted || err != nil {
		t.Fatalf("initial settle: attempted=%v err=%v", attempted, err)
	}
	if _, attempted, err := st.circleSettle(cfg, "BR-a", 6000); !attempted || err != nil {
		t.Fatalf("replay: attempted=%v err=%v", attempted, err)
	}
	if atomic.LoadInt32(&mock.transferCalls) != 1 {
		t.Fatalf("replay must not hit the network again, got %d calls", mock.transferCalls)
	}
	if got := st.circleCommitted(); got != 6000 {
		t.Fatalf("expected circleCommitted()==6000 after replay, got %d", got)
	}
	// Proves the replay consumed no additional headroom: a fresh booking that
	// only fits if BR-a's replay was free must still succeed.
	if _, attempted, err := st.circleSettle(cfg, "BR-b", 4000); !attempted || err != nil {
		t.Fatalf("BR-b settle should succeed: attempted=%v err=%v", attempted, err)
	}
	_, attempted, err := st.circleSettle(cfg, "BR-c", 1)
	var capErr *circleCapExceededError
	if !attempted || !errors.As(err, &capErr) {
		t.Fatalf("expected BR-c to hit the now-exhausted cap: attempted=%v err=%v", attempted, err)
	}
}

// TestCircleSettleFailedTransferDoesNotConsumeCap proves a failed upstream
// attempt releases its reservation rather than permanently consuming headroom.
func TestCircleSettleFailedTransferDoesNotConsumeCap(t *testing.T) {
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
		AggregateCapCents: 5000,
	}
	mock.failTransfer.Store(true)
	if _, attempted, err := st.circleSettle(cfg, "BR-fail", 5000); !attempted || err == nil {
		t.Fatalf("expected an error from the failed transfer: attempted=%v err=%v", attempted, err)
	}
	if got := st.circleCommitted(); got != 0 {
		t.Fatalf("expected circleCommitted()==0 after a failed transfer (reservation released), got %d", got)
	}
	mock.failTransfer.Store(false)
	if _, attempted, err := st.circleSettle(cfg, "BR-ok", 5000); !attempted || err != nil {
		t.Fatalf("expected the retry to succeed once headroom was released: attempted=%v err=%v", attempted, err)
	}
}

// TestCircleSettleUnconfiguredDoesNotConsumeCap proves the unconfigured
// short-circuit (no HTTP call at all) never touches the aggregate counter.
func TestCircleSettleUnconfiguredDoesNotConsumeCap(t *testing.T) {
	key := mustTestRSAKey(t)
	mock, srv := newMockCircleServer(t, key)
	defer srv.Close()

	origClient, origBase := circleHTTPClient, circleBaseURL
	circleHTTPClient = srv.Client()
	circleBaseURL = srv.URL
	defer func() { circleHTTPClient, circleBaseURL = origClient, origBase }()

	st := newStore()
	unconfigured := circleConfig{AggregateCapCents: 5000} // cap set, but no credentials
	rec, attempted, err := st.circleSettle(unconfigured, "BR-x", 5000)
	if attempted {
		t.Fatal("expected attempted=false for an unconfigured rail")
	}
	if err != nil {
		t.Fatalf("expected nil error, got %v", err)
	}
	if rec != (circleSettlement{}) {
		t.Fatalf("expected zero-value settlement, got %+v", rec)
	}
	if got := st.circleCommitted(); got != 0 {
		t.Fatalf("expected circleCommitted()==0, got %d", got)
	}

	configured := circleConfig{
		APIKey: "test-api-key", EntitySecretHex: testEntitySecretHex,
		SourceWalletID: "src-wallet", MerchantWalletID: "dst-wallet",
		AggregateCapCents: 5000,
	}
	if _, attempted, err := st.circleSettle(configured, "BR-y", 5000); !attempted || err != nil {
		t.Fatalf("configured settle should succeed: attempted=%v err=%v", attempted, err)
	}
	if atomic.LoadInt32(&mock.transferCalls) != 1 {
		t.Fatalf("expected exactly 1 transfer call, got %d", mock.transferCalls)
	}
}

// TestCircleSettleZeroCapBlocksEverything proves AggregateCapCents==0 is a
// kill switch: a credentialed, otherwise-working rail settles nothing.
func TestCircleSettleZeroCapBlocksEverything(t *testing.T) {
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
		AggregateCapCents: 0,
	}
	_, attempted, err := st.circleSettle(cfg, "BR-zero", 5000)
	var capErr *circleCapExceededError
	if !attempted || !errors.As(err, &capErr) {
		t.Fatalf("expected a cap error even for a nonzero amount against a 0 cap: attempted=%v err=%v", attempted, err)
	}
	if atomic.LoadInt32(&mock.transferCalls) != 0 {
		t.Fatalf("expected no transfer call, got %d", mock.transferCalls)
	}
}

// TestCircleSettleAggregateCapUnderConcurrentDifferentBookingRefs is the key
// concurrency test: N concurrent settles for N DIFFERENT booking_refs must
// not collectively overshoot the cap. A check-then-increment implementation
// (rather than reserve-then-commit) fails this test — that is its whole
// purpose. Run with -race.
func TestCircleSettleAggregateCapUnderConcurrentDifferentBookingRefs(t *testing.T) {
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
		AggregateCapCents: 20000,
	}

	const n = 8
	var wg sync.WaitGroup
	errs := make([]error, n)
	for i := 0; i < n; i++ {
		wg.Add(1)
		go func(i int) {
			defer wg.Done()
			_, _, err := st.circleSettle(cfg, fmt.Sprintf("BR-cap-%d", i), 5000)
			errs[i] = err
		}(i)
	}
	wg.Wait()

	var okCount, capCount int
	for _, err := range errs {
		var capErr *circleCapExceededError
		switch {
		case err == nil:
			okCount++
		case errors.As(err, &capErr):
			capCount++
		default:
			t.Fatalf("unexpected error: %v", err)
		}
	}
	if okCount != 4 || capCount != 4 {
		t.Fatalf("expected 4 successes and 4 cap refusals (cap 20000 / 5000 per booking), got %d ok, %d cap", okCount, capCount)
	}
	if atomic.LoadInt32(&mock.transferCalls) != 4 {
		t.Fatalf("expected exactly 4 transfer calls, got %d", mock.transferCalls)
	}
	_, total := st.circleSummary()
	if total != 20000 {
		t.Fatalf("expected summary total 20000, got %d", total)
	}
	if got := st.circleCommitted(); got != 20000 {
		t.Fatalf("expected circleCommitted()==20000, got %d", got)
	}
}

// TestCircleSettleCommittedMatchesSummaryWhenQuiescent asserts the stated
// invariant (circleSettle's doc comment on circleCommittedCents): once no
// settle is in flight, circleCommitted() equals the sum of recorded
// settlements — after a mix of successes, a failure, a replay, and a cap
// refusal.
func TestCircleSettleCommittedMatchesSummaryWhenQuiescent(t *testing.T) {
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
		AggregateCapCents: 100000,
	}

	// 2 successes.
	if _, attempted, err := st.circleSettle(cfg, "BR-1", 1000); !attempted || err != nil {
		t.Fatalf("BR-1: attempted=%v err=%v", attempted, err)
	}
	if _, attempted, err := st.circleSettle(cfg, "BR-2", 2000); !attempted || err != nil {
		t.Fatalf("BR-2: attempted=%v err=%v", attempted, err)
	}
	// 1 failure (transfer rejected upstream).
	mock.failTransfer.Store(true)
	if _, attempted, err := st.circleSettle(cfg, "BR-3", 3000); !attempted || err == nil {
		t.Fatalf("BR-3 should fail: attempted=%v err=%v", attempted, err)
	}
	mock.failTransfer.Store(false)
	// 1 replay.
	if _, attempted, err := st.circleSettle(cfg, "BR-1", 1000); !attempted || err != nil {
		t.Fatalf("BR-1 replay: attempted=%v err=%v", attempted, err)
	}
	// 1 cap refusal (well beyond remaining headroom).
	_, attempted, err := st.circleSettle(cfg, "BR-4", 200000)
	var capErr *circleCapExceededError
	if !attempted || !errors.As(err, &capErr) {
		t.Fatalf("BR-4 should be cap-refused: attempted=%v err=%v", attempted, err)
	}

	_, total := st.circleSummary()
	if got := st.circleCommitted(); got != int64(total) {
		t.Fatalf("invariant violated: circleCommitted()=%d != summary total=%d", got, total)
	}
}

// TestCircleAdminHandlerRejectsWhenAggregateCapExhausted proves the admin
// entry point returns 403 exceeds_circle_aggregate_cap, with numbers, once
// the cumulative ceiling is exhausted — and that the refused POST never
// reaches the transfer endpoint.
func TestCircleAdminHandlerRejectsWhenAggregateCapExhausted(t *testing.T) {
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
	// circleAdminHandler calls loadCircleConfig() at CONSTRUCTION time, so this
	// must be set before the handler below is built.
	t.Setenv("CIRCLE_AGGREGATE_CAP_USD", "50.00") // 5000 cents

	st := newStore()
	h := circleAdminHandler(st, testBudgetHardMaxCents)

	w1 := httptest.NewRecorder()
	h(w1, httptest.NewRequest("POST", "/admin/circle/settle",
		strings.NewReader(`{"booking_ref":"BR-1","total_cents":5000}`)))
	if w1.Code != http.StatusOK {
		t.Fatalf("first settle: HTTP %d: %s", w1.Code, w1.Body.String())
	}

	w2 := httptest.NewRecorder()
	h(w2, httptest.NewRequest("POST", "/admin/circle/settle",
		strings.NewReader(`{"booking_ref":"BR-2","total_cents":5000}`)))
	if w2.Code != http.StatusForbidden {
		t.Fatalf("expected 403, got %d: %s", w2.Code, w2.Body.String())
	}
	var resp map[string]any
	if err := json.Unmarshal(w2.Body.Bytes(), &resp); err != nil {
		t.Fatalf("response json: %v", err)
	}
	if resp["error"] != "exceeds_circle_aggregate_cap" {
		t.Fatalf("unexpected error field: %v", resp)
	}
	if got := int64(resp["aggregate_cap_cents"].(float64)); got != 5000 {
		t.Fatalf("unexpected aggregate_cap_cents: %v", got)
	}
	if got := int64(resp["settled_cents"].(float64)); got != 5000 {
		t.Fatalf("unexpected settled_cents: %v", got)
	}
	if got := int64(resp["requested_cents"].(float64)); got != 5000 {
		t.Fatalf("unexpected requested_cents: %v", got)
	}
	if atomic.LoadInt32(&mock.transferCalls) != 1 {
		t.Fatalf("second POST must not have increased transferCalls, got %d", mock.transferCalls)
	}
}

// TestCircleAdminHandlerGetSummaryReportsAggregateCap proves the GET summary
// carries aggregate_cap_cents/aggregate_committed_cents/aggregate_remaining_cents,
// and that remaining floors at 0 rather than going negative.
func TestCircleAdminHandlerGetSummaryReportsAggregateCap(t *testing.T) {
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
	t.Setenv("CIRCLE_AGGREGATE_CAP_USD", "200.00") // 20000 cents

	st := newStore()
	h := circleAdminHandler(st, testBudgetHardMaxCents)

	w1 := httptest.NewRecorder()
	h(w1, httptest.NewRequest("POST", "/admin/circle/settle",
		strings.NewReader(`{"booking_ref":"BR-1","total_cents":5000}`)))
	if w1.Code != http.StatusOK {
		t.Fatalf("settle: HTTP %d: %s", w1.Code, w1.Body.String())
	}

	w2 := httptest.NewRecorder()
	h(w2, httptest.NewRequest("GET", "/admin/circle/settle", nil))
	if w2.Code != http.StatusOK {
		t.Fatalf("GET: HTTP %d", w2.Code)
	}
	var resp map[string]any
	if err := json.Unmarshal(w2.Body.Bytes(), &resp); err != nil {
		t.Fatalf("response json: %v", err)
	}
	if got := int64(resp["aggregate_cap_cents"].(float64)); got != 20000 {
		t.Fatalf("unexpected aggregate_cap_cents: %v", got)
	}
	if got := int64(resp["aggregate_committed_cents"].(float64)); got != 5000 {
		t.Fatalf("unexpected aggregate_committed_cents: %v", got)
	}
	if got := int64(resp["aggregate_remaining_cents"].(float64)); got != 15000 {
		t.Fatalf("unexpected aggregate_remaining_cents: %v", got)
	}

	// Floor check: a fresh store/handler with cap 0 must report
	// aggregate_remaining_cents==0, never negative.
	st2 := newStore()
	t.Setenv("CIRCLE_AGGREGATE_CAP_USD", "0")
	h2 := circleAdminHandler(st2, testBudgetHardMaxCents)
	w3 := httptest.NewRecorder()
	h2(w3, httptest.NewRequest("GET", "/admin/circle/settle", nil))
	var resp2 map[string]any
	if err := json.Unmarshal(w3.Body.Bytes(), &resp2); err != nil {
		t.Fatalf("response json: %v", err)
	}
	if got := int64(resp2["aggregate_remaining_cents"].(float64)); got != 0 {
		t.Fatalf("expected remaining to floor at 0, got %v", got)
	}
}

// TestCompleteCheckoutAggregateCapBlocksSecondBookingEndToEnd is the one that
// proves the agent-driven path is covered: through dispatchTool itself (the
// exact function /api/ucp/mcp calls in production), a first booking exhausts
// the aggregate cap and a second, otherwise-identical booking still COMPLETES
// (the booking commits) but carries a cap-refusal circle_settlement with no
// transaction_id — and the agent-facing map does not leak the cap numbers
// (§5's deliberate asymmetry vs. the admin path).
func TestCompleteCheckoutAggregateCapBlocksSecondBookingEndToEnd(t *testing.T) {
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
	t.Setenv("CIRCLE_AGGREGATE_CAP_USD", "330.00") // 33000 cents — exactly one booking's worth

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
	dispatchTool(testCfg, st, "L2", "agentA", toolCallParams{
		Name:      "update_checkout",
		Arguments: coArgs(map[string]any{"id": cid, "buyer_consent": true}),
	})
	resp, code2 := dispatchTool(testCfg, st, "L2", "agentA", toolCallParams{
		Name:      "complete_checkout",
		Arguments: coArgs(map[string]any{"id": cid}),
	})
	if code2 != http.StatusOK || resp["status"] != "complete" {
		t.Fatalf("first complete_checkout: %v", resp)
	}
	settlement, ok := resp["circle_settlement"].(map[string]any)
	if !ok || settlement["transaction_id"] == "" || settlement["transaction_id"] == nil {
		t.Fatalf("expected a real transaction_id on the first booking, got %v", resp)
	}

	d2, code3 := dispatchTool(testCfg, st, "L2", "agentA", toolCallParams{
		Name: "create_checkout",
		Arguments: coArgs(map[string]any{
			"user_id":         "u1",
			"line_items":      []map[string]any{li("bali-alaya-ubud", "2026-07-01", "2026-07-03")},
			"settlement_rail": "circle_usdc",
		}),
	})
	if code3 != http.StatusOK {
		t.Fatalf("create_checkout #2: %v", d2)
	}
	cid2 := d2["id"].(string)
	dispatchTool(testCfg, st, "L2", "agentA", toolCallParams{
		Name:      "update_checkout",
		Arguments: coArgs(map[string]any{"id": cid2, "buyer_consent": true}),
	})
	resp2, code4 := dispatchTool(testCfg, st, "L2", "agentA", toolCallParams{
		Name:      "complete_checkout",
		Arguments: coArgs(map[string]any{"id": cid2}),
	})
	if code4 != http.StatusOK || resp2["status"] != "complete" {
		t.Fatalf("second complete_checkout should still commit the booking: %v", resp2)
	}
	settlement2, ok := resp2["circle_settlement"].(map[string]any)
	if !ok {
		t.Fatalf("expected circle_settlement on the second booking too, got %v", resp2)
	}
	if settlement2["error"] != "exceeds_circle_aggregate_cap" {
		t.Fatalf("expected exceeds_circle_aggregate_cap, got %v", settlement2)
	}
	if _, has := settlement2["transaction_id"]; has {
		t.Fatalf("cap-refused settlement must not carry a transaction_id: %v", settlement2)
	}
	// Locks in the deliberate asymmetry (§5): the agent-facing map carries no numbers.
	if _, has := settlement2["aggregate_cap_cents"]; has {
		t.Fatalf("agent-facing circle_settlement must not carry aggregate_cap_cents: %v", settlement2)
	}
	if _, has := settlement2["settled_cents"]; has {
		t.Fatalf("agent-facing circle_settlement must not carry settled_cents: %v", settlement2)
	}
	if atomic.LoadInt32(&mock.transferCalls) != 1 {
		t.Fatalf("expected exactly 1 real transfer call total, got %d", mock.transferCalls)
	}
}

// TestLoadCircleConfigAggregateCap asserts the full §3 semantics table:
// unset -> default; parsed dollars -> cents; unparseable -> default (never
// unbounded); zero -> kill switch; negative -> clamped to zero.
func TestLoadCircleConfigAggregateCap(t *testing.T) {
	cases := []struct {
		name   string
		setEnv bool
		envVal string
		want   int64
	}{
		{"unset -> built-in $10,000 default, NOT unbounded", false, "", 1000000},
		{"parsed dollars-and-cents", true, "250.50", 25050},
		{"unparseable falls back to the default, not unbounded", true, "abc", 1000000},
		{"zero is a kill switch", true, "0", 0},
		{"negative clamps to zero", true, "-5", 0},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			if c.setEnv {
				t.Setenv("CIRCLE_AGGREGATE_CAP_USD", c.envVal)
			} else {
				t.Setenv("CIRCLE_AGGREGATE_CAP_USD", "") // explicitly unset relative to outer env
			}
			cfg := loadCircleConfig()
			if cfg.AggregateCapCents != c.want {
				t.Fatalf("AggregateCapCents = %d, want %d", cfg.AggregateCapCents, c.want)
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
