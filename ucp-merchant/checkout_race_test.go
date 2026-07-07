package main

// checkout_race_test.go — L3 (audit finding, ucp-merchant deep review): the
// checkout+wallet money path (complete_checkout's critical section, checkout.go,
// under st.mu) has NEVER been driven concurrently despite CI running
// `go test ./... -race`. A single-goroutine test suite can never trip the race
// detector, so a future regression that narrows the lock's scope (e.g. reading
// wlt.BalanceCents / s.Status outside st.mu, or moving the wallet debit to a
// separately-locked call after the session-status check) would ship silently.
//
// These tests spawn real concurrent goroutines against the SAME store/session/
// wallet and must be run with `-race` to be meaningful — the race detector
// instruments the shared wallet/session field accesses (BalanceCents, Ledger,
// seenCheckout, Status) and will flag any regression that lets two goroutines
// touch them outside a common critical section, even when the resulting
// counts/balances happen to look right on a given run.
//
// Style matches the existing harness (checkout_test.go / wallet_test.go /
// adversarial_money_test.go): reuses coArgs/li/firstCatalogHotel/
// createWalletCheckout/walletTestCfg, table-free focused tests.

import (
	"encoding/json"
	"net/http"
	"sync"
	"testing"
)

// TestCompleteCheckoutConcurrentDoubleSpend — N goroutines hammer
// complete_checkout on the SAME funded session concurrently. Exactly one may
// debit the wallet; every other caller must observe the post-commit
// "idempotent" replay path (checkout.go's s.Status=="complete" branch), never
// a second debit. A regression that checks s.Status and performs the debit as
// two separate critical sections (instead of one, under st.mu for the whole
// function) would let two goroutines both observe "incomplete" and both debit
// — this test's ledger/balance assertions catch that, and -race additionally
// catches the unsynchronized access itself.
func TestCompleteCheckoutConcurrentDoubleSpend(t *testing.T) {
	st := newStore()
	cfg := walletTestCfg()
	cfg.BudgetCeilingCents = 100000000
	cfg.BudgetHardMaxCents = 100000000
	hotel := firstCatalogHotel(t)
	sess := "trip-race-single"
	const seed = int64(100000000)
	st.walletFund(sess, seed, "", "")

	id, total := createWalletCheckout(t, st, cfg, hotel, sess, seed)
	checkoutTool(cfg, st, "L2", "", "update_checkout", coArgs(map[string]any{"id": id, "buyer_consent": true}))

	const n = 25
	var wg sync.WaitGroup
	results := make([]map[string]any, n)
	codes := make([]int, n)
	wg.Add(n)
	for i := 0; i < n; i++ {
		i := i
		go func() {
			defer wg.Done()
			args, _ := json.Marshal(map[string]any{"checkout": map[string]any{"id": id}})
			resp, code := checkoutTool(cfg, st, "L2", "", "complete_checkout", args)
			results[i] = resp
			codes[i] = code
		}()
	}
	wg.Wait()

	winners, idempotent := 0, 0
	for i, resp := range results {
		if codes[i] != http.StatusOK {
			t.Fatalf("concurrent complete_checkout call %d: want HTTP 200, got %d %v", i, codes[i], resp)
		}
		if resp["status"] != "complete" {
			t.Fatalf("concurrent complete_checkout call %d: want status=complete, got %v", i, resp)
		}
		if resp["idempotent"] == true {
			idempotent++
		} else {
			winners++
		}
	}
	if winners != 1 {
		t.Fatalf("want exactly 1 non-idempotent winner among %d concurrent completes, got %d (idempotent=%d)", n, winners, idempotent)
	}
	if idempotent != n-1 {
		t.Fatalf("want %d idempotent replays, got %d", n-1, idempotent)
	}

	w, ok := st.walletGet(sess)
	if !ok {
		t.Fatal("wallet vanished")
	}
	if len(w.Ledger) != 1 {
		t.Fatalf("concurrent double-spend guard failed: ledger has %d entries, want exactly 1", len(w.Ledger))
	}
	if w.Ledger[0].AmountCents != total {
		t.Fatalf("ledger entry amount=%d, want total=%d", w.Ledger[0].AmountCents, total)
	}
	if w.BalanceCents != seed-total {
		t.Fatalf("final balance=%d, want %d (seed-total, debited exactly once)", w.BalanceCents, seed-total)
	}
}

// TestCompleteVsCancelRace — N/2 goroutines call complete_checkout and N/2 call
// cancel_checkout concurrently on the SAME session+wallet. cancel_checkout is a
// one-way trapdoor (a cancelled checkout can never be re-completed — checkout.go
// "checkout_cancelled" guard), so no matter the interleaving the session must
// end up "cancelled", and the wallet must never end up net-debited: either no
// complete_checkout call won the race before the first cancel (ledger empty,
// balance==seed), or one did and the subsequent cancel refunded it (ledger has
// exactly one paired debit+credit, balance==seed again). A ledger with exactly
// one UNPAIRED debit (debited but never credited back) is the double-spend /
// stuck-charge failure mode this test exists to catch.
func TestCompleteVsCancelRace(t *testing.T) {
	st := newStore()
	cfg := walletTestCfg()
	cfg.BudgetCeilingCents = 100000000
	cfg.BudgetHardMaxCents = 100000000
	hotel := firstCatalogHotel(t)
	sess := "trip-race-complete-vs-cancel"
	const seed = int64(100000000)
	st.walletFund(sess, seed, "", "")

	id, _ := createWalletCheckout(t, st, cfg, hotel, sess, seed)
	checkoutTool(cfg, st, "L2", "", "update_checkout", coArgs(map[string]any{"id": id, "buyer_consent": true}))

	const halfN = 15
	var wg sync.WaitGroup
	wg.Add(2 * halfN)
	for i := 0; i < halfN; i++ {
		go func() {
			defer wg.Done()
			args, _ := json.Marshal(map[string]any{"checkout": map[string]any{"id": id}})
			checkoutTool(cfg, st, "L2", "", "complete_checkout", args)
		}()
		go func() {
			defer wg.Done()
			args, _ := json.Marshal(map[string]any{"checkout": map[string]any{"id": id}})
			checkoutTool(cfg, st, "L2", "", "cancel_checkout", args)
		}()
	}
	wg.Wait()

	s := st.sessions[id]
	if s.Status != "cancelled" {
		t.Fatalf("cancel is a one-way trapdoor: final status=%q, want cancelled", s.Status)
	}

	w, ok := st.walletGet(sess)
	if !ok {
		t.Fatal("wallet vanished")
	}
	switch len(w.Ledger) {
	case 0:
		// No complete_checkout call won the race before the first cancel fired.
	case 2:
		if w.Ledger[0].Type != "debit" || w.Ledger[1].Type != "credit" {
			t.Fatalf("2-entry ledger must be [debit, credit], got %+v", w.Ledger)
		}
		if w.Ledger[0].AmountCents != w.Ledger[1].AmountCents {
			t.Fatalf("debit/credit amount mismatch: %+v", w.Ledger)
		}
	default:
		t.Fatalf("ledger has %d entries (%+v) — want 0 (never debited) or 2 (paired debit+credit), never an unpaired charge", len(w.Ledger), w.Ledger)
	}
	if w.BalanceCents != seed {
		t.Fatalf("final balance=%d, want seed=%d restored (net-zero: either never debited, or debited+refunded)", w.BalanceCents, seed)
	}
}

// TestTwoSessionsSharingOneWalletRace — two DISTINCT checkout sessions (own
// checkout ids, so each gets its own walletDebitLocked seenCheckout entry) are
// bound to the SAME wallet_session_id, which is funded for exactly ONE of
// their totals. Many goroutines hammer complete_checkout for BOTH sessions
// concurrently. The store-wide st.mu must serialize the read-balance-then-debit
// sequence across sessions, not just within one — a regression that only
// guards a single session's seenCheckout entry (but lets two DIFFERENT
// checkout ids race the shared wallet's balance check-then-decrement) would
// let both sessions debit and drive the balance negative / over-draw the
// wallet. This test asserts that never happens.
func TestTwoSessionsSharingOneWalletRace(t *testing.T) {
	st := newStore()
	cfg := walletTestCfg()
	cfg.BudgetCeilingCents = 100000000
	cfg.BudgetHardMaxCents = 100000000
	hotel := firstCatalogHotel(t)
	sess := "trip-race-shared-wallet"

	// Learn the exact per-checkout total via a probe (no wallet bound), then
	// fund the shared wallet for exactly ONE total — never enough for both.
	_, total := createWalletCheckout(t, st, cfg, hotel, "", 100000000)
	st.walletFund(sess, total, "", "")

	idA, _ := createWalletCheckout(t, st, cfg, hotel, sess, 100000000)
	idB, _ := createWalletCheckout(t, st, cfg, hotel, sess, 100000000)
	checkoutTool(cfg, st, "L2", "", "update_checkout", coArgs(map[string]any{"id": idA, "buyer_consent": true}))
	checkoutTool(cfg, st, "L2", "", "update_checkout", coArgs(map[string]any{"id": idB, "buyer_consent": true}))

	const attemptsPerSession = 15
	var wg sync.WaitGroup
	wg.Add(2 * attemptsPerSession)
	for i := 0; i < attemptsPerSession; i++ {
		go func() {
			defer wg.Done()
			args, _ := json.Marshal(map[string]any{"checkout": map[string]any{"id": idA}})
			checkoutTool(cfg, st, "L2", "", "complete_checkout", args)
		}()
		go func() {
			defer wg.Done()
			args, _ := json.Marshal(map[string]any{"checkout": map[string]any{"id": idB}})
			checkoutTool(cfg, st, "L2", "", "complete_checkout", args)
		}()
	}
	wg.Wait()

	sA, sB := st.sessions[idA], st.sessions[idB]
	completeCount := 0
	if sA.Status == "complete" {
		completeCount++
	}
	if sB.Status == "complete" {
		completeCount++
	}
	if completeCount != 1 {
		t.Fatalf("shared wallet funded for exactly ONE total: want exactly 1 of 2 sessions complete, got %d (A=%s B=%s)", completeCount, sA.Status, sB.Status)
	}

	w, ok := st.walletGet(sess)
	if !ok {
		t.Fatal("wallet vanished")
	}
	if w.BalanceCents != 0 {
		t.Fatalf("shared-wallet balance=%d, want exactly 0 (drawn down by the single winner, never negative, never over-drawn by the loser)", w.BalanceCents)
	}
	if w.BalanceCents < 0 {
		t.Fatalf("shared-wallet balance went negative: %d — double-spend across two sessions", w.BalanceCents)
	}
	if len(w.Ledger) != 1 {
		t.Fatalf("shared wallet ledger has %d entries, want exactly 1 (only the single winning session may debit)", len(w.Ledger))
	}
}
