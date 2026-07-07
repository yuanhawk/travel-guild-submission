package main

import (
	"crypto/ecdsa"
	"crypto/elliptic"
	"crypto/rand"
	"encoding/json"
	"testing"
	"time"
)

func coArgs(checkout map[string]any) json.RawMessage {
	b, _ := json.Marshal(map[string]any{"checkout": checkout})
	return b
}

func li(hotel, ci, co string) map[string]any {
	return map[string]any{"hotel_id": hotel, "checkin": ci, "checkout": co}
}

var testCfg = config{BudgetCeilingCents: 80000, BudgetHardMaxCents: 200000}

func TestCheckoutL2Flow(t *testing.T) {
	st := newStore()
	d, code := checkoutTool(testCfg, st, "L2", "agentA", "create_checkout",
		coArgs(map[string]any{"user_id": "u1", "line_items": []map[string]any{li("bali-alaya-ubud", "2026-07-01", "2026-07-03")}}))
	if code != 200 || d["status"] != "incomplete" {
		t.Fatalf("create: %v", d)
	}
	if d["total_cents"].(int64) != 33000 {
		t.Fatalf("total_cents want 33000 got %v", d["total_cents"])
	}
	cid := d["id"].(string)

	d, _ = checkoutTool(testCfg, st, "L2", "agentA", "complete_checkout", coArgs(map[string]any{"id": cid}))
	if d["status"] != "requires_consent" {
		t.Fatalf("want requires_consent: %v", d)
	}
	// Ownership: a different agent cannot act on the session.
	d, _ = checkoutTool(testCfg, st, "L2", "agentB", "complete_checkout", coArgs(map[string]any{"id": cid}))
	if d["reason"] != "not_session_owner" {
		t.Fatalf("want not_session_owner: %v", d)
	}
	checkoutTool(testCfg, st, "L2", "agentA", "update_checkout", coArgs(map[string]any{"id": cid, "buyer_consent": true}))
	d, _ = checkoutTool(testCfg, st, "L2", "agentA", "complete_checkout", coArgs(map[string]any{"id": cid}))
	if d["status"] != "complete" || d["booking_ref"] == "" {
		t.Fatalf("want complete+ref: %v", d)
	}
	d, _ = checkoutTool(testCfg, st, "L2", "agentA", "complete_checkout", coArgs(map[string]any{"id": cid}))
	if d["idempotent"] != true {
		t.Fatalf("want idempotent: %v", d)
	}
}

func TestCheckoutL2OverBudget(t *testing.T) {
	// Old behaviour: static house ceiling (BudgetCeilingCents=80000).
	// bali-alila-seminyak at 3 nights = 119400¢ > 80000¢ → price_exceeds_budget.
	st := newStore()
	d, _ := checkoutTool(testCfg, st, "L2", "a", "create_checkout",
		coArgs(map[string]any{"user_id": "u1", "line_items": []map[string]any{li("bali-alila-seminyak", "2026-07-01", "2026-07-04")}})) // 119400
	cid := d["id"].(string)
	checkoutTool(testCfg, st, "L2", "a", "update_checkout", coArgs(map[string]any{"id": cid, "buyer_consent": true}))
	d, _ = checkoutTool(testCfg, st, "L2", "a", "complete_checkout", coArgs(map[string]any{"id": cid}))
	if d["reason"] != "price_exceeds_budget" {
		t.Fatalf("want price_exceeds_budget: %v", d)
	}
}

// TestCheckoutUserBudgetVeto — SEV-1b: user_budget_cents overrides the house ceiling.
// user_budget=90000¢, package=119400¢, house_cap=200000¢ (well above) → veto.
// user_budget=125000¢, package=119400¢ → accept (user budget generous enough).
func TestCheckoutUserBudgetVeto(t *testing.T) {
	// Cfg with a very high house ceiling so only the user budget matters.
	cfg := config{BudgetCeilingCents: 200000, BudgetHardMaxCents: 500000}
	st := newStore()

	// Case 1: package (119400¢) > user_budget (90000¢) → price_exceeds_budget.
	d, _ := checkoutTool(cfg, st, "L2", "a", "create_checkout",
		coArgs(map[string]any{
			"user_id": "u1", "line_items": []map[string]any{li("bali-alila-seminyak", "2026-07-01", "2026-07-04")}, // 119400
			"user_budget_cents": int64(90000),
		}))
	cid := d["id"].(string)
	// VULN-003: consent must come from update_checkout, not complete_checkout body.
	checkoutTool(cfg, st, "L2", "a", "update_checkout", coArgs(map[string]any{"id": cid, "buyer_consent": true}))
	d, _ = checkoutTool(cfg, st, "L2", "a", "complete_checkout", coArgs(map[string]any{"id": cid}))
	if d["reason"] != "price_exceeds_budget" {
		t.Fatalf("SEV-1b: want price_exceeds_budget when package>user_budget: %v", d)
	}
	if ceiling, ok := d["budget_ceiling_cents"].(int64); !ok || ceiling != 90000 {
		t.Fatalf("SEV-1b: budget_ceiling_cents should be user_budget=90000, got %v", d["budget_ceiling_cents"])
	}

	// Case 2: package (119400¢) < user_budget (125000¢) → accept.
	st2 := newStore()
	d2, _ := checkoutTool(cfg, st2, "L2", "a", "create_checkout",
		coArgs(map[string]any{
			"user_id": "u1", "line_items": []map[string]any{li("bali-alila-seminyak", "2026-07-01", "2026-07-04")}, // 119400
			"user_budget_cents": int64(125000),
		}))
	cid2 := d2["id"].(string)
	// VULN-003: consent from update_checkout.
	checkoutTool(cfg, st2, "L2", "a", "update_checkout", coArgs(map[string]any{"id": cid2, "buyer_consent": true}))
	d2, _ = checkoutTool(cfg, st2, "L2", "a", "complete_checkout", coArgs(map[string]any{"id": cid2}))
	if d2["status"] != "complete" {
		t.Fatalf("SEV-1b: want complete when package<user_budget: %v", d2)
	}
}

// TestCheckoutUserBudgetAboveHouseDefault — SEV-1b regression guard:
// With user_budget > $800 house default the ceiling must be the user_budget, not the
// house default.  This is the core SEV-1b bug: prior code only let the user budget
// LOWER the house default, never raise it — every trip over $800 was wrongly capped.
//
//  user_budget=100000¢ (above house default 80000¢):
//    • 90000¢ package (bali-alaya-ubud × 5 nights = 82500¢, < 100000)  → ACCEPT
//    • 110000¢ package (bali-alila-seminyak × 3 = 119400¢, > 100000)   → denied, ceiling=100000
//  user_budget=50000¢ (below house default):
//    • 62000¢ package (bali-legian-beach × 5 = 62000¢, > 50000)        → denied, ceiling=50000
//  no user_budget → falls back to house default 80000¢:
//    • 119400¢ package                                                   → denied, ceiling=80000
func TestCheckoutUserBudgetAboveHouseDefault(t *testing.T) {
	// Use testCfg: BudgetCeilingCents=80000, BudgetHardMaxCents=200000.
	// With user_budget=100000 the new ceiling must be 100000, not 80000.

	// --- Case 1: user_budget=100000, package=82500 (5n × 16500) → ACCEPT ---
	st1 := newStore()
	d, _ := checkoutTool(testCfg, st1, "L2", "a", "create_checkout",
		coArgs(map[string]any{
			"user_id":           "u1",
			"line_items":        []map[string]any{li("bali-alaya-ubud", "2026-07-01", "2026-07-06")}, // 5n × 16500 = 82500
			"user_budget_cents": int64(100000),
		}))
	cid1 := d["id"].(string)
	// VULN-003: consent via update_checkout.
	checkoutTool(testCfg, st1, "L2", "a", "update_checkout", coArgs(map[string]any{"id": cid1, "buyer_consent": true}))
	d, _ = checkoutTool(testCfg, st1, "L2", "a", "complete_checkout",
		coArgs(map[string]any{"id": cid1}))
	if d["status"] != "complete" {
		t.Fatalf("SEV-1b: user_budget=100000 > house_default=80000: package 82500 should ACCEPT (not cap at 80000); got %v", d)
	}

	// --- Case 2: user_budget=100000, package=119400 (3n × 39800) → denied, ceiling=100000 ---
	st2 := newStore()
	d2, _ := checkoutTool(testCfg, st2, "L2", "a", "create_checkout",
		coArgs(map[string]any{
			"user_id":           "u1",
			"line_items":        []map[string]any{li("bali-alila-seminyak", "2026-07-01", "2026-07-04")}, // 3n × 39800 = 119400
			"user_budget_cents": int64(100000),
		}))
	cid2 := d2["id"].(string)
	// VULN-003: consent via update_checkout.
	checkoutTool(testCfg, st2, "L2", "a", "update_checkout", coArgs(map[string]any{"id": cid2, "buyer_consent": true}))
	d2, _ = checkoutTool(testCfg, st2, "L2", "a", "complete_checkout",
		coArgs(map[string]any{"id": cid2}))
	if d2["reason"] != "price_exceeds_budget" {
		t.Fatalf("SEV-1b: user_budget=100000, package 119400 should be denied; got %v", d2)
	}
	if ceil, ok := d2["budget_ceiling_cents"].(int64); !ok || ceil != 100000 {
		t.Fatalf("SEV-1b: budget_ceiling_cents should be user_budget=100000, got %v", d2["budget_ceiling_cents"])
	}

	// --- Case 3: user_budget=50000 (< house default 80000), package=62000 → denied, ceiling=50000 ---
	st3 := newStore()
	d3, _ := checkoutTool(testCfg, st3, "L2", "a", "create_checkout",
		coArgs(map[string]any{
			"user_id":           "u1",
			"line_items":        []map[string]any{li("bali-legian-beach", "2026-07-01", "2026-07-06")}, // 5n × 12400 = 62000
			"user_budget_cents": int64(50000),
		}))
	cid3 := d3["id"].(string)
	// VULN-003: consent via update_checkout.
	checkoutTool(testCfg, st3, "L2", "a", "update_checkout", coArgs(map[string]any{"id": cid3, "buyer_consent": true}))
	d3, _ = checkoutTool(testCfg, st3, "L2", "a", "complete_checkout",
		coArgs(map[string]any{"id": cid3}))
	if d3["reason"] != "price_exceeds_budget" {
		t.Fatalf("SEV-1b: user_budget=50000 < house_default=80000: package 62000 should be denied at user ceiling; got %v", d3)
	}
	if ceil, ok := d3["budget_ceiling_cents"].(int64); !ok || ceil != 50000 {
		t.Fatalf("SEV-1b: budget_ceiling_cents should be user_budget=50000 (lower binds), got %v", d3["budget_ceiling_cents"])
	}

	// --- Case 4: no user_budget → falls back to house default (80000¢) ---
	st4 := newStore()
	d4, _ := checkoutTool(testCfg, st4, "L2", "a", "create_checkout",
		coArgs(map[string]any{
			"user_id":    "u1",
			"line_items": []map[string]any{li("bali-alila-seminyak", "2026-07-01", "2026-07-04")}, // 119400 > 80000
		}))
	cid4 := d4["id"].(string)
	// VULN-003: consent via update_checkout.
	checkoutTool(testCfg, st4, "L2", "a", "update_checkout", coArgs(map[string]any{"id": cid4, "buyer_consent": true}))
	d4, _ = checkoutTool(testCfg, st4, "L2", "a", "complete_checkout",
		coArgs(map[string]any{"id": cid4}))
	if d4["reason"] != "price_exceeds_budget" {
		t.Fatalf("SEV-1b: no user_budget → house_default=80000: package 119400 should be denied; got %v", d4)
	}
	if ceil, ok := d4["budget_ceiling_cents"].(int64); !ok || ceil != 80000 {
		t.Fatalf("SEV-1b: no user_budget → house default ceiling should be 80000, got %v", d4["budget_ceiling_cents"])
	}
}

// TestCheckoutIdempotencyKey — SEV-1a: client idempotency_key → same booking_ref.
func TestCheckoutIdempotencyKey(t *testing.T) {
	st := newStore()
	d, _ := checkoutTool(testCfg, st, "L2", "a", "create_checkout",
		coArgs(map[string]any{"user_id": "u1", "line_items": []map[string]any{li("bali-alaya-ubud", "2026-07-01", "2026-07-03")}}))
	cid := d["id"].(string)
	idemKey := "trip-test-idem-12345"

	// First complete: consent via update_checkout (VULN-003), then complete.
	checkoutTool(testCfg, st, "L2", "a", "update_checkout", coArgs(map[string]any{"id": cid, "buyer_consent": true}))
	d, _ = checkoutTool(testCfg, st, "L2", "a", "complete_checkout", coArgs(map[string]any{
		"id": cid, "idempotency_key": idemKey,
	}))
	if d["status"] != "complete" || d["booking_ref"] == "" {
		t.Fatalf("first complete: want complete+ref: %v", d)
	}
	ref1 := d["booking_ref"].(string)

	// Create a NEW checkout (different session) but use the SAME idempotency_key.
	// This simulates the orchestrator retrying after a timeout.
	d2, _ := checkoutTool(testCfg, st, "L2", "a", "create_checkout",
		coArgs(map[string]any{"user_id": "u1", "line_items": []map[string]any{li("bali-alaya-ubud", "2026-07-01", "2026-07-03")}}))
	cid2 := d2["id"].(string)
	// Consent for the second checkout (though it will be idempotent-short-circuited).
	checkoutTool(testCfg, st, "L2", "a", "update_checkout", coArgs(map[string]any{"id": cid2, "buyer_consent": true}))
	d2, _ = checkoutTool(testCfg, st, "L2", "a", "complete_checkout", coArgs(map[string]any{
		"id": cid2, "idempotency_key": idemKey,
	}))
	if d2["idempotent"] != true {
		t.Fatalf("SEV-1a: second call with same idempotency_key must return idempotent=true: %v", d2)
	}
	ref2 := d2["booking_ref"].(string)
	if ref1 != ref2 {
		t.Fatalf("SEV-1a: idempotency must return same booking_ref: %q != %q", ref1, ref2)
	}
}

// TestCheckoutIdempotencyOwnership — #1 (HIGH): a DIFFERENT agent must NOT replay
// another agent's idempotency_key and be short-circuited into their booking
// (cross-agent leak + consent bypass on the money path).
func TestCheckoutIdempotencyOwnership(t *testing.T) {
	st := newStore()
	// Agent "a" books with a key.
	d, _ := checkoutTool(testCfg, st, "L2", "a", "create_checkout",
		coArgs(map[string]any{"user_id": "ua", "line_items": []map[string]any{li("bali-alaya-ubud", "2026-07-01", "2026-07-03")}}))
	cidA := d["id"].(string)
	key := "victim-key-abc"
	// VULN-003: consent via update_checkout.
	checkoutTool(testCfg, st, "L2", "a", "update_checkout", coArgs(map[string]any{"id": cidA, "buyer_consent": true}))
	d, _ = checkoutTool(testCfg, st, "L2", "a", "complete_checkout", coArgs(map[string]any{
		"id": cidA, "idempotency_key": key,
	}))
	if d["status"] != "complete" {
		t.Fatalf("agent a complete failed: %v", d)
	}
	refA := d["booking_ref"].(string)

	// Agent "b" creates its OWN checkout and REPLAYS agent a's key.
	d2, _ := checkoutTool(testCfg, st, "L2", "b", "create_checkout",
		coArgs(map[string]any{"user_id": "ub", "line_items": []map[string]any{li("bali-alaya-ubud", "2026-07-01", "2026-07-03")}}))
	cidB := d2["id"].(string)
	// VULN-003: consent for agent b's checkout too.
	checkoutTool(testCfg, st, "L2", "b", "update_checkout", coArgs(map[string]any{"id": cidB, "buyer_consent": true}))
	d2, _ = checkoutTool(testCfg, st, "L2", "b", "complete_checkout", coArgs(map[string]any{
		"id": cidB, "idempotency_key": key,
	}))
	// b must NOT be handed a's booking via the idempotent short-circuit.
	if d2["idempotent"] == true {
		t.Fatalf("#1 leak: agent b replayed agent a's key and got the idempotent short-circuit: %v", d2)
	}
	if d2["booking_ref"] == refA {
		t.Fatalf("#1 leak: agent b received agent a's booking_ref %q (cross-agent leak)", refA)
	}
}

// TestCheckoutL3Mandate exercises the full L3 mandate path with a registered
// device key — this also proves the C1 deadlock fix (it would hang otherwise).
func TestCheckoutL3Mandate(t *testing.T) {
	st := newStore()
	priv, _ := ecdsa.GenerateKey(elliptic.P256(), rand.Reader)
	st.registerUserKey("u1", &priv.PublicKey)

	d, _ := checkoutTool(testCfg, st, "L3", "a", "create_checkout",
		coArgs(map[string]any{"user_id": "u1", "line_items": []map[string]any{li("bali-alila-seminyak", "2026-07-01", "2026-07-04")}})) // 119400
	cid := d["id"].(string)

	d, _ = checkoutTool(testCfg, st, "L3", "a", "complete_checkout", coArgs(map[string]any{"id": cid}))
	if d["status"] != "requires_mandate" {
		t.Fatalf("want requires_mandate: %v", d)
	}
	m := ap2Mandate{UserID: "u1", CheckoutID: cid, BudgetCents: 150000, Currency: "USD",
		ValidUntil: time.Now().Add(time.Hour).Format(time.RFC3339)}
	m.Signature = signMandate(m, priv)
	b, _ := json.Marshal(map[string]any{"checkout": map[string]any{"id": cid, "ap2_mandate": m}})
	d, _ = checkoutTool(testCfg, st, "L3", "a", "complete_checkout", b)
	if d["status"] != "complete" {
		t.Fatalf("L3 valid mandate should complete (deadlock/verify?): %v", d)
	}
}

func TestCheckoutHardMax(t *testing.T) {
	st := newStore()
	priv, _ := ecdsa.GenerateKey(elliptic.P256(), rand.Reader)
	st.registerUserKey("u1", &priv.PublicKey)
	d, _ := checkoutTool(testCfg, st, "L3", "a", "create_checkout",
		coArgs(map[string]any{"user_id": "u1", "line_items": []map[string]any{li("bali-alila-seminyak", "2026-07-01", "2026-07-07")}})) // 238800 > hard max
	cid := d["id"].(string)
	m := ap2Mandate{UserID: "u1", CheckoutID: cid, BudgetCents: 300000, Currency: "USD",
		ValidUntil: time.Now().Add(time.Hour).Format(time.RFC3339)}
	m.Signature = signMandate(m, priv)
	b, _ := json.Marshal(map[string]any{"checkout": map[string]any{"id": cid, "ap2_mandate": m}})
	d, _ = checkoutTool(testCfg, st, "L3", "a", "complete_checkout", b)
	if d["reason"] != "price_exceeds_hard_max" {
		t.Fatalf("want price_exceeds_hard_max: %v", d)
	}
}

// TestCapacityCheck — SEV-2: adults > MaxOccupancy → error at create_checkout.
func TestCapacityCheck(t *testing.T) {
	st := newStore()

	// Case 1: 5-star hotel (bali-alaya-ubud, MaxOccupancy=4), adults=5 → error
	d, code := checkoutTool(testCfg, st, "L2", "a", "create_checkout",
		coArgs(map[string]any{
			"user_id": "u1",
			"line_items": []map[string]any{
				{"hotel_id": "bali-alaya-ubud", "checkin": "2026-07-01", "checkout": "2026-07-03", "adults": 5},
			},
		}))
	if code != 400 || d["reason"] != "exceeds_room_capacity" {
		t.Fatalf("SEV-2: expected exceeds_room_capacity for 5 adults at 4-max hotel, got code=%d %v", code, d)
	}

	// Case 2: same hotel, adults=4 → ok (at limit)
	d2, code2 := checkoutTool(testCfg, st, "L2", "a", "create_checkout",
		coArgs(map[string]any{
			"user_id": "u1",
			"line_items": []map[string]any{
				{"hotel_id": "bali-alaya-ubud", "checkin": "2026-07-01", "checkout": "2026-07-03", "adults": 4},
			},
		}))
	if code2 != 200 || d2["status"] != "incomplete" {
		t.Fatalf("SEV-2: expected ok for 4 adults at 4-max hotel, got code=%d %v", code2, d2)
	}

	// Case 3: no adults field (0) → no capacity check (default behavior)
	d3, code3 := checkoutTool(testCfg, st, "L2", "a", "create_checkout",
		coArgs(map[string]any{
			"user_id": "u1",
			"line_items": []map[string]any{
				{"hotel_id": "bali-alaya-ubud", "checkin": "2026-07-01", "checkout": "2026-07-03"},
			},
		}))
	if code3 != 200 || d3["status"] != "incomplete" {
		t.Fatalf("SEV-2: adults=0 (unset) should not trigger capacity check: code=%d %v", code3, d3)
	}
}

// TestSimAvailability — L3 World Simulator: flip_availability gates catalog + checkout.
//
// Verifies three invariants:
//   1. sim_set_availability(hotel, false) → search_catalog excludes the hotel.
//   2. lookup_catalog returns status:"unavailable" for a sold-out hotel.
//   3. create_checkout rejects a line_item referencing a sold-out hotel
//      (status:"unavailable", reason:"hotel_sold_out").
//   4. Restoring availability (true) makes the hotel bookable again.
func TestSimAvailability(t *testing.T) {
	st := newStore()
	const hotelID = "bali-legian-beach"
	const ci, co = "2026-07-03", "2026-07-05"

	// ── baseline: hotel is available ──────────────────────────────────────────

	// search_catalog should include bali-legian-beach
	searchArgs := func() json.RawMessage {
		b, _ := json.Marshal(map[string]any{"query": map[string]any{
			"city": "bali", "checkin": ci, "checkout": co, "max_cents": int64(500000),
		}})
		return b
	}
	res, code := catalogToolWithStore(st, "search_catalog", searchArgs())
	if code != 200 {
		t.Fatalf("search_catalog: want 200, got %d", code)
	}
	results := res["results"].([]map[string]any)
	found := false
	for _, r := range results {
		if r["hotel_id"] == hotelID {
			found = true
			break
		}
	}
	if !found {
		t.Fatalf("baseline: %s should appear in search_catalog results", hotelID)
	}

	// create_checkout should succeed
	d, code := checkoutTool(testCfg, st, "L2", "agentA", "create_checkout",
		coArgs(map[string]any{"user_id": "u1", "line_items": []map[string]any{li(hotelID, ci, co)}}))
	if code != 200 || d["status"] != "incomplete" {
		t.Fatalf("baseline create_checkout: want incomplete, got code=%d %v", code, d)
	}

	// ── flip to unavailable ───────────────────────────────────────────────────
	st.simSetAvailability(hotelID, false)

	// 1. search_catalog EXCLUDES sold-out hotel
	res2, _ := catalogToolWithStore(st, "search_catalog", searchArgs())
	results2 := res2["results"].([]map[string]any)
	for _, r := range results2 {
		if r["hotel_id"] == hotelID {
			t.Fatalf("sim: sold-out hotel %s must NOT appear in search_catalog", hotelID)
		}
	}

	// 2. lookup_catalog returns status:"unavailable"
	lookupArgs, _ := json.Marshal(map[string]any{"product": map[string]any{
		"hotel_id": hotelID, "checkin": ci, "checkout": co,
	}})
	res3, code3 := catalogToolWithStore(st, "lookup_catalog", lookupArgs)
	if code3 != 409 {
		t.Fatalf("sim: lookup_catalog sold-out should return 409, got %d: %v", code3, res3)
	}
	if res3["status"] != "unavailable" {
		t.Fatalf("sim: lookup_catalog should return status:unavailable, got %v", res3)
	}

	// 3. create_checkout REJECTS sold-out hotel
	d2, code2 := checkoutTool(testCfg, st, "L2", "agentA", "create_checkout",
		coArgs(map[string]any{"user_id": "u1", "line_items": []map[string]any{li(hotelID, ci, co)}}))
	if code2 != 409 {
		t.Fatalf("sim: create_checkout sold-out should return 409, got %d: %v", code2, d2)
	}
	if d2["status"] != "unavailable" || d2["reason"] != "hotel_sold_out" {
		t.Fatalf("sim: create_checkout should return status:unavailable reason:hotel_sold_out, got %v", d2)
	}
	if d2["hotel_id"] != hotelID {
		t.Fatalf("sim: hotel_id in error should be %q, got %v", hotelID, d2["hotel_id"])
	}

	// ── restore availability ──────────────────────────────────────────────────
	st.simSetAvailability(hotelID, true)

	// search_catalog includes the hotel again
	res4, _ := catalogToolWithStore(st, "search_catalog", searchArgs())
	results4 := res4["results"].([]map[string]any)
	restored := false
	for _, r := range results4 {
		if r["hotel_id"] == hotelID {
			restored = true
			break
		}
	}
	if !restored {
		t.Fatalf("sim: hotel %s should reappear in search after restoring availability", hotelID)
	}

	// create_checkout works again after restore
	d4, code4 := checkoutTool(testCfg, st, "L2", "agentA", "create_checkout",
		coArgs(map[string]any{"user_id": "u1", "line_items": []map[string]any{li(hotelID, ci, co)}}))
	if code4 != 200 || d4["status"] != "incomplete" {
		t.Fatalf("sim: create_checkout after restore should succeed, got code=%d %v", code4, d4)
	}
}

// TestSimTransfer covers the cascade flight/transfer-cancellation fault:
// flip a directed inter-city edge to cancelled, read it back, restore it.
// This fault is read-as-data by the Transport agent (NOT a hotel/checkout gate).
func TestSimTransfer(t *testing.T) {
	st := newStore()

	// baseline: every transfer feasible
	if !st.simTransferFeasible("gondar", "lalibela") {
		t.Fatalf("baseline: gondar→lalibela should be feasible")
	}
	if len(st.simListCancelledTransfers()) != 0 {
		t.Fatalf("baseline: no cancelled transfers expected")
	}

	// cancel ET120 Gondar→Lalibela
	st.simSetTransfer("Gondar", "Lalibela", false)
	if st.simTransferFeasible("gondar", "lalibela") {
		t.Fatalf("after cancel: gondar→lalibela should be INFEASIBLE")
	}
	// directional: the reverse edge is unaffected
	if !st.simTransferFeasible("lalibela", "gondar") {
		t.Fatalf("cancel is directional: lalibela→gondar should still be feasible")
	}
	// case-insensitive read
	if st.simTransferFeasible("GONDAR", "LALIBELA") {
		t.Fatalf("transfer lookup should be case-insensitive")
	}
	cancelled := st.simListCancelledTransfers()
	if len(cancelled) != 1 || cancelled[0]["from"] != "gondar" || cancelled[0]["to"] != "lalibela" {
		t.Fatalf("cancelled list wrong: %v", cancelled)
	}

	// second cascading cancel (ET106 Addis→Semera)
	st.simSetTransfer("addis ababa", "semera", false)
	if len(st.simListCancelledTransfers()) != 2 {
		t.Fatalf("expected 2 cancelled transfers, got %d", len(st.simListCancelledTransfers()))
	}

	// the MCP dispatch path
	args, _ := json.Marshal(map[string]any{"from_city": "gondar", "to_city": "lalibela", "feasible": true})
	d, code := dispatchSimTransferTool(st, toolCallParams{Name: "sim_set_transfer", Arguments: args})
	if code != 200 || d["status"] != "ok" || d["feasible"] != true {
		t.Fatalf("dispatchSimTransferTool restore: code=%d %v", code, d)
	}
	if !st.simTransferFeasible("gondar", "lalibela") {
		t.Fatalf("after restore via MCP: gondar→lalibela should be feasible again")
	}
	// missing args rejected
	bad, badCode := dispatchSimTransferTool(st, toolCallParams{Name: "sim_set_transfer", Arguments: json.RawMessage(`{"from_city":"x"}`)})
	if badCode != 400 {
		t.Fatalf("missing to_city should be 400, got %d: %v", badCode, bad)
	}
}

// TestCancelCheckout — VOID path (§12.1 cascade): cancelling a completed checkout
// releases its booking (status→cancelled, booking_ref dropped); idempotent and
// owner-only; a cancelled checkout can never be re-completed.
func TestCancelCheckout(t *testing.T) {
	st := newStore()

	// Complete a checkout so there's a live booking to void.
	d, _ := checkoutTool(testCfg, st, "L2", "agentA", "create_checkout",
		coArgs(map[string]any{"user_id": "u1", "line_items": []map[string]any{li("bali-alaya-ubud", "2026-07-01", "2026-07-03")}}))
	cid := d["id"].(string)
	checkoutTool(testCfg, st, "L2", "agentA", "update_checkout", coArgs(map[string]any{"id": cid, "buyer_consent": true}))
	d, _ = checkoutTool(testCfg, st, "L2", "agentA", "complete_checkout", coArgs(map[string]any{"id": cid}))
	if d["status"] != "complete" || d["booking_ref"] == "" {
		t.Fatalf("setup: want complete+ref: %v", d)
	}
	ref := d["booking_ref"].(string)

	// Non-owner cannot cancel → 403 not_session_owner.
	dNon, codeNon := checkoutTool(testCfg, st, "L2", "agentB", "cancel_checkout", coArgs(map[string]any{"id": cid}))
	if codeNon != 403 || dNon["reason"] != "not_session_owner" {
		t.Fatalf("non-owner cancel: want 403 not_session_owner, got code=%d %v", codeNon, dNon)
	}
	// Owner's booking must be untouched after the rejected non-owner cancel.
	if s := st.sessions[cid]; s.Status != "complete" || s.BookingRef != ref {
		t.Fatalf("non-owner cancel must not mutate session: status=%s ref=%q", s.Status, s.BookingRef)
	}

	// Owner cancels → cancelled, booking released.
	dCan, codeCan := checkoutTool(testCfg, st, "L2", "agentA", "cancel_checkout", coArgs(map[string]any{"id": cid}))
	if codeCan != 200 || dCan["status"] != "cancelled" || dCan["cancelled"] != true {
		t.Fatalf("owner cancel: want 200 cancelled, got code=%d %v", codeCan, dCan)
	}
	if dCan["released_booking_ref"] != ref {
		t.Fatalf("cancel should report the released booking_ref %q, got %v", ref, dCan["released_booking_ref"])
	}
	if s := st.sessions[cid]; s.Status != "cancelled" || s.BookingRef != "" {
		t.Fatalf("cancel must release the booking: status=%s ref=%q", s.Status, s.BookingRef)
	}

	// Idempotent: cancelling again → safe success.
	d2, code2 := checkoutTool(testCfg, st, "L2", "agentA", "cancel_checkout", coArgs(map[string]any{"id": cid}))
	if code2 != 200 || d2["status"] != "cancelled" || d2["already"] != "cancelled" {
		t.Fatalf("idempotent re-cancel: want 200 cancelled already=cancelled, got code=%d %v", code2, d2)
	}

	// A cancelled checkout can NEVER be re-completed.
	d3, code3 := checkoutTool(testCfg, st, "L2", "agentA", "complete_checkout", coArgs(map[string]any{"id": cid}))
	if code3 != 409 || d3["reason"] != "checkout_cancelled" {
		t.Fatalf("re-complete after cancel: want 409 checkout_cancelled, got code=%d %v", code3, d3)
	}

	// Cancelling an unknown id is a safe idempotent success (the caller never has
	// to special-case a missing/superseded id).
	d4, code4 := checkoutTool(testCfg, st, "L2", "agentA", "cancel_checkout", coArgs(map[string]any{"id": "co_does_not_exist"}))
	if code4 != 200 || d4["cancelled"] != true || d4["already"] != "unknown" {
		t.Fatalf("cancel unknown id: want 200 cancelled already=unknown, got code=%d %v", code4, d4)
	}
}
