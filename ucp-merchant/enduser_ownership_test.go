package main

// enduser_ownership_test.go — regression tests for task #161: END-USER
// OWNERSHIP binding at the Go merchant.
//
// AgentID (RFC 9421 ProfileURL) isolates different UCP agent COMPANIES, but
// every end user of THIS orchestrator shares ONE process-wide AgentID, so
// AgentID alone cannot tell end user A from end user B. These tests drive
// checkoutSession.UserID / wallet.OwnerUserID — verified upstream by the
// Python orchestrator's _authorize_trip_action and threaded into the
// merchant request body — as the SECOND, independent ownership dimension.
//
// All checks are gated on cfg.RequireSignatures (prod); the unsigned
// demo/CI path must remain byte-identical (see the back-compat guards below).

import (
	"encoding/json"
	"net/http"
	"testing"
)

// enduserCfg is the RequireSignatures=true config shared by these tests
// (mirrors TestWalletOwnerBinding's cfg).
var enduserCfg = config{
	BudgetCeilingCents: 100000000, BudgetHardMaxCents: 200000000,
	RequireSignatures: true,
}

// TestUpdateCheckoutUserOwnershipVeto: a session created for end user "userA"
// (under agent "agentA") must reject an update_checkout claiming "userB",
// even though the AgentID (session-owner) check passes.
func TestUpdateCheckoutUserOwnershipVeto(t *testing.T) {
	st := newStore()
	hotel := firstCatalogHotel(t)

	d, code := checkoutTool(enduserCfg, st, "L2", "agentA", "create_checkout",
		coArgs(map[string]any{"user_id": "userA", "line_items": []map[string]any{li(hotel, "2026-03-01", "2026-03-02")}}))
	if code != http.StatusOK {
		t.Fatalf("setup create_checkout failed: code=%d %v", code, d)
	}
	cid := d["id"].(string)

	// Same agent, WRONG end user → must be vetoed.
	resp, code2 := checkoutTool(enduserCfg, st, "L2", "agentA", "update_checkout",
		coArgs(map[string]any{"id": cid, "user_id": "userB", "buyer_consent": true}))
	if code2 != http.StatusForbidden {
		t.Fatalf("END-USER OWNERSHIP: update_checkout with mismatched user_id must be 403, got %d: %v", code2, resp)
	}
	if resp["reason"] != "not_session_owner" {
		t.Fatalf("END-USER OWNERSHIP: expected not_session_owner, got %v", resp)
	}

	// Correct end user → consent succeeds.
	resp2, code3 := checkoutTool(enduserCfg, st, "L2", "agentA", "update_checkout",
		coArgs(map[string]any{"id": cid, "user_id": "userA", "buyer_consent": true}))
	if code3 != http.StatusOK || resp2["buyer_consent"] != true {
		t.Fatalf("END-USER OWNERSHIP: matching user_id must succeed: code=%d %v", code3, resp2)
	}
}

// TestCompleteCheckoutUserOwnershipVeto is the load-bearing test: same agent
// (same orchestrator signing key / AgentID), different end user → rejected.
// Positive control: completing as the correct end user succeeds.
func TestCompleteCheckoutUserOwnershipVeto(t *testing.T) {
	st := newStore()
	hotel := firstCatalogHotel(t)

	d, code := checkoutTool(enduserCfg, st, "L2", "agentA", "create_checkout",
		coArgs(map[string]any{"user_id": "userA", "line_items": []map[string]any{li(hotel, "2026-03-01", "2026-03-02")}}))
	if code != http.StatusOK {
		t.Fatalf("setup create_checkout failed: code=%d %v", code, d)
	}
	cid := d["id"].(string)

	// Consent as the correct end user.
	checkoutTool(enduserCfg, st, "L2", "agentA", "update_checkout",
		coArgs(map[string]any{"id": cid, "user_id": "userA", "buyer_consent": true}))

	// Complete as userB (same agentA) → must be vetoed, session must not book.
	resp, code2 := checkoutTool(enduserCfg, st, "L2", "agentA", "complete_checkout",
		coArgs(map[string]any{"id": cid, "user_id": "userB"}))
	if code2 != http.StatusForbidden {
		t.Fatalf("END-USER OWNERSHIP: complete_checkout with mismatched user_id must be 403, got %d: %v", code2, resp)
	}
	if resp["reason"] != "not_session_owner" {
		t.Fatalf("END-USER OWNERSHIP: expected not_session_owner, got %v", resp)
	}

	// Positive control: complete as the correct end user succeeds.
	resp2, code3 := checkoutTool(enduserCfg, st, "L2", "agentA", "complete_checkout",
		coArgs(map[string]any{"id": cid, "user_id": "userA"}))
	if code3 != http.StatusOK || resp2["status"] != "complete" {
		t.Fatalf("END-USER OWNERSHIP: matching user_id must complete: code=%d %v", code3, resp2)
	}
}

// TestCancelCheckoutUserOwnershipVeto mirrors the complete_checkout veto for
// cancel_checkout.
func TestCancelCheckoutUserOwnershipVeto(t *testing.T) {
	st := newStore()
	hotel := firstCatalogHotel(t)

	d, code := checkoutTool(enduserCfg, st, "L2", "agentA", "create_checkout",
		coArgs(map[string]any{"user_id": "userA", "line_items": []map[string]any{li(hotel, "2026-03-01", "2026-03-02")}}))
	if code != http.StatusOK {
		t.Fatalf("setup create_checkout failed: code=%d %v", code, d)
	}
	cid := d["id"].(string)

	resp, code2 := checkoutTool(enduserCfg, st, "L2", "agentA", "cancel_checkout",
		coArgs(map[string]any{"id": cid, "user_id": "userB"}))
	if code2 != http.StatusForbidden {
		t.Fatalf("END-USER OWNERSHIP: cancel_checkout with mismatched user_id must be 403, got %d: %v", code2, resp)
	}
	if resp["reason"] != "not_session_owner" {
		t.Fatalf("END-USER OWNERSHIP: expected not_session_owner, got %v", resp)
	}

	// Correct end user → cancels fine.
	resp2, code3 := checkoutTool(enduserCfg, st, "L2", "agentA", "cancel_checkout",
		coArgs(map[string]any{"id": cid, "user_id": "userA"}))
	if code3 != http.StatusOK || resp2["cancelled"] != true {
		t.Fatalf("END-USER OWNERSHIP: matching user_id must cancel: code=%d %v", code3, resp2)
	}
}

// TestIdempotencyReplayUserOwnershipVeto guards the complete_checkout
// idempotency short-circuit (checkout.go ~line 349): a prod replay of a
// guessed/observed idempotency_key under the SAME agent but a DIFFERENT
// end user must NOT hand back the original user's booking_ref.
func TestIdempotencyReplayUserOwnershipVeto(t *testing.T) {
	st := newStore()
	hotel := firstCatalogHotel(t)

	d, _ := checkoutTool(enduserCfg, st, "L2", "agentA", "create_checkout",
		coArgs(map[string]any{"user_id": "userA", "line_items": []map[string]any{li(hotel, "2026-03-01", "2026-03-02")}}))
	cidA := d["id"].(string)
	checkoutTool(enduserCfg, st, "L2", "agentA", "update_checkout",
		coArgs(map[string]any{"id": cidA, "user_id": "userA", "buyer_consent": true}))
	key := "replay-key-xyz"
	d2, _ := checkoutTool(enduserCfg, st, "L2", "agentA", "complete_checkout",
		coArgs(map[string]any{"id": cidA, "user_id": "userA", "idempotency_key": key}))
	if d2["status"] != "complete" {
		t.Fatalf("setup complete for userA failed: %v", d2)
	}
	refA := d2["booking_ref"].(string)

	// A SEPARATE checkout for userB, same agentA, replaying userA's key.
	dB, _ := checkoutTool(enduserCfg, st, "L2", "agentA", "create_checkout",
		coArgs(map[string]any{"user_id": "userB", "line_items": []map[string]any{li(hotel, "2026-03-01", "2026-03-02")}}))
	cidB := dB["id"].(string)
	checkoutTool(enduserCfg, st, "L2", "agentA", "update_checkout",
		coArgs(map[string]any{"id": cidB, "user_id": "userB", "buyer_consent": true}))
	dB2, codeB := checkoutTool(enduserCfg, st, "L2", "agentA", "complete_checkout",
		coArgs(map[string]any{"id": cidB, "user_id": "userB", "idempotency_key": key}))

	if dB2["idempotent"] == true {
		t.Fatalf("END-USER OWNERSHIP: userB replaying userA's idempotency_key must NOT get the idempotent short-circuit: %v", dB2)
	}
	if dB2["booking_ref"] == refA {
		t.Fatalf("END-USER OWNERSHIP: userB must NOT receive userA's booking_ref via idempotency replay: %v", dB2)
	}
	// userB's own checkout must still complete normally on its own merits.
	if codeB != http.StatusOK || dB2["status"] != "complete" {
		t.Fatalf("userB's own complete_checkout should succeed independently: code=%d %v", codeB, dB2)
	}
}

// TestWalletGetUserOwnershipVeto mirrors TestWalletOwnerBinding but on the
// end-user dimension: a wallet funded with ownerUserID="userA" must reject a
// wallet_get claiming user_id="userB", even under the SAME agentA.
func TestWalletGetUserOwnershipVeto(t *testing.T) {
	st := newStore()
	sess := "trip-user-ownership"
	if _, err := st.walletFund(sess, 100000000, "agentA", "userA"); err != nil {
		t.Fatalf("walletFund setup failed: %v", err)
	}

	walletArgs, _ := json.Marshal(map[string]any{"wallet_session_id": sess, "user_id": "userB"})
	resp, code := dispatchTool(enduserCfg, st, "L2", "agentA", toolCallParams{Name: "wallet_get", Arguments: walletArgs})
	if code != http.StatusForbidden {
		t.Fatalf("END-USER OWNERSHIP: wallet_get with mismatched user_id must be 403, got %d: %v", code, resp)
	}
	if resp["reason"] != "wallet_not_owner" {
		t.Fatalf("END-USER OWNERSHIP: expected wallet_not_owner, got %v", resp)
	}

	// Correct end user → read succeeds.
	walletArgsOK, _ := json.Marshal(map[string]any{"wallet_session_id": sess, "user_id": "userA"})
	resp2, code2 := dispatchTool(enduserCfg, st, "L2", "agentA", toolCallParams{Name: "wallet_get", Arguments: walletArgsOK})
	if code2 != http.StatusOK {
		t.Fatalf("END-USER OWNERSHIP: matching user_id must read the wallet: code=%d %v", code2, resp2)
	}
}

// ---------------------------------------------------------------------------
// Back-compat guards (must stay green): the unsigned demo/CI default
// (RequireSignatures=false) must ignore a mismatched user_id entirely, and a
// session with no bound end user (legacy anon UserID=="") must still
// complete under RequireSignatures=true (the documented anon-legacy
// fail-open — see A2/(c) of the design spec).
// ---------------------------------------------------------------------------

// TestUnsignedDemoIgnoresUserIDMismatch confirms the unsigned/demo path
// (RequireSignatures=false, the CI default) is completely unaffected by a
// mismatched user_id — the END-USER OWNERSHIP check must be inert.
func TestUnsignedDemoIgnoresUserIDMismatch(t *testing.T) {
	st := newStore()
	hotel := firstCatalogHotel(t)

	d, _ := checkoutTool(testCfg, st, "L2", "a", "create_checkout",
		coArgs(map[string]any{"user_id": "userA", "line_items": []map[string]any{li(hotel, "2026-03-01", "2026-03-02")}}))
	cid := d["id"].(string)

	// Mismatched user_id, but RequireSignatures is unset (false) on testCfg.
	resp, code := checkoutTool(testCfg, st, "L2", "a", "update_checkout",
		coArgs(map[string]any{"id": cid, "user_id": "userB", "buyer_consent": true}))
	if code != http.StatusOK || resp["buyer_consent"] != true {
		t.Fatalf("unsigned demo path must ignore user_id mismatch: code=%d %v", code, resp)
	}

	resp2, code2 := checkoutTool(testCfg, st, "L2", "a", "complete_checkout",
		coArgs(map[string]any{"id": cid, "user_id": "userB"}))
	if code2 != http.StatusOK || resp2["status"] != "complete" {
		t.Fatalf("unsigned demo path must ignore user_id mismatch and complete: code=%d %v", code2, resp2)
	}
}

// TestLegacyAnonSessionFailsOpenUnderRequireSignatures confirms a session
// created with UserID=="" (legacy anon, pre-owner_token rows) still completes
// under RequireSignatures=true regardless of the caller's claimed user_id —
// the documented anon-legacy fail-open (design spec section (c)).
func TestLegacyAnonSessionFailsOpenUnderRequireSignatures(t *testing.T) {
	st := newStore()
	hotel := firstCatalogHotel(t)

	// No user_id at create → s.UserID == "".
	d, _ := checkoutTool(enduserCfg, st, "L2", "agentA", "create_checkout",
		coArgs(map[string]any{"line_items": []map[string]any{li(hotel, "2026-03-01", "2026-03-02")}}))
	cid := d["id"].(string)

	resp, code := checkoutTool(enduserCfg, st, "L2", "agentA", "update_checkout",
		coArgs(map[string]any{"id": cid, "user_id": "anyone-at-all", "buyer_consent": true}))
	if code != http.StatusOK || resp["buyer_consent"] != true {
		t.Fatalf("legacy anon session (UserID==\"\") must fail open on update_checkout: code=%d %v", code, resp)
	}

	resp2, code2 := checkoutTool(enduserCfg, st, "L2", "agentA", "complete_checkout",
		coArgs(map[string]any{"id": cid, "user_id": "anyone-at-all"}))
	if code2 != http.StatusOK || resp2["status"] != "complete" {
		t.Fatalf("legacy anon session (UserID==\"\") must fail open and complete: code=%d %v", code2, resp2)
	}
}

// TestCompleteCheckoutOwnerlessUserWalletFailsOpen pins the mirror-image
// fail-open (2026-07-04 security review, informational/non-blocking): a wallet
// that HAS an agent owner but no recorded end-user owner (wlt.OwnerUserID==
// "", e.g. a legacy pre-#161 wallet) must still let a signed caller with a
// real, non-empty user_id complete against it — the H1 gate at checkout.go
// only fires when BOTH s.UserID and wlt.OwnerUserID are non-empty. This is
// intentional and matches every other AgentID/UserID check in this file; it
// depends entirely on the upstream orchestrator injecting a verified
// non-empty ownerUserID whenever the wallet actually has a real end user, so
// this test exists purely to make any future change to this fail-open a
// deliberate, reviewed decision.
func TestCompleteCheckoutOwnerlessUserWalletFailsOpen(t *testing.T) {
	st := newStore()
	hotel := firstCatalogHotel(t)
	const sess = "trip-ownerless-user-wallet"

	// Agent-owned, but NOT user-owned.
	if _, err := st.walletFund(sess, 100000000, "agentA", ""); err != nil {
		t.Fatalf("walletFund setup failed: %v", err)
	}

	d, code := checkoutTool(enduserCfg, st, "L2", "agentA", "create_checkout",
		coArgs(map[string]any{
			"user_id":           "userX",
			"line_items":        []map[string]any{li(hotel, "2026-03-01", "2026-03-02")},
			"wallet_session_id": sess,
		}))
	if code != http.StatusOK {
		t.Fatalf("setup create_checkout failed: code=%d %v", code, d)
	}
	cid := d["id"].(string)

	checkoutTool(enduserCfg, st, "L2", "agentA", "update_checkout",
		coArgs(map[string]any{"id": cid, "user_id": "userX", "buyer_consent": true}))

	resp, code2 := checkoutTool(enduserCfg, st, "L2", "agentA", "complete_checkout",
		coArgs(map[string]any{"id": cid, "user_id": "userX"}))
	if code2 != http.StatusOK || resp["status"] != "complete" {
		t.Fatalf("documented fail-open: signed caller with real user_id against a wlt.OwnerUserID=='' wallet must complete (upstream trust boundary), code=%d %v", code2, resp)
	}
}
