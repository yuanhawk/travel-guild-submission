package main

import (
	"encoding/json"
	"testing"
)

// fli builds a FOOD_DELIVERY line item (food_id + optional qty), the way an MCP
// caller would. The hotel li() helper lives in checkout_test.go.
func fli(foodID string, qty int) map[string]any {
	m := map[string]any{"food_id": foodID}
	if qty > 0 {
		m["qty"] = qty
	}
	return m
}

// TestFoodCheckoutL2Flow — a FOOD_DELIVERY checkout rides the SAME rails as a
// hotel checkout: create → consent → complete, integer-cents total = price+fee.
func TestFoodCheckoutL2Flow(t *testing.T) {
	st := newStore()
	// tokyo ramen: 890 + 400 = 1290¢.
	d, code := checkoutTool(testCfg, st, "L2", "agentA", "create_checkout",
		coArgs(map[string]any{"user_id": "u1", "line_items": []map[string]any{fli("food-tokyo-ramen-midnight", 0)}}))
	if code != 200 || d["status"] != "incomplete" {
		t.Fatalf("create food checkout: %v", d)
	}
	if d["total_cents"].(int64) != 1290 {
		t.Fatalf("food total want 1290 got %v", d["total_cents"])
	}
	cid := d["id"].(string)
	checkoutTool(testCfg, st, "L2", "agentA", "update_checkout", coArgs(map[string]any{"id": cid, "buyer_consent": true}))
	d, _ = checkoutTool(testCfg, st, "L2", "agentA", "complete_checkout", coArgs(map[string]any{"id": cid}))
	if d["status"] != "complete" || d["booking_ref"] == "" {
		t.Fatalf("food checkout should complete with a ref: %v", d)
	}
}

// TestFoodCheckoutQtyMultiplies — Qty multiplies (price+fee) in integer cents.
func TestFoodCheckoutQtyMultiplies(t *testing.T) {
	st := newStore()
	// singapore laksa: 720 + 350 = 1070¢; ×3 = 3210¢.
	d, _ := checkoutTool(testCfg, st, "L2", "a", "create_checkout",
		coArgs(map[string]any{"user_id": "u1", "line_items": []map[string]any{fli("food-singapore-laksa-supper", 3)}}))
	if d["total_cents"].(int64) != 3210 {
		t.Fatalf("qty×(price+fee) want 3210 got %v", d["total_cents"])
	}
}

// TestFoodCheckoutBudgetVeto — the integer-cents budget veto applies to food
// EXACTLY as it does to hotels: a food total over the user budget is declined.
func TestFoodCheckoutBudgetVeto(t *testing.T) {
	st := newStore()
	// hanoi pho: 470 + 200 = 670¢; user_budget 500¢ → over budget.
	d, _ := checkoutTool(testCfg, st, "L2", "a", "create_checkout",
		coArgs(map[string]any{
			"user_id": "u1", "line_items": []map[string]any{fli("food-hanoi-pho-night", 0)},
			"user_budget_cents": int64(500),
		}))
	cid := d["id"].(string)
	// VULN-003: consent via update_checkout, not complete body.
	checkoutTool(testCfg, st, "L2", "a", "update_checkout", coArgs(map[string]any{"id": cid, "buyer_consent": true}))
	d, _ = checkoutTool(testCfg, st, "L2", "a", "complete_checkout", coArgs(map[string]any{"id": cid}))
	if d["reason"] != "price_exceeds_budget" {
		t.Fatalf("food over user_budget should be vetoed: %v", d)
	}
}

// TestFoodCheckoutUnknownItem — an unknown food_id fails closed at create (no
// fabricated price), exactly like an unknown hotel_id.
func TestFoodCheckoutUnknownItem(t *testing.T) {
	st := newStore()
	d, code := checkoutTool(testCfg, st, "L2", "a", "create_checkout",
		coArgs(map[string]any{"user_id": "u1", "line_items": []map[string]any{fli("food-not-real", 0)}}))
	if code == 200 && d["status"] == "incomplete" {
		t.Fatalf("unknown food_id must not create a priced checkout: %v", d)
	}
}

// TestMixedHotelAndFoodCheckout — a single checkout may carry BOTH a hotel line
// and a food line; the total is the integer-cents sum and it completes on the
// same rails. Proves the kind-agnostic enforcement core.
func TestMixedHotelAndFoodCheckout(t *testing.T) {
	st := newStore()
	// bali-alaya-ubud 2 nights = 33000¢ + tokyo ramen 1290¢ = 34290¢ (< 80000 ceiling).
	d, code := checkoutTool(testCfg, st, "L2", "a", "create_checkout",
		coArgs(map[string]any{"user_id": "u1", "line_items": []map[string]any{
			li("bali-alaya-ubud", "2026-07-01", "2026-07-03"),
			fli("food-tokyo-ramen-midnight", 0),
		}}))
	if code != 200 || d["total_cents"].(int64) != 34290 {
		t.Fatalf("mixed hotel+food total want 34290 got %v (code %d)", d["total_cents"], code)
	}
	cid := d["id"].(string)
	checkoutTool(testCfg, st, "L2", "a", "update_checkout", coArgs(map[string]any{"id": cid, "buyer_consent": true}))
	d, _ = checkoutTool(testCfg, st, "L2", "a", "complete_checkout", coArgs(map[string]any{"id": cid}))
	if d["status"] != "complete" {
		t.Fatalf("mixed checkout should complete: %v", d)
	}
}

// TestLodgingLineByteIdentity — var-0 GUARD for #44: adding the FOOD_DELIVERY
// kind must NOT change how a pure-lodging line item serializes. The food_id/qty
// fields are omitempty, so a hotel-only line marshals BYTE-IDENTICALLY to the
// pre-#44 shape. If someone drops omitempty (or reorders fields) this snapshot
// breaks loudly — protecting the deterministic byte-identical output thesis.
func TestLodgingLineByteIdentity(t *testing.T) {
	hotelOnly := lineItem{HotelID: "bali-alaya-ubud", Checkin: "2026-07-01", Checkout: "2026-07-03"}
	got, _ := json.Marshal(hotelOnly)
	const want = `{"hotel_id":"bali-alaya-ubud","checkin":"2026-07-01","checkout":"2026-07-03","adults":0}`
	if string(got) != want {
		t.Fatalf("lodging line serialization drifted after #44:\n got: %s\nwant: %s", got, want)
	}

	// And a food line carries food_id (+ qty when >1) but never empty hotel keys'
	// food fields — i.e. the two kinds are cleanly separable in the wire form.
	foodLine := lineItem{FoodID: "food-tokyo-ramen-midnight", Qty: 2}
	fgot, _ := json.Marshal(foodLine)
	const fwant = `{"hotel_id":"","checkin":"","checkout":"","adults":0,"food_id":"food-tokyo-ramen-midnight","qty":2}`
	if string(fgot) != fwant {
		t.Fatalf("food line serialization unexpected:\n got: %s\nwant: %s", fgot, fwant)
	}
}
