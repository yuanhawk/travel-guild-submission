package main

// adversarial_money_test.go — four security-critical money-path branches flagged
// as untested (0%) by a coverage audit. Each test drives the EXACT rejection/
// acceptance branch in product code (no product changes). Style matches the
// existing harness (checkout_test.go / wallet_test.go / rfc9421_test.go):
// table-free focused tests calling checkoutTool / dispatchWalletFund / safeClient
// directly, reusing coArgs/li/firstCatalogHotel/createWalletCheckout helpers.

import (
	"crypto/ecdsa"
	"crypto/elliptic"
	"crypto/rand"
	"encoding/json"
	"net"
	"net/http"
	"strings"
	"testing"
	"time"
)

// ---------------------------------------------------------------------------
// GAP 1 — L3 mandate REJECTION (403): a tier-L3 checkout presenting an
// invalid/forged ap2_mandate must be DENIED, not booked. Drives the
// verifyMandate-fails branch in checkout.go (status:"denied" → HTTP 403).
// Distinct from TestCheckoutL3Mandate (nil mandate → requires_mandate 200) and
// TestCheckoutHardMax (valid mandate, hard-max veto): here the mandate is
// PRESENT but cryptographically/structurally invalid.
// ---------------------------------------------------------------------------
func TestCheckoutL3ForgedMandateRejected(t *testing.T) {
	st := newStore()

	// Register the user's REAL device key. The attacker will sign with a different
	// key, so verifyMandate fails the signature check.
	userPriv, _ := ecdsa.GenerateKey(elliptic.P256(), rand.Reader)
	st.registerUserKey("u1", &userPriv.PublicKey)

	// bali-alaya-ubud × 2 nights = 33000¢ — comfortably under the 200000¢ hard-max
	// so we reach the L3 mandate-verification branch (not the hard-max veto).
	d, _ := checkoutTool(testCfg, st, "L3", "a", "create_checkout",
		coArgs(map[string]any{"user_id": "u1", "line_items": []map[string]any{li("bali-alaya-ubud", "2026-07-01", "2026-07-03")}}))
	cid := d["id"].(string)

	// --- Case A: FORGED signature (signed by an attacker key, not u1's device) ---
	attackerPriv, _ := ecdsa.GenerateKey(elliptic.P256(), rand.Reader)
	forged := ap2Mandate{
		UserID: "u1", CheckoutID: cid, BudgetCents: 150000, Currency: "USD",
		ValidUntil: time.Now().Add(time.Hour).Format(time.RFC3339),
	}
	forged.Signature = signMandate(forged, attackerPriv) // wrong signer
	bForged, _ := json.Marshal(map[string]any{"checkout": map[string]any{"id": cid, "ap2_mandate": forged}})
	dF, codeF := checkoutTool(testCfg, st, "L3", "a", "complete_checkout", bForged)
	if codeF != http.StatusForbidden {
		t.Fatalf("forged mandate must be 403, got code=%d %v", codeF, dF)
	}
	if dF["status"] != "denied" || dF["reason"] != "mandate_invalid_signature" {
		t.Fatalf("forged mandate: want denied/mandate_invalid_signature, got %v", dF)
	}
	// The session must NOT have been booked.
	if s := st.sessions[cid]; s.Status == "complete" || s.BookingRef != "" {
		t.Fatalf("forged mandate must NOT book: status=%s ref=%q", s.Status, s.BookingRef)
	}

	// --- Case B: VALID signature but BOUND TO A DIFFERENT checkout id (replay/
	// mis-binding). verifyMandate rejects with mandate_mismatch → 403. ---
	misbound := ap2Mandate{
		UserID: "u1", CheckoutID: "co_some_other_session", BudgetCents: 150000, Currency: "USD",
		ValidUntil: time.Now().Add(time.Hour).Format(time.RFC3339),
	}
	misbound.Signature = signMandate(misbound, userPriv) // correctly signed, wrong binding
	bMis, _ := json.Marshal(map[string]any{"checkout": map[string]any{"id": cid, "ap2_mandate": misbound}})
	dM, codeM := checkoutTool(testCfg, st, "L3", "a", "complete_checkout", bMis)
	if codeM != http.StatusForbidden || dM["status"] != "denied" || dM["reason"] != "mandate_mismatch" {
		t.Fatalf("mis-bound mandate: want 403 denied/mandate_mismatch, got code=%d %v", codeM, dM)
	}
	if s := st.sessions[cid]; s.Status == "complete" {
		t.Fatalf("mis-bound mandate must NOT book: status=%s", s.Status)
	}
}

// ---------------------------------------------------------------------------
// GAP 2 — complete_checkout WALLET-OWNERSHIP veto (cross-agent wallet leak).
// An agent that OWNS its checkout session but points it at ANOTHER agent's
// wallet must be vetoed (403 wallet_not_owner) — the wallet may not be drained
// by a non-owner. Drives the WALLET-001 branch INSIDE complete_checkout
// (cfg.RequireSignatures && wlt.OwnerAgentID != "" && wlt.OwnerAgentID != agentID).
// Distinct from TestWalletOwnerBinding, which exercises the wallet_get path.
// ---------------------------------------------------------------------------
func TestCompleteCheckoutWalletNotOwnerVeto(t *testing.T) {
	st := newStore()
	cfg := config{
		BudgetCeilingCents: 100000000, BudgetHardMaxCents: 200000000,
		// WALLET-001 ownership check only fires under RequireSignatures (prod).
		RequireSignatures: true,
	}
	hotel := firstCatalogHotel(t)
	const sess = "trip-victim-wallet"

	// Victim agentA funds a wallet (ownerAgentID = "agentA"), amply funded so the
	// veto is reached BEFORE any insufficient-funds gate.
	st.walletFund(sess, 100000000, "agentA", "")

	// Attacker agentB creates ITS OWN checkout session but binds it to agentA's
	// wallet_session_id (the cross-agent leak attempt). agentB owns the SESSION,
	// so it passes the not_session_owner gate and reaches the wallet check.
	createArgs, _ := json.Marshal(map[string]any{
		"checkout": map[string]any{
			"user_id":           "ub",
			"line_items":        []map[string]any{{"hotel_id": hotel, "checkin": "2026-03-01", "checkout": "2026-03-02", "adults": 1}},
			"user_budget_cents": int64(100000000),
			"wallet_session_id": sess, // <-- agentA's wallet
		},
	})
	d, code := checkoutTool(cfg, st, "L2", "agentB", "create_checkout", createArgs)
	if code != http.StatusOK {
		t.Fatalf("attacker create_checkout setup failed: code=%d %v", code, d)
	}
	cid := d["id"].(string)

	// Consent via update_checkout (agentB owns the session). user_id echoed
	// consistently ("ub", matching create) so this test isolates the WALLET-001
	// assertion from the (task #161) END-USER OWNERSHIP check on update/complete.
	checkoutTool(cfg, st, "L2", "agentB", "update_checkout",
		coArgs(map[string]any{"id": cid, "user_id": "ub", "buyer_consent": true}))

	// Attacker agentB completes → must be vetoed 403 wallet_not_owner.
	resp, code2 := checkoutTool(cfg, st, "L2", "agentB", "complete_checkout",
		coArgs(map[string]any{"id": cid, "user_id": "ub"}))
	if code2 != http.StatusForbidden {
		t.Fatalf("WALLET-001 complete veto: want 403, got code=%d %v", code2, resp)
	}
	if resp["status"] != "denied" || resp["reason"] != "wallet_not_owner" {
		t.Fatalf("WALLET-001 complete veto: want denied/wallet_not_owner, got %v", resp)
	}
	// Victim's wallet must be UNTOUCHED (no debit, no ledger row) and the attacker
	// session must NOT be booked.
	w, _ := st.walletGet(sess)
	if w.BalanceCents != 100000000 || len(w.Ledger) != 0 {
		t.Fatalf("vetoed commit mutated victim wallet: bal=%d ledger=%d", w.BalanceCents, len(w.Ledger))
	}
	if s := st.sessions[cid]; s.Status == "complete" || s.BookingRef != "" {
		t.Fatalf("vetoed commit must NOT book: status=%s ref=%q", s.Status, s.BookingRef)
	}
}

// ---------------------------------------------------------------------------
// GAP 3 — SSRF dial-layer BLOCK at the RFC 9421 outbound dial (safeClient
// DialContext). A cloud-metadata / link-local target (169.254.169.254) must be
// BLOCKED, and the #56-narrowed dev allowance must NOT widen it: even with
// allowDevSSRF=true the dev exception covers ONLY loopback, so metadata stays
// blocked. Existing tests cover loopback (blocked w/o dev, allowed w/ dev); this
// covers the metadata/private branch that the dev flag must never open.
// ---------------------------------------------------------------------------
func TestSSRFDialBlocksMetadataEvenWithDevSSRF(t *testing.T) {
	// IP literal as host → net.LookupIP returns it directly (no DNS), so the dial
	// deterministically targets the cloud-metadata link-local address.
	const metadataURL = "http://169.254.169.254/latest/meta-data/"

	// Dev allowance OFF: blocked.
	if _, err := safeClient(false).Get(metadataURL); err == nil {
		t.Fatal("SSRF: metadata target must be blocked with allowDevSSRF=false")
	} else if !strings.Contains(err.Error(), "blocked_ip") {
		t.Fatalf("SSRF(dev off): want blocked_ip, got %v", err)
	}

	// Dev allowance ON: STILL blocked — the #56 dev exception covers only
	// loopback, never link-local/metadata (this is the security-critical branch).
	if _, err := safeClient(true).Get(metadataURL); err == nil {
		t.Fatal("SSRF: metadata target must STAY blocked even with allowDevSSRF=true")
	} else if !strings.Contains(err.Error(), "blocked_ip") {
		t.Fatalf("SSRF(dev on): want blocked_ip for metadata, got %v", err)
	}

	// A private-range (RFC1918) target is likewise blocked in both modes.
	const privateURL = "http://10.0.0.1/"
	if _, err := safeClient(false).Get(privateURL); err == nil || !strings.Contains(err.Error(), "blocked_ip") {
		t.Fatalf("SSRF: private 10.x must be blocked (dev off), got %v", err)
	}
	if _, err := safeClient(true).Get(privateURL); err == nil || !strings.Contains(err.Error(), "blocked_ip") {
		t.Fatalf("SSRF: private 10.x must stay blocked (dev on), got %v", err)
	}
}

// ---------------------------------------------------------------------------
// M1 — CGNAT / RFC 6598 (100.64.0.0/10) must be blocked. net.IP.IsPrivate only
// covers RFC1918, so this shared-address range is NOT caught by it. AliCloud
// exposes instance metadata at 100.100.100.200 (inside 100.64.0.0/10), so an
// attacker-influenceable profile/keyfetch URL could otherwise reach cloud
// metadata. The block must hold with AllowDevSSRF both OFF and ON (the dev
// exception covers only loopback). Without the isBlockedIP CGNAT branch this
// test fails (the dial would proceed past the guard).
// ---------------------------------------------------------------------------
func TestSSRFDialBlocksCGNATMetadata(t *testing.T) {
	// AliCloud metadata + a generic CGNAT host, as IP literals so net.LookupIP
	// returns them directly (no DNS) and the dial deterministically targets them.
	for _, target := range []string{
		"http://100.100.100.200/latest/meta-data/", // AliCloud metadata
		"http://100.64.1.1/",                        // generic 100.64.0.0/10 host
	} {
		// Dev allowance OFF: blocked.
		if _, err := safeClient(false).Get(target); err == nil {
			t.Fatalf("SSRF: CGNAT target %s must be blocked with allowDevSSRF=false", target)
		} else if !strings.Contains(err.Error(), "blocked_ip") {
			t.Fatalf("SSRF(dev off) %s: want blocked_ip, got %v", target, err)
		}
		// Dev allowance ON: STILL blocked — the dev exception covers only loopback,
		// never CGNAT/metadata (the security-critical branch).
		if _, err := safeClient(true).Get(target); err == nil {
			t.Fatalf("SSRF: CGNAT target %s must STAY blocked even with allowDevSSRF=true", target)
		} else if !strings.Contains(err.Error(), "blocked_ip") {
			t.Fatalf("SSRF(dev on) %s: want blocked_ip, got %v", target, err)
		}
	}

	// Authoritative direct-guard checks on the parsed IPs.
	for _, ip := range []string{"100.100.100.200", "100.64.0.0", "100.64.1.1", "100.127.255.255"} {
		if !isBlockedIP(net.ParseIP(ip)) {
			t.Fatalf("isBlockedIP(%s): CGNAT/RFC6598 address must be blocked", ip)
		}
	}
	// Sanity: 100.63.x and 100.128.x are OUTSIDE 100.64.0.0/10 and must NOT be
	// blocked by the CGNAT branch (guards against an over-broad mask).
	for _, ip := range []string{"100.63.255.255", "100.128.0.0"} {
		if isBlockedIP(net.ParseIP(ip)) {
			t.Fatalf("isBlockedIP(%s): address outside 100.64.0.0/10 must NOT be blocked", ip)
		}
	}
}

// ---------------------------------------------------------------------------
// GAP 4 — dispatchWalletFund (0% covered): success (200) + rejection (400).
// Drives the wallet_fund MCP dispatch directly, exercising owner binding and
// the two input-validation rejections.
// ---------------------------------------------------------------------------
func TestDispatchWalletFundSuccessAndRejection(t *testing.T) {
	st := newStore()

	// --- Success: valid session + non-negative balance → 200, owner bound ---
	okArgs, _ := json.Marshal(map[string]any{
		"wallet_session_id": "trip-fund-ok", "wallet_balance_cents": int64(50000),
	})
	resp, code := dispatchWalletFund(st, "agentX",
		toolCallParams{Name: "wallet_fund", Arguments: okArgs})
	if code != http.StatusOK || resp["status"] != "ok" {
		t.Fatalf("dispatchWalletFund success: want 200/ok, got code=%d %v", code, resp)
	}
	if resp["balance_cents"].(int64) != 50000 || resp["seed_cents"].(int64) != 50000 {
		t.Fatalf("dispatchWalletFund success: wrong balance/seed: %v", resp)
	}
	if resp["simulated"] != true || resp["wallet_session_id"] != "trip-fund-ok" {
		t.Fatalf("dispatchWalletFund success: missing simulated/session label: %v", resp)
	}
	// Owner binding (WALLET-001) must record the funding agent.
	w, ok := st.walletGet("trip-fund-ok")
	if !ok || w.OwnerAgentID != "agentX" || w.BalanceCents != 50000 {
		t.Fatalf("dispatchWalletFund: wallet not bound/funded correctly: %+v", w)
	}

	// --- Rejection A: missing wallet_session_id → 400 ---
	badArgs1, _ := json.Marshal(map[string]any{"wallet_balance_cents": int64(1000)})
	resp1, code1 := dispatchWalletFund(st, "agentX",
		toolCallParams{Name: "wallet_fund", Arguments: badArgs1})
	if code1 != http.StatusBadRequest || resp1["error"] == nil {
		t.Fatalf("dispatchWalletFund missing session: want 400+error, got code=%d %v", code1, resp1)
	}

	// --- Rejection B: negative balance → 400 ---
	badArgs2, _ := json.Marshal(map[string]any{
		"wallet_session_id": "trip-neg", "wallet_balance_cents": int64(-5),
	})
	resp2, code2 := dispatchWalletFund(st, "agentX",
		toolCallParams{Name: "wallet_fund", Arguments: badArgs2})
	if code2 != http.StatusBadRequest || resp2["error"] == nil {
		t.Fatalf("dispatchWalletFund negative balance: want 400+error, got code=%d %v", code2, resp2)
	}
	// The rejected negative-balance fund must NOT have created a wallet.
	if _, exists := st.walletGet("trip-neg"); exists {
		t.Fatal("dispatchWalletFund: rejected fund must not create a wallet")
	}
}
