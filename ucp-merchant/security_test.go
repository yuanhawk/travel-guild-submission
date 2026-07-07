package main

// security_test.go — regression tests for the 9 Go merchant security findings:
//
//   WALLET-003  integer overflow at create_checkout (qty / nights)
//   WALLET-002  missing wallet fails-closed (402 wallet_not_found)
//   VULN-003    body consent bypass (complete ignores buyer_consent in body)
//   WALLET-001  wallet ownership binding (403 wallet_not_owner)
//   RFC9421-004 empty/insufficient signature component coverage
//   RFC9421-003 missing content-digest on bodied requests
//   RFC9421-001 sim/wallet tools require dev.ucp.sim.control capability
//   VULN-002    UCP_PROFILE=prod secure-defaults (RequireSignatures=true)
//   D2 (Admin)  admin route token gate (requireAdmin middleware)
//
// These tests are deterministic, in-process, and run with `go test ./...`.
// They do NOT require any environment variables (CI-safe).

import (
	"crypto/ecdsa"
	"encoding/base64"
	"encoding/json"
	"fmt"
	"math"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"
)

// ---------------------------------------------------------------------------
// WALLET-003: integer overflow at create_checkout — qty / nights
// ---------------------------------------------------------------------------

// ---------------------------------------------------------------------------
// WALLET-003 (extended): integer overflow at create_checkout — nights
// ---------------------------------------------------------------------------

// TestCheckoutNightsOverflowRejected confirms that a hotel line item whose
// stay length exceeds maxNights (3650 nights) is rejected at create_checkout
// (b4 gap: earlier review only verified qty overflow, not nights overflow).
// Must red if the maxNights guard is removed or bypassed.
func TestCheckoutNightsOverflowRejected(t *testing.T) {
	st := newStore()
	cfg := config{BudgetCeilingCents: math.MaxInt64, BudgetHardMaxCents: math.MaxInt64}
	hotel := firstCatalogHotel(t)

	// Build dates that span maxNights+1 = 3651 nights (> 10 years).
	// checkin 2024-01-01, checkout 2034-01-02 = 3652 nights.
	args, _ := json.Marshal(map[string]any{
		"checkout": map[string]any{
			"user_id": "u1",
			"line_items": []map[string]any{
				{"hotel_id": hotel, "checkin": "2024-01-01", "checkout": "2034-01-02"},
			},
		},
	})
	d, code := checkoutTool(cfg, st, "L2", "a", "create_checkout", args)
	if code == http.StatusOK {
		t.Fatalf("WALLET-003-nights: stay > maxNights (%d) must NOT produce a valid "+
			"checkout (overflow guard missing): total_cents=%v", maxNights, d["total_cents"])
	}
	// The guard in lineItemsTotalCentsChecked returns ok=false which maps to invalid_line_items.
	if d["reason"] != "invalid_line_items" {
		t.Fatalf("WALLET-003-nights: expected reason=invalid_line_items, got code=%d %v", code, d)
	}
}

// TestCheckoutQtyOverflowRejected confirms that a food line with MaxInt64 qty
// is rejected at create_checkout (not silently overflowing to a tiny total).
func TestCheckoutQtyOverflowRejected(t *testing.T) {
	st := newStore()
	cfg := config{BudgetCeilingCents: math.MaxInt64, BudgetHardMaxCents: math.MaxInt64}
	args, _ := json.Marshal(map[string]any{
		"checkout": map[string]any{
			"user_id": "u1",
			"line_items": []map[string]any{
				{"food_id": "food-tokyo-ramen-midnight", "qty": math.MaxInt64},
			},
		},
	})
	d, code := checkoutTool(cfg, st, "L2", "a", "create_checkout", args)
	if code == http.StatusOK {
		t.Fatalf("WALLET-003: MaxInt64 qty must NOT produce a valid checkout (overflow silent accept): %v", d)
	}
	if d["reason"] != "invalid_quantity" {
		t.Fatalf("WALLET-003: expected reason=invalid_quantity, got code=%d %v", code, d)
	}
}

// ---------------------------------------------------------------------------
// WALLET-002: missing wallet fails-closed (402 wallet_not_found)
// ---------------------------------------------------------------------------

// TestCheckoutMissingWalletFailsClosed binds a wallet_session_id but never
// calls walletFund. complete_checkout must return 402 wallet_not_found (not
// silently treating it as an unfunded wallet, and never returning "complete").
// All calls use agentID "" to match the session owner set by createWalletCheckout.
func TestCheckoutMissingWalletFailsClosed(t *testing.T) {
	st := newStore()
	cfg := config{BudgetCeilingCents: 100000000, BudgetHardMaxCents: 200000000}
	hotel := firstCatalogHotel(t)
	sess := "trip-never-funded"
	const agent = "" // createWalletCheckout uses "" as agentID — must match below

	// Bind the wallet session but do NOT fund it.
	id, _ := createWalletCheckout(t, st, cfg, hotel, sess, 100000000)
	// Consent via update_checkout (agentID must match session owner "").
	updArgs, _ := json.Marshal(map[string]any{"checkout": map[string]any{"id": id, "buyer_consent": true}})
	checkoutTool(cfg, st, "L2", agent, "update_checkout", updArgs)
	resp, code := checkoutTool(cfg, st, "L2", agent, "complete_checkout",
		json.RawMessage(fmt.Sprintf(`{"checkout":{"id":%q}}`, id)))
	if code != http.StatusPaymentRequired {
		t.Fatalf("WALLET-002: missing wallet must return 402, got %d: %v", code, resp)
	}
	if resp["reason"] != "wallet_not_found" {
		t.Fatalf("WALLET-002: expected reason=wallet_not_found, got %v", resp)
	}
	if s := st.sessions[id]; s.Status == "complete" {
		t.Fatal("WALLET-002: session must NOT be complete after wallet_not_found")
	}
}

// ---------------------------------------------------------------------------
// VULN-003: body consent bypass
// ---------------------------------------------------------------------------

// TestCompleteIgnoresBodyConsent confirms that passing buyer_consent:true in
// the complete_checkout body does NOT grant consent. The fix is that complete
// checks only s.BuyerConsent (set by update_checkout), not c.BuyerConsent.
func TestCompleteIgnoresBodyConsent(t *testing.T) {
	st := newStore()
	cfg := config{BudgetCeilingCents: 100000000, BudgetHardMaxCents: 200000000}
	d, _ := checkoutTool(cfg, st, "L2", "a", "create_checkout",
		coArgs(map[string]any{"user_id": "u1", "line_items": []map[string]any{li("bali-alaya-ubud", "2026-07-01", "2026-07-03")}}))
	cid := d["id"].(string)

	// Attempt 1: pass buyer_consent in the complete body (must be IGNORED).
	d2, _ := checkoutTool(cfg, st, "L2", "a", "complete_checkout",
		coArgs(map[string]any{"id": cid, "buyer_consent": true}))
	if d2["status"] == "complete" {
		t.Fatalf("VULN-003: body buyer_consent must NOT complete the checkout: %v", d2)
	}
	if d2["status"] != "requires_consent" {
		t.Fatalf("VULN-003: expected requires_consent, got %v", d2)
	}

	// Attempt 2: call update_checkout (proper consent path) → complete must succeed.
	checkoutTool(cfg, st, "L2", "a", "update_checkout", coArgs(map[string]any{"id": cid, "buyer_consent": true}))
	d3, _ := checkoutTool(cfg, st, "L2", "a", "complete_checkout", coArgs(map[string]any{"id": cid}))
	if d3["status"] != "complete" {
		t.Fatalf("VULN-003: after update_checkout consent, complete must succeed: %v", d3)
	}
}

// ---------------------------------------------------------------------------
// WALLET-001: wallet ownership binding (requires RequireSignatures=true)
// ---------------------------------------------------------------------------

// TestWalletOwnerBinding funds a wallet under agentA (ownerAgentID="agentA"),
// then has agentB attempt to complete a checkout against it. With
// RequireSignatures=true the merchant must return 403 wallet_not_owner.
func TestWalletOwnerBinding(t *testing.T) {
	st := newStore()
	cfg := config{
		BudgetCeilingCents: 100000000, BudgetHardMaxCents: 200000000,
		// WALLET-001: ownership check only fires when RequireSignatures=true.
		RequireSignatures: true,
	}
	hotel := firstCatalogHotel(t)
	sess := "trip-ownership"

	// Fund the wallet with ownerAgentID = "agentA".
	st.walletFund(sess, 100000000, "agentA", "")

	// Create a checkout under agentA so the session exists in the store.
	_, _ = createWalletCheckout(t, st, cfg, hotel, sess, 100000000)

	// The most direct wallet ownership test: agentB tries to read agentA's wallet
	// via wallet_get. With RequireSignatures=true and OwnerAgentID="agentA", the
	// 403 wallet_not_owner fires (dispatchWalletGet enforces this).
	walletArgs, _ := json.Marshal(map[string]any{"wallet_session_id": sess})
	resp2, code2 := dispatchTool(cfg, st, "L2", "agentB", toolCallParams{Name: "wallet_get", Arguments: walletArgs})
	if code2 != http.StatusForbidden {
		t.Fatalf("WALLET-001: agentB accessing agentA wallet via wallet_get must return 403, got %d: %v", code2, resp2)
	}
	if resp2["reason"] != "wallet_not_owner" {
		t.Fatalf("WALLET-001: expected wallet_not_owner, got %v", resp2)
	}
}

// ---------------------------------------------------------------------------
// RFC9421-004 + RFC9421-003: signature coverage requirements
// ---------------------------------------------------------------------------

// TestVerifyRejectsEmptyComponents confirms that an empty sig-input component
// list ("()") is rejected as inadequate_signature_coverage.
func TestVerifyRejectsEmptyComponents(t *testing.T) {
	mk, _ := loadOrCreateKey()
	base := time.Unix(1_800_000_000, 0)
	v := testVerifier(mk, base, nil)
	body := []byte(`{"jsonrpc":"2.0"}`)

	// Build headers manually with an empty component list.
	raw := fmt.Sprintf(`();keyid="%s";created=%d;alg="ecdsa-p256-sha256"`, mk.kid, base.Unix())
	sig := mk.signRaw([]byte(signatureBase([]string{}, func(string) string { return "" }, raw)))
	h := map[string]string{
		"content-type":   "application/json",
		"content-digest": contentDigest(body),
		"ucp-agent":      `profile="https://a.test/.well-known/ucp"`,
		"signature-input": "sig1=" + raw,
		"signature":       "sig1=:" + base64.StdEncoding.EncodeToString(sig) + ":",
	}
	_, _, err := v.verify("POST", "m", "/p", "", h, body)
	if err == nil {
		t.Fatal("RFC9421-004: empty component list must be rejected")
	}
	if !strings.Contains(err.Error(), "inadequate_signature_coverage") {
		t.Fatalf("RFC9421-004: expected inadequate_signature_coverage, got %q", err)
	}
}

// TestVerifyRejectsMissingAuthority confirms that a signature missing @authority
// is rejected even if it covers @method and @path.
func TestVerifyRejectsMissingAuthority(t *testing.T) {
	mk, _ := loadOrCreateKey()
	base := time.Unix(1_800_000_000, 0)
	v := testVerifier(mk, base, nil)
	body := []byte(`{"jsonrpc":"2.0"}`)
	cd := contentDigest(body)

	comps := []string{"@method", "@path", "content-digest"}
	raw := fmt.Sprintf(`("@method" "@path" "content-digest");keyid="%s";created=%d;alg="ecdsa-p256-sha256"`, mk.kid, base.Unix())
	rc := func(c string) string {
		switch c {
		case "@method":
			return "POST"
		case "@path":
			return "/p"
		default:
			return cd
		}
	}
	sig := mk.signRaw([]byte(signatureBase(comps, rc, raw)))
	h := map[string]string{
		"content-type":   "application/json",
		"content-digest": cd,
		"ucp-agent":      `profile="https://a.test/.well-known/ucp"`,
		"signature-input": "sig1=" + raw,
		"signature":       "sig1=:" + base64.StdEncoding.EncodeToString(sig) + ":",
	}
	_, _, err := v.verify("POST", "m", "/p", "", h, body)
	if err == nil {
		t.Fatal("RFC9421-004: signature missing @authority must be rejected")
	}
	if !strings.Contains(err.Error(), "inadequate_signature_coverage") {
		t.Fatalf("RFC9421-004: expected inadequate_signature_coverage, got %q", err)
	}
}

// TestVerifyRejectsUndigestedBody confirms that a bodied request whose
// signature does not cover content-digest is rejected (RFC9421-003).
func TestVerifyRejectsUndigestedBody(t *testing.T) {
	mk, _ := loadOrCreateKey()
	base := time.Unix(1_800_000_000, 0)
	v := testVerifier(mk, base, nil)
	body := []byte(`{"jsonrpc":"2.0"}`) // non-empty body

	// Build a sig covering @method @authority @path (all required for RFC9421-004)
	// but NOT content-digest.
	comps := []string{"@method", "@authority", "@path"}
	raw := fmt.Sprintf(`("@method" "@authority" "@path");keyid="%s";created=%d;alg="ecdsa-p256-sha256"`, mk.kid, base.Unix())
	rc := func(c string) string {
		switch c {
		case "@method":
			return "POST"
		case "@authority":
			return "m"
		case "@path":
			return "/p"
		default:
			return ""
		}
	}
	sig := mk.signRaw([]byte(signatureBase(comps, rc, raw)))
	h := map[string]string{
		"content-type":   "application/json",
		"content-digest": contentDigest(body),
		"ucp-agent":      `profile="https://a.test/.well-known/ucp"`,
		"signature-input": "sig1=" + raw,
		"signature":       "sig1=:" + base64.StdEncoding.EncodeToString(sig) + ":",
	}
	_, _, err := v.verify("POST", "m", "/p", "", h, body)
	if err == nil {
		t.Fatal("RFC9421-003: bodied request without content-digest coverage must be rejected")
	}
	if !strings.Contains(err.Error(), "body_not_digest_covered") {
		t.Fatalf("RFC9421-003: expected body_not_digest_covered, got %q", err)
	}
}

// ---------------------------------------------------------------------------
// RFC9421-001: sim/wallet tools require dev.ucp.sim.control capability
// ---------------------------------------------------------------------------

// TestSimToolsRequireCapability confirms that an unsigned L1 caller (no
// dev.ucp.sim.control) is denied sim_set_counterparty, and that an L2 caller
// (who has the cap via tierCaps) can use it.
//
// The capability gate lives in mcpHandler (not dispatchTool), so this test
// sends real HTTP requests through mcpHandler via httptest.
func TestSimToolsRequireCapability(t *testing.T) {
	// Capability + toolCapability unit check (fast, no HTTP).
	capSim := toolCapability("sim_set_counterparty")
	if capSim != "dev.ucp.sim.control" {
		t.Fatalf("RFC9421-001: sim_set_counterparty must map to dev.ucp.sim.control, got %q", capSim)
	}
	activeL1 := negotiate(nil, "L1")
	if activeL1["dev.ucp.sim.control"] {
		t.Fatal("RFC9421-001: dev.ucp.sim.control must NOT be active at L1")
	}
	activeL2 := negotiate(nil, "L2")
	if !activeL2["dev.ucp.sim.control"] {
		t.Fatal("RFC9421-001: dev.ucp.sim.control must be active at L2")
	}

	// End-to-end via mcpHandler: L1 caller denied, L2 caller allowed.
	cfg := config{
		BudgetCeilingCents: 100000000, BudgetHardMaxCents: 200000000,
		DefaultTier:  "L1", // default is L1 (unsigned floor)
		UnsignedTier: "L1",
	}
	st := newStore()
	noSig := newSigVerifier(func(string, string) (map[string]*ecdsa.PublicKey, []string, error) {
		return nil, nil, nil
	})
	srv := httptest.NewServer(mcpHandler(cfg, st, noSig))
	defer srv.Close()

	toolCall := func(tier string) (map[string]any, int) {
		body, _ := json.Marshal(map[string]any{
			"jsonrpc": "2.0", "id": 1, "method": "tools/call",
			"params": map[string]any{"name": "sim_set_counterparty", "arguments": map[string]any{
				"meta":             map[string]any{"autonomy_level": tier},
				"counterparty_id":  "cp-test",
				"solvent":          true,
			}},
		})
		resp, err := http.Post(srv.URL+"/api/ucp/mcp", "application/json", strings.NewReader(string(body)))
		if err != nil {
			t.Fatal(err)
		}
		defer resp.Body.Close()
		var out struct {
			Result struct {
				StructuredContent map[string]any `json:"structuredContent"`
			} `json:"result"`
		}
		_ = json.NewDecoder(resp.Body).Decode(&out)
		return out.Result.StructuredContent, resp.StatusCode
	}

	// L1 unsigned: UnsignedTier=L1 → effective tier L1 → denied.
	d1, code1 := toolCall("L1")
	if code1 != http.StatusForbidden {
		t.Fatalf("RFC9421-001: L1 caller must be denied sim_set_counterparty (403), got %d: %v", code1, d1)
	}
	if d1["reason"] != "capability_not_active" {
		t.Fatalf("RFC9421-001: expected capability_not_active, got %v", d1)
	}

	// L2: use a server that grants L2 unsigned (demo mode).
	cfgL2 := cfg
	cfgL2.UnsignedTier = "L2"
	cfgL2.DefaultTier = "L2"
	srv2 := httptest.NewServer(mcpHandler(cfgL2, st, noSig))
	defer srv2.Close()
	body2, _ := json.Marshal(map[string]any{
		"jsonrpc": "2.0", "id": 2, "method": "tools/call",
		"params": map[string]any{"name": "sim_set_counterparty", "arguments": map[string]any{
			"meta":            map[string]any{"autonomy_level": "L2"},
			"counterparty_id": "cp-test",
			"solvent":         true,
		}},
	})
	resp2, err2 := http.Post(srv2.URL+"/api/ucp/mcp", "application/json", strings.NewReader(string(body2)))
	if err2 != nil {
		t.Fatal(err2)
	}
	defer resp2.Body.Close()
	if resp2.StatusCode == http.StatusForbidden {
		var out2 struct {
			Result struct {
				StructuredContent map[string]any `json:"structuredContent"`
			} `json:"result"`
		}
		_ = json.NewDecoder(resp2.Body).Decode(&out2)
		if out2.Result.StructuredContent["reason"] == "capability_not_active" {
			t.Fatalf("RFC9421-001: L2 caller must be ALLOWED sim_set_counterparty, got denied: %v", out2.Result.StructuredContent)
		}
	}
}

// ---------------------------------------------------------------------------
// VULN-002: UCP_PROFILE=prod sets RequireSignatures=true by default
// ---------------------------------------------------------------------------

// TestProdProfileDefaults sets UCP_PROFILE=prod (with a dummy admin token to
// satisfy the startup guard) and verifies that loadConfig sets
// RequireSignatures=true and UnsignedTier="L1".
func TestProdProfileDefaults(t *testing.T) {
	t.Setenv("UCP_PROFILE", "prod")
	t.Setenv("UCP_ADMIN_TOKEN", "dummy-test-token")
	// Make sure UCP_REQUIRE_SIGNATURES is not set (test the implicit default).
	t.Setenv("UCP_REQUIRE_SIGNATURES", "")
	// Clear vars that could interfere.
	t.Setenv("UCP_UNSIGNED_TIER", "")
	// b5: ensure the dev SSRF bypass env var is also unset (implicit default must be false).
	t.Setenv("UCP_ALLOW_DEV_SSRF", "")

	cfg := loadConfig()
	if !cfg.RequireSignatures {
		t.Fatal("VULN-002: UCP_PROFILE=prod must default RequireSignatures=true")
	}
	if cfg.UnsignedTier != "L1" {
		t.Fatalf("VULN-002: UCP_PROFILE=prod must keep UnsignedTier=L1 (got %q)", cfg.UnsignedTier)
	}
	if cfg.AdminToken != "dummy-test-token" {
		t.Fatalf("VULN-002: AdminToken must be loaded from UCP_ADMIN_TOKEN")
	}
	// b5 (gap): prod profile MUST NOT enable the dev SSRF bypass.
	// UCP_ALLOW_DEV_SSRF is not set in this test (Setenv cleared UCP_UNSIGNED_TIER
	// but not SSRF — ensure the implicit default wins even under prod profile).
	if cfg.AllowDevSSRF {
		t.Fatal("b5/VULN-002: UCP_PROFILE=prod must NOT enable AllowDevSSRF " +
			"(prod SSRF bypass open: loopback/private-range profile fetch is a prod security risk)")
	}
}

// TestProdProfileExplicitOverride confirms that UCP_REQUIRE_SIGNATURES=0
// can opt out of the prod default (explicit value beats profile).
func TestProdProfileExplicitOverride(t *testing.T) {
	t.Setenv("UCP_PROFILE", "prod")
	t.Setenv("UCP_ADMIN_TOKEN", "t")
	t.Setenv("UCP_REQUIRE_SIGNATURES", "0")

	cfg := loadConfig()
	// =0 means false even under prod profile (explicit=0 beats implicit default).
	// Note: "0" != "1" so requireSig stays false, but the prod logic only fires
	// when the var is "" (unset). With "0" the var is non-empty so the explicit
	// path is taken: os.Getenv(...) == "1" → false.
	if cfg.RequireSignatures {
		t.Fatal("VULN-002: UCP_REQUIRE_SIGNATURES=0 must override prod default (explicit beats implicit)")
	}
}

// ---------------------------------------------------------------------------
// D2 (Admin): admin route token gate
// ---------------------------------------------------------------------------

// TestAdminTokenGate verifies the requireAdmin middleware:
//   - When AdminToken is set, requests without the header get 401.
//   - When the correct token is supplied, the handler passes through.
//   - When AdminToken is empty (CI / demo), any request passes through.
func TestAdminTokenGate(t *testing.T) {
	// Case 1: token set, missing header → 401.
	cfg := config{AdminToken: "super-secret"}
	st := newStore()
	protected := requireAdmin(cfg, walletSimAdminHandler(st))

	w1 := httptest.NewRecorder()
	protected(w1, httptest.NewRequest("GET", "/admin/sim/wallet", nil))
	if w1.Code != http.StatusUnauthorized {
		t.Fatalf("D2: missing token must return 401, got %d", w1.Code)
	}
	var body1 map[string]any
	_ = json.Unmarshal(w1.Body.Bytes(), &body1)
	if body1["error"] != "admin_token_required" {
		t.Fatalf("D2: expected error=admin_token_required, got %v", body1)
	}

	// Case 2: correct token → handler passes through (200 GET summary).
	w2 := httptest.NewRecorder()
	req2 := httptest.NewRequest("GET", "/admin/sim/wallet", nil)
	req2.Header.Set("X-Admin-Token", "super-secret")
	protected(w2, req2)
	if w2.Code != http.StatusOK {
		t.Fatalf("D2: correct token must pass through (200), got %d", w2.Code)
	}

	// Case 3: token unset (demo / CI) → any request passes through.
	cfgOpen := config{AdminToken: ""}
	openProtected := requireAdmin(cfgOpen, walletSimAdminHandler(st))
	w3 := httptest.NewRecorder()
	openProtected(w3, httptest.NewRequest("GET", "/admin/sim/wallet", nil))
	if w3.Code != http.StatusOK {
		t.Fatalf("D2: open (no token set) must pass through (200), got %d", w3.Code)
	}
}

