package main

// wallet_ownership_test.go — regression tests for the wallet-race audit
// (H1, M1, M2, L1):
//
//   H1  complete_checkout's wallet gate + walletDebitLocked now assert
//       END-USER ownership (OwnerUserID), not just OwnerAgentID — closing a
//       cross-user fund-drain: every end user of this orchestrator shares ONE
//       process-wide AgentID, so a same-agent attacker could previously bind
//       their OWN checkout session to a VICTIM's wallet_session_id
//       (create_checkout has no ownership check at bind time) and drain it.
//   M1  walletFund's prior-owner mismatch guard (wallet_owner_mismatch) —
//       previously 0% covered (every existing test used empty-string owners).
//   M2  complete_checkout's wallet_no_owner_signed_caller deny branch —
//       previously 0% covered.
//   L1  dispatchWalletGet/dispatchWalletFund now build their JSON snapshot
//       under the SAME st.mu critical section as the read/write, instead of
//       reading the live wallet (incl. its Ledger slice) after unlocking.

import (
	"encoding/json"
	"fmt"
	"net/http"
	"strings"
	"sync"
	"testing"
)

// ---------------------------------------------------------------------------
// H1 — cross-user wallet-drain veto (end-user ownership at complete_checkout).
// ---------------------------------------------------------------------------

// TestCompleteCheckoutWalletCrossUserDrainVeto is the H1 regression test. It
// mirrors TestCompleteCheckoutWalletNotOwnerVeto (cross-AGENT leak) but drives
// the dimension that was previously UNCHECKED: same agentA (every end user of
// this orchestrator shares one process-wide signing key), different end user.
func TestCompleteCheckoutWalletCrossUserDrainVeto(t *testing.T) {
	st := newStore()
	cfg := config{
		BudgetCeilingCents: 100000000, BudgetHardMaxCents: 200000000,
		// END-USER ownership check only fires under RequireSignatures (prod).
		RequireSignatures: true,
	}
	hotel := firstCatalogHotel(t)
	const sess = "trip-victim-userA-wallet"

	// Victim userA funds a wallet (ownerAgentID="agentA", ownerUserID="userA"),
	// amply funded so the veto is reached before any insufficient-funds gate.
	st.walletFund(sess, 100000000, "agentA", "userA")

	// Attacker userB — SAME agentA — creates ITS OWN checkout session but binds
	// it to victim userA's wallet_session_id (create_checkout performs no
	// ownership check on wallet_session_id at bind time; this is the exploit).
	createArgs, _ := json.Marshal(map[string]any{
		"checkout": map[string]any{
			"user_id":           "userB",
			"line_items":        []map[string]any{{"hotel_id": hotel, "checkin": "2026-03-01", "checkout": "2026-03-02", "adults": 1}},
			"user_budget_cents": int64(100000000),
			"wallet_session_id": sess, // <-- victim userA's wallet
		},
	})
	d, code := checkoutTool(cfg, st, "L2", "agentA", "create_checkout", createArgs)
	if code != http.StatusOK {
		t.Fatalf("attacker create_checkout setup failed: code=%d %v", code, d)
	}
	cid := d["id"].(string)

	// Consent via update_checkout (userB owns THIS session, so the session-
	// ownership checks pass — the exploit is entirely in the wallet binding).
	checkoutTool(cfg, st, "L2", "agentA", "update_checkout",
		coArgs(map[string]any{"id": cid, "user_id": "userB", "buyer_consent": true}))

	// Attacker userB completes → must be vetoed 403 wallet_not_owner.
	resp, code2 := checkoutTool(cfg, st, "L2", "agentA", "complete_checkout",
		coArgs(map[string]any{"id": cid, "user_id": "userB"}))
	if code2 != http.StatusForbidden {
		t.Fatalf("H1 cross-user wallet drain: want 403, got code=%d %v", code2, resp)
	}
	if resp["status"] != "denied" || resp["reason"] != "wallet_not_owner" {
		t.Fatalf("H1 cross-user wallet drain: want denied/wallet_not_owner, got %v", resp)
	}
	// Victim's wallet must be UNTOUCHED (no debit, no ledger row) and the
	// attacker's session must NOT be booked.
	w, _ := st.walletGet(sess)
	if w.BalanceCents != 100000000 || len(w.Ledger) != 0 {
		t.Fatalf("H1 vetoed commit mutated victim wallet: bal=%d ledger=%d", w.BalanceCents, len(w.Ledger))
	}
	if s := st.sessions[cid]; s.Status == "complete" || s.BookingRef != "" {
		t.Fatalf("H1 vetoed commit must NOT book: status=%s ref=%q", s.Status, s.BookingRef)
	}

	// Positive control: the LEGITIMATE owner (userA) completing against their
	// OWN wallet still succeeds normally (the fix must not fail-closed on the
	// honest path).
	createArgsOK, _ := json.Marshal(map[string]any{
		"checkout": map[string]any{
			"user_id":           "userA",
			"line_items":        []map[string]any{{"hotel_id": hotel, "checkin": "2026-03-01", "checkout": "2026-03-02", "adults": 1}},
			"user_budget_cents": int64(100000000),
			"wallet_session_id": sess,
		},
	})
	dOK, codeOK := checkoutTool(cfg, st, "L2", "agentA", "create_checkout", createArgsOK)
	if codeOK != http.StatusOK {
		t.Fatalf("victim create_checkout setup failed: code=%d %v", codeOK, dOK)
	}
	cidOK := dOK["id"].(string)
	checkoutTool(cfg, st, "L2", "agentA", "update_checkout",
		coArgs(map[string]any{"id": cidOK, "user_id": "userA", "buyer_consent": true}))
	respOK, codeOK2 := checkoutTool(cfg, st, "L2", "agentA", "complete_checkout",
		coArgs(map[string]any{"id": cidOK, "user_id": "userA"}))
	if codeOK2 != http.StatusOK || respOK["status"] != "complete" {
		t.Fatalf("legitimate owner completion should succeed: code=%d %v", codeOK2, respOK)
	}
	w2, _ := st.walletGet(sess)
	if w2.BalanceCents != 100000000-respOK["total_cents"].(int64) {
		t.Fatalf("legitimate debit did not apply: %+v", w2)
	}
}

// TestWalletDebitLockedRejectsOwnerMismatch drives the H1 fix at the debit
// PRIMITIVE itself (walletDebitLocked/walletDebit), independent of the
// complete_checkout call site — defense in depth: even a future/direct caller
// of walletDebit cannot drain a wallet bound to a different end user.
func TestWalletDebitLockedRejectsOwnerMismatch(t *testing.T) {
	st := newStore()
	sess := "trip-debit-owner-check"
	st.walletFund(sess, 500000, "agentA", "userA")

	bal, ok, reason := st.walletDebit(sess, "co_x", "BK-x", 1000, "userB")
	if ok || reason != "wallet_not_owner" {
		t.Fatalf("debit primitive must refuse owner mismatch: ok=%v reason=%q", ok, reason)
	}
	if bal != 500000 {
		t.Fatalf("balance changed on a refused debit: %d", bal)
	}
	w, _ := st.walletGet(sess)
	if len(w.Ledger) != 0 {
		t.Fatalf("refused debit added a ledger row: %+v", w.Ledger)
	}

	// The correct owner succeeds normally.
	bal2, ok2, reason2 := st.walletDebit(sess, "co_x", "BK-x", 1000, "userA")
	if !ok2 || reason2 != "" || bal2 != 499000 {
		t.Fatalf("correct-owner debit should succeed: ok=%v reason=%q bal=%d", ok2, reason2, bal2)
	}

	// An empty expectedOwnerUserID (admin/legacy no-binding callers) always
	// skips the check, matching the "" == "" convention used everywhere else.
	bal3, ok3, reason3 := st.walletDebit(sess, "co_y", "BK-y", 500, "")
	if !ok3 || reason3 != "" || bal3 != 498500 {
		t.Fatalf("expectedOwnerUserID=='' should skip the ownership check: ok=%v reason=%q bal=%d", ok3, reason3, bal3)
	}
}

// TestWalletDebitLockedFailsOpenOnOwnerlessUserWallet pins the OTHER half of
// the H1 fail-open documented on walletDebitLocked (see wallet.go's "TRUST
// BOUNDARY" comment, flagged by a 2026-07-04 review as informational):
// when the WALLET itself has no recorded end-user owner (w.OwnerUserID==""),
// e.g. a legacy pre-#161 wallet funded before end-user threading existed, the
// debit primitive skips the ownership check even though the CALLER's claimed
// expectedOwnerUserID is non-empty. This is intentional (mirrors "" =="" on
// either side) and relies entirely on the upstream orchestrator never
// forwarding a client-controlled, unverified UserID — it is NOT something
// this primitive can safely tighten on its own (it cannot distinguish a
// genuinely ownerless/legacy wallet from an attacker-stripped one). This test
// exists so a future change to this fail-open is a deliberate, reviewed
// decision rather than an accidental regression.
func TestWalletDebitLockedFailsOpenOnOwnerlessUserWallet(t *testing.T) {
	st := newStore()
	sess := "trip-debit-ownerless-user-wallet"
	// Agent-owned (agentA) but NOT user-owned — the legacy/partial-binding case.
	st.walletFund(sess, 500000, "agentA", "")

	bal, ok, reason := st.walletDebit(sess, "co_z", "BK-z", 1000, "userX")
	if !ok || reason != "" || bal != 499000 {
		t.Fatalf("documented fail-open: a wallet with w.OwnerUserID=='' must accept ANY expectedOwnerUserID (upstream trust boundary), got ok=%v reason=%q bal=%d", ok, reason, bal)
	}
}

// ---------------------------------------------------------------------------
// M1 — walletFund's prior-owner mismatch guard (previously 0% covered).
// ---------------------------------------------------------------------------

func TestWalletFundOwnerMismatchAgent(t *testing.T) {
	st := newStore()
	sess := "trip-fund-mismatch-agent"
	if _, err := st.walletFund(sess, 100000, "agentA", ""); err != nil {
		t.Fatalf("initial fund failed: %v", err)
	}
	// A DIFFERENT agent attempting to reset/overwrite must be rejected.
	if _, err := st.walletFund(sess, 999999, "agentB", ""); err == nil {
		t.Fatalf("expected wallet_owner_mismatch for a cross-agent re-fund")
	} else if !strings.Contains(err.Error(), "wallet_owner_mismatch") {
		t.Fatalf("expected wallet_owner_mismatch, got: %v", err)
	}
	// Balance/ledger/owner must be UNCHANGED by the rejected re-fund.
	w, _ := st.walletGet(sess)
	if w.BalanceCents != 100000 || w.OwnerAgentID != "agentA" {
		t.Fatalf("rejected re-fund mutated wallet state: %+v", w)
	}
	// The TRUE owner resetting its own wallet still works (create-OR-RESET).
	w2, err2 := st.walletFund(sess, 42, "agentA", "")
	if err2 != nil || w2.BalanceCents != 42 {
		t.Fatalf("legit re-fund by the true owner should succeed: %+v err=%v", w2, err2)
	}
}

func TestWalletFundOwnerMismatchUser(t *testing.T) {
	st := newStore()
	sess := "trip-fund-mismatch-user"
	if _, err := st.walletFund(sess, 100000, "agentA", "userA"); err != nil {
		t.Fatalf("initial fund failed: %v", err)
	}
	// SAME agent, but a DIFFERENT end user attempting to reset — must still be
	// rejected (the OwnerUserID guard is independent of the OwnerAgentID one).
	if _, err := st.walletFund(sess, 999999, "agentA", "userB"); err == nil {
		t.Fatalf("expected wallet_owner_mismatch for a cross-user re-fund")
	} else if !strings.Contains(err.Error(), "wallet_owner_mismatch") {
		t.Fatalf("expected wallet_owner_mismatch, got: %v", err)
	}
	w, _ := st.walletGet(sess)
	if w.BalanceCents != 100000 || w.OwnerUserID != "userA" {
		t.Fatalf("rejected re-fund mutated wallet state: %+v", w)
	}
	// The TRUE owner (same agent + same user) resetting still works.
	w2, err2 := st.walletFund(sess, 555555, "agentA", "userA")
	if err2 != nil || w2.BalanceCents != 555555 {
		t.Fatalf("legit re-fund by the true owner should succeed: %+v err=%v", w2, err2)
	}
}

// ---------------------------------------------------------------------------
// M2 — complete_checkout's wallet_no_owner_signed_caller deny branch
// (previously 0% covered).
// ---------------------------------------------------------------------------

// TestCompleteCheckoutNoOwnerSignedCallerVeto funds an OWNERLESS wallet (as
// the admin/unsigned demo path does) and drives the branch that refuses a
// SIGNED caller from claiming it, then confirms the unsigned path is
// unaffected (positive control).
func TestCompleteCheckoutNoOwnerSignedCallerVeto(t *testing.T) {
	st := newStore()
	cfg := config{
		BudgetCeilingCents: 100000000, BudgetHardMaxCents: 200000000,
		RequireSignatures: true,
	}
	hotel := firstCatalogHotel(t)
	const sess = "trip-no-owner-signed"

	// Fund an ownerless wallet (ownerAgentID=="" and ownerUserID=="").
	if _, err := st.walletFund(sess, 100000000, "", ""); err != nil {
		t.Fatalf("walletFund setup failed: %v", err)
	}

	newCheckoutArgs := func() json.RawMessage {
		b, _ := json.Marshal(map[string]any{
			"checkout": map[string]any{
				"user_id":           "u1",
				"line_items":        []map[string]any{{"hotel_id": hotel, "checkin": "2026-03-01", "checkout": "2026-03-02", "adults": 1}},
				"user_budget_cents": int64(100000000),
				"wallet_session_id": sess,
			},
		})
		return b
	}

	// A SIGNED agent (agentA) creates + consents its own checkout against the
	// ownerless wallet.
	d, code := checkoutTool(cfg, st, "L2", "agentA", "create_checkout", newCheckoutArgs())
	if code != http.StatusOK {
		t.Fatalf("signed create_checkout setup failed: code=%d %v", code, d)
	}
	cid := d["id"].(string)
	checkoutTool(cfg, st, "L2", "agentA", "update_checkout",
		coArgs(map[string]any{"id": cid, "user_id": "u1", "buyer_consent": true}))

	resp, code2 := checkoutTool(cfg, st, "L2", "agentA", "complete_checkout",
		coArgs(map[string]any{"id": cid, "user_id": "u1"}))
	if code2 != http.StatusForbidden {
		t.Fatalf("signed caller on an ownerless wallet must be 403, got code=%d %v", code2, resp)
	}
	if resp["status"] != "denied" || resp["reason"] != "wallet_no_owner_signed_caller" {
		t.Fatalf("expected denied/wallet_no_owner_signed_caller, got %v", resp)
	}
	// Wallet + session must be untouched — NO debit.
	w, _ := st.walletGet(sess)
	if w.BalanceCents != 100000000 || len(w.Ledger) != 0 {
		t.Fatalf("vetoed commit mutated wallet: bal=%d ledger=%d", w.BalanceCents, len(w.Ledger))
	}
	if s := st.sessions[cid]; s.Status == "complete" {
		t.Fatalf("vetoed commit must NOT book: status=%s", s.Status)
	}

	// Positive control: an UNSIGNED caller (agentID=="") on the SAME ownerless
	// wallet succeeds normally — this is the intended demo/CI path and must
	// remain byte-identical.
	d2, code3 := checkoutTool(cfg, st, "L2", "", "create_checkout", newCheckoutArgs())
	if code3 != http.StatusOK {
		t.Fatalf("unsigned create_checkout setup failed: code=%d %v", code3, d2)
	}
	cid2 := d2["id"].(string)
	checkoutTool(cfg, st, "L2", "", "update_checkout",
		coArgs(map[string]any{"id": cid2, "user_id": "u1", "buyer_consent": true}))
	resp2, code4 := checkoutTool(cfg, st, "L2", "", "complete_checkout",
		coArgs(map[string]any{"id": cid2, "user_id": "u1"}))
	if code4 != http.StatusOK || resp2["status"] != "complete" {
		t.Fatalf("unsigned caller on an ownerless wallet should succeed: code=%d %v", code4, resp2)
	}
}

// ---------------------------------------------------------------------------
// L1 — dispatchWalletGet/dispatchWalletFund read their JSON snapshot under
// the SAME st.mu critical section as the underlying read/write (no more
// unlocked reads of the live wallet racing a concurrent debit/credit).
// ---------------------------------------------------------------------------

// TestWalletGetViewRaceSafeUnderConcurrentDebit hammers dispatchWalletGet
// concurrently with walletDebit on the SAME session. Before the L1 fix this
// raced (walletGet unlocked -> walletView(w) unlocked, reading w.Ledger while
// walletDebitLocked concurrently appended to it); run with `-race` to confirm
// the fix. This test does not assert a specific balance (debits interleave
// non-deterministically) — it only requires no data race and no panic.
func TestWalletGetViewRaceSafeUnderConcurrentDebit(t *testing.T) {
	st := newStore()
	const sess = "trip-race-l1"
	st.walletFund(sess, 100000000, "", "")
	cfg := config{}

	var wg sync.WaitGroup
	stop := make(chan struct{})
	wg.Add(1)
	go func() {
		defer wg.Done()
		for i := 0; ; i++ {
			select {
			case <-stop:
				return
			default:
			}
			st.walletDebit(sess, fmt.Sprintf("co_%d", i), "", 1, "")
		}
	}()

	args, _ := json.Marshal(map[string]any{"wallet_session_id": sess})
	for i := 0; i < 500; i++ {
		if _, code := dispatchWalletGet(st, cfg, "", toolCallParams{Name: "wallet_get", Arguments: args}); code != http.StatusOK {
			t.Fatalf("unexpected wallet_get failure mid-race: code=%d", code)
		}
	}
	close(stop)
	wg.Wait()
}

// TestWalletFundViewRaceSafeUnderConcurrentDebit mirrors the above for
// dispatchWalletFund: fund/reset a DIFFERENT session repeatedly while a
// concurrent debit mutates yet another wallet, plus a concurrent debit on the
// FRESHLY-funded session itself, to exercise walletFundView's locked snapshot.
func TestWalletFundViewRaceSafeUnderConcurrentDebit(t *testing.T) {
	st := newStore()
	const sess = "trip-race-l1-fund"

	var wg sync.WaitGroup
	stop := make(chan struct{})
	wg.Add(1)
	go func() {
		defer wg.Done()
		for i := 0; ; i++ {
			select {
			case <-stop:
				return
			default:
			}
			st.walletDebit(sess, fmt.Sprintf("co_%d", i), "", 1, "")
		}
	}()

	args, _ := json.Marshal(map[string]any{"wallet_session_id": sess, "wallet_balance_cents": int64(100000000)})
	for i := 0; i < 500; i++ {
		if _, code := dispatchWalletFund(st, "", toolCallParams{Name: "wallet_fund", Arguments: args}); code != http.StatusOK {
			t.Fatalf("unexpected wallet_fund failure mid-race: code=%d", code)
		}
	}
	close(stop)
	wg.Wait()
}
