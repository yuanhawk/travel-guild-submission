package main

// wallet.go — SIMULATED prepaid wallet (settlement-layer state).
//
// A demo/sim prepaid balance the agent draws down when a checkout commits and
// is refunded when a booking is voided. It is the additive insufficient-funds
// (HTTP 402) gate that sits ALONGSIDE — never replacing — the existing
// price_exceeds_budget (HTTP 403) merchant veto.
//
// HONESTLY LABELED: every wallet response carries "simulated": true + a note
// mirroring alipaySimNote — this is settlement-layer state seeded from the
// caller-supplied balance, NOT a real money rail.
//
// var-0 KEYSTONE: wallets are keyed by a caller-supplied wallet_session_id (the
// orchestrator passes the deterministic per-run request digest / idempotency_key).
// walletFund is create-OR-RESET (overwrite balance←seed, clear ledger, reset
// seenCheckout/nextSeq) — idempotent-by-overwrite. Identical input → identical
// key → identical reset → identical ledger on replay. NO wall-clock / random in
// wallet state; ledger ordering is by the monotonic Seq counter, never a
// timestamp. Any display timestamp is added CLIENT-side only.

import (
	"encoding/json"
	"errors"
	"io"
	"net/http"
	"sort"
)

const walletSimNote = "SIMULATED prepaid wallet — settlement-layer state seeded from the input balance; no real money moves (see alipay_sim.go for the simulated settlement rail this mirrors)."

// walletEntry is one immutable ledger line. AmountCents is a POSITIVE magnitude;
// Type ("debit"|"credit") gives the sign. Seq is the monotonic order key.
type walletEntry struct {
	CheckoutID  string `json:"checkout_id"`
	BookingRef  string `json:"booking_ref,omitempty"`
	AmountCents int64  `json:"amount_cents"`
	Type        string `json:"type"` // "debit" | "credit"
	Seq         int    `json:"seq"`
}

// wallet is the per-session SIMULATED prepaid balance + its append-only ledger.
// seenCheckout guards a checkout against a double-debit / double-refund:
//
//	checkout_id -> "debit"  (drawn down; a re-debit is a no-op)
//	checkout_id -> "credit" (already refunded; a re-credit is a no-op)
//
// OwnerAgentID is the RFC 9421 profileURL of the agent that funded this wallet
// (set at walletFund time). When non-empty and RequireSignatures (prod), only
// the owner agent may debit or read the wallet (WALLET-001 ownership binding).
// Unsigned demo (agentID=="" → owner="") ≡ no binding (no-op in demo).
//
// OwnerUserID is the end-user ownership dimension (task #161), mirroring
// checkoutSession.UserID: every end user of this orchestrator shares the SAME
// OwnerAgentID, so AgentID alone cannot distinguish user A's wallet from user
// B's. When non-empty and RequireSignatures (prod), wallet_get is also gated
// on the caller's claimed user_id matching OwnerUserID.
type wallet struct {
	SeedCents    int64
	BalanceCents int64
	Ledger       []walletEntry
	OwnerAgentID string // WALLET-001: bound owner (empty = no binding)
	OwnerUserID  string // end-user ownership (task #161; empty = no binding)
	seenCheckout map[string]string
	nextSeq      int
}

// walletFund is create-OR-RESET (the var-0 keystone): it overwrites any existing
// wallet for sessionID with a fresh one seeded at seedCents (balance←seed, empty
// ledger, cleared seenCheckout, nextSeq←0). Idempotent-by-overwrite: an identical
// re-fund leaves byte-identical state. Takes st.mu.
//
// ownerAgentID binds the wallet to the calling agent's RFC 9421 profileURL
// (WALLET-001). Pass "" for admin/no-binding paths (demo + admin handler).
//
// ownerUserID binds the wallet to the end-user id the orchestrator verified
// (task #161), mirroring the checkoutSession.UserID binding. Pass "" for
// admin/no-binding paths (demo + admin handler) or legacy anon callers.
//
// WALLET-FUND-NO-OWNERSHIP-CHECK fix: if the wallet already exists and is bound
// to a different agent, the call is rejected with wallet_owner_mismatch.
// When ownerAgentID=="" (unsigned / demo callers) the check passes unconditionally
// because existing.OwnerAgentID=="" == "" satisfies the equality. Same rule now
// applies to ownerUserID.
// walletFundLocked is walletFund's core; assumes st.mu is already held.
func (st *store) walletFundLocked(sessionID string, seedCents int64, ownerAgentID string, ownerUserID string) (*wallet, error) {
	// Prior-owner check: refuse to overwrite another agent's or user's wallet.
	if existing, exists := st.wallets[sessionID]; exists {
		if existing.OwnerAgentID != "" && existing.OwnerAgentID != ownerAgentID {
			return nil, errors.New("wallet_owner_mismatch: cannot reset another agent's wallet")
		}
		if existing.OwnerUserID != "" && existing.OwnerUserID != ownerUserID {
			return nil, errors.New("wallet_owner_mismatch: cannot reset another user's wallet")
		}
	}
	w := &wallet{
		SeedCents:    seedCents,
		BalanceCents: seedCents,
		Ledger:       []walletEntry{},
		OwnerAgentID: ownerAgentID,
		OwnerUserID:  ownerUserID,
		seenCheckout: map[string]string{},
		nextSeq:      0,
	}
	st.wallets[sessionID] = w
	return w, nil
}

func (st *store) walletFund(sessionID string, seedCents int64, ownerAgentID string, ownerUserID string) (*wallet, error) {
	st.mu.Lock()
	defer st.mu.Unlock()
	return st.walletFundLocked(sessionID, seedCents, ownerAgentID, ownerUserID)
}

// walletFundView is walletFund + an immediately-built JSON snapshot, taken
// under the SAME st.mu critical section (L1 fix). Without this, a caller doing
// `w, _ := st.walletFund(...); v := walletView(w)` reads w's fields (including
// the live Ledger slice) AFTER the lock is released, racing a concurrent
// debit/credit on the same session. Used by the unlocked-JSON-response call
// sites (dispatchWalletFund, the admin handler).
func (st *store) walletFundView(sessionID string, seedCents int64, ownerAgentID string, ownerUserID string) (map[string]any, error) {
	st.mu.Lock()
	defer st.mu.Unlock()
	w, err := st.walletFundLocked(sessionID, seedCents, ownerAgentID, ownerUserID)
	if err != nil {
		return nil, err
	}
	return walletView(w), nil
}

// walletGetLocked reads a wallet WITHOUT taking st.mu (for callers that already
// hold it — complete/cancel_checkout). walletGet is the locking variant.
func (st *store) walletGetLocked(sessionID string) (*wallet, bool) {
	w, ok := st.wallets[sessionID]
	return w, ok
}

func (st *store) walletGet(sessionID string) (*wallet, bool) {
	st.mu.Lock()
	defer st.mu.Unlock()
	return st.walletGetLocked(sessionID)
}

// walletDebitLocked draws `amount` down from the wallet. It assumes st.mu is held
// (used inside complete_checkout).
//
// H1 fix: expectedOwnerUserID is the end-user id the caller has already
// authenticated as (the completing session's UserID). When both it and the
// wallet's OwnerUserID are non-empty and they differ, the debit is refused
// with "wallet_not_owner" — this makes the debit PRIMITIVE itself enforce the
// end-user ownership binding (mirroring the OwnerAgentID gate already in
// complete_checkout), not just the checkout.go call site, so no future/direct
// caller of this function can accidentally bypass it. Pass "" to skip the
// check (admin/legacy/no-binding paths).
//
// TRUST BOUNDARY (security review, 2026-07-04, informational/non-blocking): this
// fails open whenever expectedOwnerUserID=="" OR w.OwnerUserID=="" — by
// design (task #161), matching every other AgentID/UserID check in this
// package. Safety depends on the Python orchestrator injecting a
// session-token-verified, non-empty ownerUserID on the wallet-funding call
// AND a non-empty expectedOwnerUserID on every completing session before
// reaching this primitive. If either arrives empty because a caller stripped
// it rather than because the flow is genuinely anonymous, this check cannot
// tell the difference — that is an upstream responsibility, not something
// fixable at this layer alone.
//
// Returns (balance, ok, reason):
//   - wallet missing                     → (0, false, "wallet_not_found")
//   - end-user owner mismatch            → (balance, false, "wallet_not_owner")
//   - already debited for this checkout  → (balance, true, "already_debited") NO-OP
//   - amount > balance                   → (balance, false, "insufficient_funds")
//   - success                            → (newBalance, true, "")
func (st *store) walletDebitLocked(sessionID, checkoutID, bookingRef string, amount int64, expectedOwnerUserID string) (int64, bool, string) {
	w, exists := st.wallets[sessionID]
	if !exists {
		return 0, false, "wallet_not_found"
	}
	// H1: end-user ownership assertion at the debit primitive itself.
	if expectedOwnerUserID != "" && w.OwnerUserID != "" && w.OwnerUserID != expectedOwnerUserID {
		return w.BalanceCents, false, "wallet_not_owner"
	}
	// Double-debit guard: a checkout already drawn down is a no-op (idempotent
	// replay must NOT debit twice).
	if w.seenCheckout[checkoutID] == "debit" {
		return w.BalanceCents, true, "already_debited"
	}
	if amount > w.BalanceCents {
		return w.BalanceCents, false, "insufficient_funds"
	}
	w.BalanceCents -= amount
	w.Ledger = append(w.Ledger, walletEntry{
		CheckoutID: checkoutID, BookingRef: bookingRef,
		AmountCents: amount, Type: "debit", Seq: w.nextSeq,
	})
	w.nextSeq++
	w.seenCheckout[checkoutID] = "debit"
	return w.BalanceCents, true, ""
}

func (st *store) walletDebit(sessionID, checkoutID, bookingRef string, amount int64, expectedOwnerUserID string) (int64, bool, string) {
	st.mu.Lock()
	defer st.mu.Unlock()
	return st.walletDebitLocked(sessionID, checkoutID, bookingRef, amount, expectedOwnerUserID)
}

// walletCreditLocked refunds `amount` IFF a matching un-credited debit exists for
// this checkout (seenCheckout[checkoutID]=="debit"). Assumes st.mu is held (used
// inside cancel_checkout). Returns (balance, ok). A credit with no prior debit, or
// a second credit, is a no-op (no double-refund). After crediting the checkout is
// marked "credit".
func (st *store) walletCreditLocked(sessionID, checkoutID, bookingRef string, amount int64) (int64, bool) {
	w, exists := st.wallets[sessionID]
	if !exists {
		return 0, false
	}
	if w.seenCheckout[checkoutID] != "debit" {
		// No un-credited debit to reverse (never debited, or already credited).
		bal := w.BalanceCents
		return bal, false
	}
	w.BalanceCents += amount
	w.Ledger = append(w.Ledger, walletEntry{
		CheckoutID: checkoutID, BookingRef: bookingRef,
		AmountCents: amount, Type: "credit", Seq: w.nextSeq,
	})
	w.nextSeq++
	w.seenCheckout[checkoutID] = "credit"
	return w.BalanceCents, true
}

func (st *store) walletCredit(sessionID, checkoutID, bookingRef string, amount int64) (int64, bool) {
	st.mu.Lock()
	defer st.mu.Unlock()
	return st.walletCreditLocked(sessionID, checkoutID, bookingRef, amount)
}

// walletView is the honestly-labeled JSON projection of a wallet. L1 fix: the
// Ledger slice is COPIED (never the live w.Ledger backing array) so the
// returned map has no aliasing with the wallet once released. This function
// must only be called while st.mu is held (mirrors the existing correct
// pattern in walletSummary) — copying alone does not make an unlocked read of
// w's fields race-free, since the read of the slice header itself would still
// race a concurrent walletDebitLocked/walletCreditLocked write. Callers that
// need a snapshot to use AFTER releasing the lock should go through
// walletFundView / walletGetChecked instead of calling walletFund/walletGet
// followed by an unlocked walletView.
func walletView(w *wallet) map[string]any {
	ledger := append([]walletEntry(nil), w.Ledger...)
	if ledger == nil {
		ledger = []walletEntry{}
	}
	return map[string]any{
		"seed_cents":    w.SeedCents,
		"balance_cents": w.BalanceCents,
		"ledger":        ledger,
		"simulated":     true,
		"note":          walletSimNote,
	}
}

// walletSummary returns every wallet view keyed by session id, in deterministic
// (sorted) order for a stable admin/GET response.
func (st *store) walletSummary() []map[string]any {
	st.mu.Lock()
	defer st.mu.Unlock()
	ids := make([]string, 0, len(st.wallets))
	for id := range st.wallets {
		ids = append(ids, id)
	}
	sort.Strings(ids)
	out := make([]map[string]any, 0, len(ids))
	for _, id := range ids {
		v := walletView(st.wallets[id])
		v["wallet_session_id"] = id
		out = append(out, v)
	}
	return out
}

// dispatchWalletFund serves the wallet_fund MCP tool (sim/control — dev.ucp.sim.control
// capability required). Create-OR-RESET a wallet for {wallet_session_id} at
// {wallet_balance_cents}. agentID binds wallet ownership (WALLET-001); the
// caller-supplied user_id binds the end-user ownership dimension (task #161).
func dispatchWalletFund(st *store, agentID string, p toolCallParams) (map[string]any, int) {
	var a struct {
		WalletSessionID    string `json:"wallet_session_id"`
		WalletBalanceCents int64  `json:"wallet_balance_cents"`
		UserID             string `json:"user_id"`
	}
	_ = json.Unmarshal(p.Arguments, &a)
	if a.WalletSessionID == "" {
		return map[string]any{"error": "wallet_session_id required"}, http.StatusBadRequest
	}
	if a.WalletBalanceCents < 0 {
		return map[string]any{"error": "wallet_balance_cents must be >= 0"}, http.StatusBadRequest
	}
	// WALLET-001: bind the wallet to the funding agent's identity. Admin/unsigned
	// paths pass agentID="" → no binding (demo-safe). Task #161: bind the wallet
	// to the end-user identity too (a.UserID=="" → no binding, e.g. legacy anon).
	// L1 fix: walletFundView builds the JSON snapshot under the SAME lock as the
	// fund itself, so no unlocked read of the wallet races a concurrent debit.
	v, err := st.walletFundView(a.WalletSessionID, a.WalletBalanceCents, agentID, a.UserID)
	if err != nil {
		return map[string]any{"error": err.Error(), "status": "denied"}, http.StatusForbidden
	}
	v["wallet_session_id"] = a.WalletSessionID
	v["status"] = "ok"
	return v, http.StatusOK
}

// walletGetChecked performs the wallet_get lookup, the WALLET-001/end-user
// ownership checks, AND the JSON-view build all under a single st.mu critical
// section (L1 fix — see walletView's doc comment: reading w's fields, notably
// the live Ledger slice, after the lock is released would race a concurrent
// debit/credit on the same session). Returns:
//   - found=false                      → wallet does not exist
//   - found=true, deniedReason!=""      → ownership veto (view is nil)
//   - found=true, deniedReason==""      → view is the JSON snapshot
func (st *store) walletGetChecked(sessionID string, cfg config, agentID string, userID string) (view map[string]any, found bool, deniedReason string) {
	st.mu.Lock()
	defer st.mu.Unlock()
	w, ok := st.walletGetLocked(sessionID)
	if !ok {
		return nil, false, ""
	}
	// WALLET-001: ownership check — prod only (RequireSignatures gate).
	// Unsigned demo (agentID=="" and owner=="") satisfies the equality.
	if cfg.RequireSignatures && w.OwnerAgentID != "" && w.OwnerAgentID != agentID {
		return nil, true, "wallet_not_owner"
	}
	// END-USER OWNERSHIP (task #161) — prod only (RequireSignatures gate).
	// Mirrors the AgentID check above but on the end-user dimension: every end
	// user of this orchestrator shares the SAME AgentID, so AgentID alone
	// cannot isolate user A's wallet from user B's.
	if cfg.RequireSignatures && w.OwnerUserID != "" && w.OwnerUserID != userID {
		return nil, true, "wallet_not_owner"
	}
	return walletView(w), true, ""
}

// dispatchWalletGet serves the wallet_get MCP tool (read-only).
// cfg and agentID are used for the WALLET-001 ownership check in prod; the
// caller-supplied user_id is used for the end-user ownership check (task #161).
func dispatchWalletGet(st *store, cfg config, agentID string, p toolCallParams) (map[string]any, int) {
	var a struct {
		WalletSessionID string `json:"wallet_session_id"`
		UserID          string `json:"user_id"`
	}
	_ = json.Unmarshal(p.Arguments, &a)
	if a.WalletSessionID == "" {
		return map[string]any{"error": "wallet_session_id required"}, http.StatusBadRequest
	}
	v, found, deniedReason := st.walletGetChecked(a.WalletSessionID, cfg, agentID, a.UserID)
	if !found {
		return map[string]any{
			"status": "not_found", "wallet_session_id": a.WalletSessionID,
			"simulated": true, "note": walletSimNote,
		}, http.StatusNotFound
	}
	if deniedReason != "" {
		return map[string]any{
			"status": "denied", "reason": deniedReason,
			"wallet_session_id": a.WalletSessionID,
		}, http.StatusForbidden
	}
	v["wallet_session_id"] = a.WalletSessionID
	return v, http.StatusOK
}

// walletSimAdminHandler serves the /admin/sim/wallet endpoint (mirrors the
// alipay_sim.go admin endpoint): GET → all wallet summaries; POST
// {wallet_session_id, wallet_balance_cents} → fund/reset.
func walletSimAdminHandler(st *store) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		switch r.Method {
		case http.MethodGet:
			wallets := st.walletSummary()
			writeJSON(w, http.StatusOK, map[string]any{
				"wallets":   wallets,
				"count":     len(wallets),
				"simulated": true,
				"note":      walletSimNote,
			})
		case http.MethodPost:
			body, _ := io.ReadAll(io.LimitReader(r.Body, 4096))
			var rf struct {
				WalletSessionID    string `json:"wallet_session_id"`
				WalletBalanceCents int64  `json:"wallet_balance_cents"`
			}
			if err := json.Unmarshal(body, &rf); err != nil || rf.WalletSessionID == "" || rf.WalletBalanceCents < 0 {
				writeJSON(w, http.StatusBadRequest, map[string]string{
					"error": "wallet_session_id and non-negative wallet_balance_cents required"})
				return
			}
			// L1 fix: walletFundView snapshots the JSON view under the same lock
			// as the fund itself (no unlocked read of the wallet's live state).
			v, err := st.walletFundView(rf.WalletSessionID, rf.WalletBalanceCents, "", "")
			if err != nil {
				writeJSON(w, http.StatusForbidden, map[string]string{"error": err.Error()})
				return
			}
			v["wallet_session_id"] = rf.WalletSessionID
			v["status"] = "ok"
			writeJSON(w, http.StatusOK, v)
		default:
			writeJSON(w, http.StatusMethodNotAllowed, map[string]string{"error": "GET or POST required"})
		}
	}
}
