package main

import (
	"encoding/json"
	"sync"
	"testing"
)

// TestCounterpartyInsolvencyVoidsCheckout proves the N1 fault: a booking
// committed against an insolvent counterparty goes VOID (no booking_ref). This
// is the fault the society Fraud gate pre-empts. Distinct from sold-out
// (sim_set_availability) and flight-cancel (sim_set_transfer).
func TestCounterpartyInsolvencyVoidsCheckout(t *testing.T) {
	st := newStore()
	// Create + consent a normal session (a solvent counterparty completes fine).
	d, code := checkoutTool(testCfg, st, "L2", "a", "create_checkout",
		coArgs(map[string]any{"user_id": "u1", "line_items": []map[string]any{li("bali-alaya-ubud", "2026-07-01", "2026-07-03")}}))
	if code != 200 || d["status"] != "incomplete" {
		t.Fatalf("create: %v", d)
	}
	cid := d["id"].(string)
	checkoutTool(testCfg, st, "L2", "a", "update_checkout", coArgs(map[string]any{"id": cid, "buyer_consent": true}))

	// Flip the counterparty (counterparty_id == catalog_id == hotel_id) insolvent.
	st.simSetCounterparty("bali-alaya-ubud", false)
	if st.simCounterpartySolvent("bali-alaya-ubud") {
		t.Fatalf("counterparty should be insolvent after flip")
	}

	// complete_checkout must now VOID — no booking_ref, status:void.
	d, _ = checkoutTool(testCfg, st, "L2", "a", "complete_checkout", coArgs(map[string]any{"id": cid}))
	if d["status"] != "void" {
		t.Fatalf("want status void, got %v", d)
	}
	if d["reason"] != "counterparty_insolvent" {
		t.Fatalf("want reason counterparty_insolvent, got %v", d)
	}
	if _, hasRef := d["booking_ref"]; hasRef {
		t.Fatalf("VOID booking must not carry a booking_ref: %v", d)
	}
	if d["counterparty_id"] != "bali-alaya-ubud" {
		t.Fatalf("want counterparty_id echoed, got %v", d)
	}

	// Restore solvency → the same session now completes (the flip is reversible).
	st.simSetCounterparty("bali-alaya-ubud", true)
	d, _ = checkoutTool(testCfg, st, "L2", "a", "complete_checkout", coArgs(map[string]any{"id": cid}))
	if d["status"] != "complete" || d["booking_ref"] == "" {
		t.Fatalf("want complete+ref after solvency restored: %v", d)
	}
}

// TestCounterpartyInsolvencyCaseInsensitive proves the id is normalised (the
// §19.1 join key space is lower-cased/trimmed).
func TestCounterpartyInsolvencyCaseInsensitive(t *testing.T) {
	st := newStore()
	st.simSetCounterparty("  Bali-Alaya-Ubud  ", false)
	if st.simCounterpartySolvent("bali-alaya-ubud") {
		t.Fatalf("normalised id should be insolvent")
	}
}

// TestCounterpartyInsolvencyDistinctFromSoldOut proves the two fault classes are
// independent state: a sold-out hotel is not insolvent, and vice versa.
func TestCounterpartyInsolvencyDistinctFromSoldOut(t *testing.T) {
	st := newStore()
	st.simSetAvailability("bali-alaya-ubud", false) // sold-out
	if !st.simCounterpartySolvent("bali-alaya-ubud") {
		t.Fatalf("sold-out must NOT imply insolvent (distinct fault classes)")
	}
	st2 := newStore()
	st2.simSetCounterparty("bali-alaya-ubud", false) // insolvent
	if !st2.simIsAvailable("bali-alaya-ubud") {
		t.Fatalf("insolvent must NOT imply sold-out (distinct fault classes)")
	}
}

// TestSimCounterpartyMCP proves the MCP surface (sim_set_counterparty /
// sim_get_counterparties).
func TestSimCounterpartyMCP(t *testing.T) {
	st := newStore()
	args, _ := json.Marshal(map[string]any{"counterparty_id": "carrier-skylark-budget-air", "solvent": false})
	d, code := dispatchSimCounterpartyTool(st, toolCallParams{Name: "sim_set_counterparty", Arguments: args})
	if code != 200 || d["status"] != "ok" || d["solvent"] != false {
		t.Fatalf("sim_set_counterparty: %v (code %d)", d, code)
	}
	got := st.simListInsolventCounterparties()
	if len(got) != 1 || got[0] != "carrier-skylark-budget-air" {
		t.Fatalf("insolvent set: %v", got)
	}
	// Missing id → 400.
	bad, code := dispatchSimCounterpartyTool(st, toolCallParams{Arguments: json.RawMessage(`{}`)})
	if code != 400 || bad["error"] == nil {
		t.Fatalf("missing counterparty_id should 400: %v (code %d)", bad, code)
	}
}

// TestCounterpartyInsolvencyRaceClean exercises concurrent flips + reads under
// the race detector (go test -race) to prove the store lock covers the new map.
func TestCounterpartyInsolvencyRaceClean(t *testing.T) {
	st := newStore()
	var wg sync.WaitGroup
	for i := 0; i < 50; i++ {
		wg.Add(2)
		go func() { defer wg.Done(); st.simSetCounterparty("carrier-skylark-budget-air", false) }()
		go func() { defer wg.Done(); _ = st.simCounterpartySolvent("carrier-skylark-budget-air"); _ = st.simListInsolventCounterparties() }()
	}
	wg.Wait()
}
