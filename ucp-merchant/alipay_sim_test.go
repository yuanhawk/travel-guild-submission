package main

import (
	"encoding/json"
	"net/http/httptest"
	"strings"
	"testing"
)

// #57 AP2 simulated settlement: deterministic, idempotent, honestly labeled.
func TestAlipaySimSettleDeterministicAndIdempotent(t *testing.T) {
	st := newStore()

	r1 := st.alipaySettle("BR-ABC123", 120000)
	if r1.Status != "paid" {
		t.Fatalf("expected status paid, got %q", r1.Status)
	}
	if r1.TransactionID != alipaySimTxnID("BR-ABC123", 120000) {
		t.Fatalf("txn id not derived deterministically: %q", r1.TransactionID)
	}
	// var-0: the id is a pure function of (booking_ref,total_cents) — stable across calls.
	if alipaySimTxnID("BR-ABC123", 120000) != alipaySimTxnID("BR-ABC123", 120000) {
		t.Fatal("txn id not deterministic")
	}
	// idempotent: re-settling the same booking_ref returns the SAME record (no double-settle).
	r2 := st.alipaySettle("BR-ABC123", 120000)
	if r2 != r1 {
		t.Fatalf("settlement not idempotent: %+v != %+v", r2, r1)
	}

	// distinct inputs -> distinct ids.
	if st.alipaySettle("BR-XYZ789", 120000).TransactionID == r1.TransactionID {
		t.Fatal("different booking_ref produced the same txn id")
	}

	recs, total := st.alipaySummary()
	if len(recs) != 2 || total != 240000 {
		t.Fatalf("summary wrong: count=%d total=%d", len(recs), total)
	}
}

// Audit gap (#57): exercise the HTTP handler, not just the store method.
func TestAlipaySimAdminHandler(t *testing.T) {
	st := newStore()
	h := alipaySimAdminHandler(st)

	// POST settle -> 200, status paid, simulated:true, deterministic txn id.
	w := httptest.NewRecorder()
	h(w, httptest.NewRequest("POST", "/admin/sim/alipay/settle",
		strings.NewReader(`{"booking_ref":"BR-1","total_cents":120000}`)))
	if w.Code != 200 {
		t.Fatalf("settle HTTP %d", w.Code)
	}
	var resp map[string]any
	if err := json.Unmarshal(w.Body.Bytes(), &resp); err != nil {
		t.Fatalf("settle json: %v", err)
	}
	if resp["status"] != "paid" || resp["simulated"] != true || resp["transaction_id"] == "" {
		t.Fatalf("settle response wrong: %v", resp)
	}

	// GET summary -> reflects the settlement + carries the SIMULATED label.
	w2 := httptest.NewRecorder()
	h(w2, httptest.NewRequest("GET", "/admin/sim/alipay/settle", nil))
	var sum map[string]any
	if err := json.Unmarshal(w2.Body.Bytes(), &sum); err != nil {
		t.Fatalf("summary json: %v", err)
	}
	if sum["count"].(float64) != 1 || sum["total_settled_cents"].(float64) != 120000 || sum["simulated"] != true {
		t.Fatalf("summary response wrong: %v", sum)
	}

	// Malformed input -> 400 (no silent settle).
	w3 := httptest.NewRecorder()
	h(w3, httptest.NewRequest("POST", "/admin/sim/alipay/settle", strings.NewReader(`{"booking_ref":""}`)))
	if w3.Code != 400 {
		t.Fatalf("bad-input HTTP %d (want 400)", w3.Code)
	}
}
