package main

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

// ---------------------------------------------------------------------------
// Store-level wallet semantics (var-0 reset, debit/credit, guards).
// ---------------------------------------------------------------------------

func TestWalletFundResetsDeterministically(t *testing.T) {
	st := newStore()
	sess := "trip-deadbeef"

	w, _ := st.walletFund(sess, 500000, "", "")
	if w.BalanceCents != 500000 || w.SeedCents != 500000 || len(w.Ledger) != 0 {
		t.Fatalf("fund did not seed cleanly: %+v", w)
	}

	// Mutate via a debit, then re-fund: state must RESET (var-0 keystone — an
	// identical re-fund leaves byte-identical state regardless of prior activity).
	st.walletDebit(sess, "co_1", "BK-1", 54000, "")
	w2, _ := st.walletFund(sess, 500000, "", "")
	if w2.BalanceCents != 500000 || w2.nextSeq != 0 || len(w2.Ledger) != 0 || len(w2.seenCheckout) != 0 {
		t.Fatalf("re-fund did not reset wallet state: %+v", w2)
	}
}

func TestWalletDebitDecrementsExactlyAndLedger(t *testing.T) {
	st := newStore()
	sess := "trip-1"
	st.walletFund(sess, 500000, "", "")

	bal, ok, reason := st.walletDebit(sess, "co_1", "BK-1", 54000, "")
	if !ok || reason != "" {
		t.Fatalf("debit failed: ok=%v reason=%q", ok, reason)
	}
	if bal != 500000-54000 {
		t.Fatalf("debit decremented wrong: balance=%d want=%d", bal, 446000)
	}
	w, _ := st.walletGet(sess)
	if len(w.Ledger) != 1 {
		t.Fatalf("ledger len=%d want 1", len(w.Ledger))
	}
	e := w.Ledger[0]
	if e.Type != "debit" || e.AmountCents != 54000 || e.Seq != 0 || e.CheckoutID != "co_1" || e.BookingRef != "BK-1" {
		t.Fatalf("ledger entry wrong: %+v", e)
	}
}

func TestWalletDoubleDebitIsNoOp(t *testing.T) {
	st := newStore()
	sess := "trip-1"
	st.walletFund(sess, 500000, "", "")
	st.walletDebit(sess, "co_1", "BK-1", 54000, "")
	bal, ok, reason := st.walletDebit(sess, "co_1", "BK-1", 54000, "")
	if !ok || reason != "already_debited" {
		t.Fatalf("second debit should be a no-op: ok=%v reason=%q", ok, reason)
	}
	if bal != 446000 {
		t.Fatalf("double-debit changed balance: %d", bal)
	}
	w, _ := st.walletGet(sess)
	if len(w.Ledger) != 1 {
		t.Fatalf("double-debit added a ledger row: len=%d", len(w.Ledger))
	}
}

func TestWalletVoidCreditRestoresBalance(t *testing.T) {
	st := newStore()
	sess := "trip-1"
	st.walletFund(sess, 500000, "", "")
	st.walletDebit(sess, "co_1", "BK-1", 54000, "")

	bal, ok := st.walletCredit(sess, "co_1", "BK-1", 54000)
	if !ok || bal != 500000 {
		t.Fatalf("credit did not restore balance: ok=%v bal=%d", ok, bal)
	}
	// Double-cancel: no double-credit.
	bal2, ok2 := st.walletCredit(sess, "co_1", "BK-1", 54000)
	if ok2 {
		t.Fatalf("second credit should be a no-op (no double-refund), got ok=true bal=%d", bal2)
	}
	if bal2 != 500000 {
		t.Fatalf("double-credit changed balance: %d", bal2)
	}
}

func TestWalletCreditWithoutDebitIsNoOp(t *testing.T) {
	st := newStore()
	sess := "trip-1"
	st.walletFund(sess, 500000, "", "")
	bal, ok := st.walletCredit(sess, "co_x", "BK-x", 1000)
	if ok || bal != 500000 {
		t.Fatalf("credit without prior debit should be a no-op: ok=%v bal=%d", ok, bal)
	}
}

func TestWalletViewSimulatedLabel(t *testing.T) {
	st := newStore()
	w, _ := st.walletFund("s", 1000, "", "")
	v := walletView(w)
	if v["simulated"] != true {
		t.Fatalf("walletView missing simulated:true: %v", v)
	}
	if v["note"] == "" || v["seed_cents"].(int64) != 1000 || v["balance_cents"].(int64) != 1000 {
		t.Fatalf("walletView wrong: %v", v)
	}
}

// ---------------------------------------------------------------------------
// End-to-end through checkoutTool: the 402 vs 403 distinction is the crux.
// ---------------------------------------------------------------------------

// testCfg mirrors a small house cap so the budget/hard-max vetoes are reachable.
func walletTestCfg() config {
	return config{BudgetCeilingCents: 80000, BudgetHardMaxCents: 200000, DefaultTier: "L2"}
}

// findHotelPrice picks a real catalog hotel + a 1-night stay we can price.
// We reuse the merchant's own create_checkout to compute the total deterministically.
func createWalletCheckout(t *testing.T, st *store, cfg config, hotelID, walletSession string, budgetCents int64) (string, int64) {
	t.Helper()
	args, _ := json.Marshal(map[string]any{
		"checkout": map[string]any{
			"user_id":           "u1",
			"line_items":        []map[string]any{{"hotel_id": hotelID, "checkin": "2026-03-01", "checkout": "2026-03-02", "adults": 1}},
			"user_budget_cents": budgetCents,
			"wallet_session_id": walletSession,
		},
	})
	resp, code := checkoutTool(cfg, st, "L2", "", "create_checkout", args)
	if code != http.StatusOK {
		t.Fatalf("create_checkout failed HTTP %d: %v", code, resp)
	}
	id, _ := resp["id"].(string)
	total, _ := resp["total_cents"].(int64)
	if id == "" || total <= 0 {
		t.Fatalf("create_checkout bad result: %v", resp)
	}
	return id, total
}

// firstCatalogHotel returns a usable hotel id from the seeded catalog. It skips
// any hotel the sim might gate by picking the first with a positive nightly price.
func firstCatalogHotel(t *testing.T) string {
	t.Helper()
	for _, h := range mockCatalog {
		if h.PriceCentsNight > 0 {
			return h.ID
		}
	}
	t.Fatal("no catalog hotel available")
	return ""
}

func completeWalletCheckout(cfg config, st *store, checkoutID string) (map[string]any, int) {
	// VULN-003: consent must come from update_checkout (not complete body).
	updArgs, _ := json.Marshal(map[string]any{
		"checkout": map[string]any{"id": checkoutID, "buyer_consent": true},
	})
	checkoutTool(cfg, st, "L2", "", "update_checkout", updArgs)
	args, _ := json.Marshal(map[string]any{
		"checkout": map[string]any{"id": checkoutID},
	})
	return checkoutTool(cfg, st, "L2", "", "complete_checkout", args)
}

// INSUFFICIENT-FUNDS: fund < total < user_budget → HTTP 402, balance unchanged,
// session NOT completed.
func TestComplete402InsufficientFunds(t *testing.T) {
	st := newStore()
	cfg := walletTestCfg()
	hotel := firstCatalogHotel(t)
	sess := "trip-insufficient"

	// Create a checkout (no wallet yet) just to learn the exact total.
	probeID, total := createWalletCheckout(t, st, cfg, hotel, "", 100000000)

	// Fund the wallet BELOW the total, but set the budget ABOVE the total so the
	// 403 budget veto would NOT fire — only the 402 wallet gate should.
	fund := total - 1
	st.walletFund(sess, fund, "", "")
	cfg.BudgetCeilingCents = total + 100000
	cfg.BudgetHardMaxCents = total + 200000

	id, _ := createWalletCheckout(t, st, cfg, hotel, sess, total+100000)
	resp, code := completeWalletCheckout(cfg, st, id)
	if code != http.StatusPaymentRequired {
		t.Fatalf("expected HTTP 402, got %d: %v", code, resp)
	}
	if resp["reason"] != "insufficient_funds" || resp["status"] != "denied" {
		t.Fatalf("expected insufficient_funds/denied: %v", resp)
	}
	// Balance untouched, session not completed.
	w, _ := st.walletGet(sess)
	if w.BalanceCents != fund || len(w.Ledger) != 0 {
		t.Fatalf("rejected commit mutated wallet: bal=%d ledger=%d", w.BalanceCents, len(w.Ledger))
	}
	s := st.sessions[id]
	if s.Status == "complete" {
		t.Fatalf("session should NOT be complete after 402")
	}
	_ = probeID
}

// BUDGET-403 UNCHANGED: total > user_budget but total <= wallet → still
// 403 price_exceeds_budget (the wallet gate must NOT mask the budget veto).
func TestComplete403BudgetUnchangedWithAmpleWallet(t *testing.T) {
	st := newStore()
	cfg := walletTestCfg()
	hotel := firstCatalogHotel(t)
	sess := "trip-overbudget"

	_, total := createWalletCheckout(t, st, cfg, hotel, "", 100000000)

	// Wallet AMPLE (covers the total), budget set BELOW the total → 403 must fire.
	st.walletFund(sess, total+500000, "", "")
	cfg.BudgetHardMaxCents = total + 1000000 // hard-max not the binding constraint
	budget := total - 1                      // user budget below total → price_exceeds_budget

	id, _ := createWalletCheckout(t, st, cfg, hotel, sess, budget)
	resp, code := completeWalletCheckout(cfg, st, id)
	if code != http.StatusForbidden {
		t.Fatalf("expected HTTP 403, got %d: %v", code, resp)
	}
	if resp["reason"] != "price_exceeds_budget" {
		t.Fatalf("expected price_exceeds_budget, got: %v", resp)
	}
	// Wallet untouched (403 short-circuits before any debit).
	w, _ := st.walletGet(sess)
	if w.BalanceCents != total+500000 || len(w.Ledger) != 0 {
		t.Fatalf("403 path mutated wallet: %+v", w)
	}
}

// HAPPY PATH: debit decrements by exactly TotalCents; idempotent replay debits once.
func TestCompleteDebitsOnceWithIdempotentReplay(t *testing.T) {
	st := newStore()
	cfg := walletTestCfg()
	cfg.BudgetCeilingCents = 100000000
	cfg.BudgetHardMaxCents = 100000000
	hotel := firstCatalogHotel(t)
	sess := "trip-happy"
	st.walletFund(sess, 100000000, "", "")

	id, total := createWalletCheckout(t, st, cfg, hotel, sess, 100000000)

	// VULN-003: consent via update_checkout before complete.
	updArgs, _ := json.Marshal(map[string]any{
		"checkout": map[string]any{"id": id, "buyer_consent": true},
	})
	checkoutTool(cfg, st, "L2", "", "update_checkout", updArgs)
	args, _ := json.Marshal(map[string]any{
		"checkout": map[string]any{"id": id, "idempotency_key": "k1"},
	})
	resp, code := checkoutTool(cfg, st, "L2", "", "complete_checkout", args)
	if code != http.StatusOK || resp["status"] != "complete" {
		t.Fatalf("commit failed: HTTP %d %v", code, resp)
	}
	if resp["wallet_debit_cents"].(int64) != total {
		t.Fatalf("wallet_debit_cents=%v want %d", resp["wallet_debit_cents"], total)
	}
	if resp["wallet_balance_cents"].(int64) != 100000000-total {
		t.Fatalf("balance after debit=%v", resp["wallet_balance_cents"])
	}

	// Idempotent replay (same key) → same booking, NO second debit.
	resp2, code2 := checkoutTool(cfg, st, "L2", "", "complete_checkout", args)
	if code2 != http.StatusOK {
		t.Fatalf("replay HTTP %d", code2)
	}
	w, _ := st.walletGet(sess)
	if len(w.Ledger) != 1 {
		t.Fatalf("idempotent replay double-debited: ledger=%d", len(w.Ledger))
	}
	if w.BalanceCents != 100000000-total {
		t.Fatalf("replay changed balance: %d", w.BalanceCents)
	}
	if resp2["wallet_balance_cents"].(int64) != 100000000-total {
		t.Fatalf("replay echoed wrong balance: %v", resp2["wallet_balance_cents"])
	}
}

// VOID → CREDIT restores the balance through the full checkout path.
func TestCompleteThenCancelCreditsBack(t *testing.T) {
	st := newStore()
	cfg := walletTestCfg()
	cfg.BudgetCeilingCents = 100000000
	cfg.BudgetHardMaxCents = 100000000
	hotel := firstCatalogHotel(t)
	sess := "trip-void"
	st.walletFund(sess, 100000000, "", "")

	id, total := createWalletCheckout(t, st, cfg, hotel, sess, 100000000)
	if _, code := completeWalletCheckout(cfg, st, id); code != http.StatusOK {
		t.Fatalf("commit HTTP %d", code)
	}

	cargs, _ := json.Marshal(map[string]any{"checkout": map[string]any{"id": id}})
	resp, code := checkoutTool(cfg, st, "L2", "", "cancel_checkout", cargs)
	if code != http.StatusOK || resp["cancelled"] != true {
		t.Fatalf("cancel failed: HTTP %d %v", code, resp)
	}
	if resp["wallet_credit_cents"].(int64) != total {
		t.Fatalf("wallet_credit_cents=%v want %d", resp["wallet_credit_cents"], total)
	}
	w, _ := st.walletGet(sess)
	if w.BalanceCents != 100000000 {
		t.Fatalf("credit did not restore balance: %d", w.BalanceCents)
	}
	// Double-cancel: no double-credit.
	resp2, _ := checkoutTool(cfg, st, "L2", "", "cancel_checkout", cargs)
	if _, has := resp2["wallet_credit_cents"]; has {
		t.Fatalf("double-cancel double-credited: %v", resp2)
	}
	w2, _ := st.walletGet(sess)
	if w2.BalanceCents != 100000000 {
		t.Fatalf("double-cancel changed balance: %d", w2.BalanceCents)
	}
}

// Backward-compat: an empty wallet_session_id leaves complete/cancel responses
// free of any wallet_* keys (byte-identical to pre-wallet behaviour).
func TestEmptyWalletSessionByteIdentical(t *testing.T) {
	st := newStore()
	cfg := walletTestCfg()
	cfg.BudgetCeilingCents = 100000000
	cfg.BudgetHardMaxCents = 100000000
	hotel := firstCatalogHotel(t)

	id, _ := createWalletCheckout(t, st, cfg, hotel, "", 100000000)
	resp, _ := completeWalletCheckout(cfg, st, id)
	for k := range resp {
		if strings.HasPrefix(k, "wallet_") || k == "simulated" {
			t.Fatalf("empty-session response carried wallet key %q: %v", k, resp)
		}
	}
}

// Admin handler: GET summary + POST fund/reset, honestly labeled.
func TestWalletAdminHandler(t *testing.T) {
	st := newStore()
	h := walletSimAdminHandler(st)

	w := httptest.NewRecorder()
	h(w, httptest.NewRequest("POST", "/admin/sim/wallet",
		strings.NewReader(`{"wallet_session_id":"s1","wallet_balance_cents":500000}`)))
	if w.Code != 200 {
		t.Fatalf("fund HTTP %d", w.Code)
	}
	var resp map[string]any
	_ = json.Unmarshal(w.Body.Bytes(), &resp)
	if resp["simulated"] != true || resp["balance_cents"].(float64) != 500000 {
		t.Fatalf("fund response wrong: %v", resp)
	}

	w2 := httptest.NewRecorder()
	h(w2, httptest.NewRequest("GET", "/admin/sim/wallet", nil))
	var sum map[string]any
	_ = json.Unmarshal(w2.Body.Bytes(), &sum)
	if sum["count"].(float64) != 1 || sum["simulated"] != true {
		t.Fatalf("summary wrong: %v", sum)
	}

	w3 := httptest.NewRecorder()
	h(w3, httptest.NewRequest("POST", "/admin/sim/wallet", strings.NewReader(`{"wallet_session_id":""}`)))
	if w3.Code != 400 {
		t.Fatalf("bad-input HTTP %d (want 400)", w3.Code)
	}
}
