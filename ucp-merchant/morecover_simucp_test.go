package main

// morecover_simucp_test.go — Additional coverage for low-covered sim.go and
// ucp.go handlers. All test funcs named TestMCSim_* and all helpers prefixed
// mcsim_ to avoid any collision with existing package-level symbols.
//
// Targets:
//   sim.go:  simAdminHandler(139), simStatusHandler(185),
//            simTransferAdminHandler(244), simCounterpartyAdminHandler(343),
//            simListUnavailable(125), simCounterpartySolventLocked(320)
//   ucp.go:  ucpProfileHandler(57)
//
// Pattern: httptest.NewRecorder / httptest.NewRequest, table-driven, asserts
// both status codes AND concrete JSON body fields. No network to real hosts.

import (
	"crypto/ecdsa"
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

// ---------------------------------------------------------------------------
// helpers
// ---------------------------------------------------------------------------

// mcsim_newStore returns a clean store ready for sim tests.
func mcsim_newStore() *store { return newStore() }

// mcsim_body reads and JSON-decodes the response body; fails the test on error.
func mcsim_body(t *testing.T, resp *httptest.ResponseRecorder) map[string]any {
	t.Helper()
	var out map[string]any
	if err := json.NewDecoder(resp.Body).Decode(&out); err != nil {
		t.Fatalf("mcsim_body: decode failed: %v (raw: %q)", err, resp.Body.String())
	}
	return out
}

// mcsim_postJSON fires a POST with the given JSON string against an http.HandlerFunc.
func mcsim_postJSON(h http.HandlerFunc, body string) *httptest.ResponseRecorder {
	req := httptest.NewRequest(http.MethodPost, "/", strings.NewReader(body))
	req.Header.Set("Content-Type", "application/json")
	rr := httptest.NewRecorder()
	h(rr, req)
	return rr
}

// mcsim_getReq fires a GET against an http.HandlerFunc.
func mcsim_getReq(h http.HandlerFunc) *httptest.ResponseRecorder {
	req := httptest.NewRequest(http.MethodGet, "/", http.NoBody)
	rr := httptest.NewRecorder()
	h(rr, req)
	return rr
}

// mcsim_methodReq fires any HTTP method against an http.HandlerFunc.
func mcsim_methodReq(h http.HandlerFunc, method string) *httptest.ResponseRecorder {
	req := httptest.NewRequest(method, "/", http.NoBody)
	rr := httptest.NewRecorder()
	h(rr, req)
	return rr
}

// mcsim_assertStatus checks the response code and fails descriptively.
func mcsim_assertStatus(t *testing.T, rr *httptest.ResponseRecorder, want int) {
	t.Helper()
	if rr.Code != want {
		raw, _ := io.ReadAll(rr.Body)
		t.Fatalf("mcsim_assertStatus: want %d got %d; body=%q", want, rr.Code, raw)
	}
}

// mcsim_assertField checks that the decoded map contains the expected value.
func mcsim_assertField(t *testing.T, m map[string]any, key string, want any) {
	t.Helper()
	got, ok := m[key]
	if !ok {
		t.Fatalf("mcsim_assertField: key %q missing from %v", key, m)
	}
	// JSON numbers decode as float64; coerce bool/float64 comparisons.
	switch w := want.(type) {
	case bool:
		if gb, ok2 := got.(bool); !ok2 || gb != w {
			t.Fatalf("mcsim_assertField: key %q: want %v got %v", key, want, got)
		}
	default:
		if got != want {
			t.Fatalf("mcsim_assertField: key %q: want %v got %v", key, want, got)
		}
	}
}

// ---------------------------------------------------------------------------
// simAdminHandler — POST /admin/sim/availability
// ---------------------------------------------------------------------------

// TestMCSim_AdminHandlerMethodNotAllowed verifies that non-POST methods are
// rejected with 405 MethodNotAllowed.
func TestMCSim_AdminHandlerMethodNotAllowed(t *testing.T) {
	st := mcsim_newStore()
	h := simAdminHandler(st)

	for _, method := range []string{http.MethodGet, http.MethodPut, http.MethodDelete} {
		rr := mcsim_methodReq(h, method)
		mcsim_assertStatus(t, rr, http.StatusMethodNotAllowed)
		m := mcsim_body(t, rr)
		if m["error"] == nil {
			t.Fatalf("simAdminHandler %s: expected error field, got %v", method, m)
		}
	}
}

// TestMCSim_AdminHandlerBadJSON verifies that malformed JSON body is rejected
// with 400 and an error field.
func TestMCSim_AdminHandlerBadJSON(t *testing.T) {
	st := mcsim_newStore()
	h := simAdminHandler(st)

	cases := []struct {
		name string
		body string
	}{
		{"empty_body", ""},
		{"not_json", "not-json-at-all"},
		{"missing_hotel_id", `{"available": false}`},
		{"empty_hotel_id", `{"hotel_id": "", "available": false}`},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			rr := mcsim_postJSON(h, tc.body)
			mcsim_assertStatus(t, rr, http.StatusBadRequest)
			m := mcsim_body(t, rr)
			if m["error"] == nil {
				t.Fatalf("simAdminHandler %s: expected error field, got %v", tc.name, m)
			}
		})
	}
}

// TestMCSim_AdminHandlerHotelNotFound verifies that an unknown hotel_id returns
// 404 with error:"hotel_not_found".
func TestMCSim_AdminHandlerHotelNotFound(t *testing.T) {
	st := mcsim_newStore()
	h := simAdminHandler(st)

	rr := mcsim_postJSON(h, `{"hotel_id": "does-not-exist-xyz", "available": false}`)
	mcsim_assertStatus(t, rr, http.StatusNotFound)
	m := mcsim_body(t, rr)
	if m["error"] != "hotel_not_found" {
		t.Fatalf("expected hotel_not_found, got %v", m)
	}
	if m["hotel_id"] != "does-not-exist-xyz" {
		t.Fatalf("expected hotel_id echo, got %v", m)
	}
}

// TestMCSim_AdminHandlerFlipUnavailable verifies the happy path: a known hotel
// can be flipped unavailable and the response echoes the new state.
func TestMCSim_AdminHandlerFlipUnavailable(t *testing.T) {
	st := mcsim_newStore()
	h := simAdminHandler(st)

	// bali-legian-beach is in the embedded catalog.json.
	rr := mcsim_postJSON(h, `{"hotel_id": "bali-legian-beach", "available": false}`)
	mcsim_assertStatus(t, rr, http.StatusOK)
	m := mcsim_body(t, rr)
	mcsim_assertField(t, m, "hotel_id", "bali-legian-beach")
	mcsim_assertField(t, m, "available", false)
	mcsim_assertField(t, m, "status", "ok")

	// Confirm in-process state was actually mutated.
	if st.simIsAvailable("bali-legian-beach") {
		t.Fatal("store should mark hotel unavailable after admin flip")
	}
}

// TestMCSim_AdminHandlerRestoreAvailability verifies that available:true (or
// absent) flips the hotel back to available.
func TestMCSim_AdminHandlerRestoreAvailability(t *testing.T) {
	st := mcsim_newStore()
	h := simAdminHandler(st)

	// First mark unavailable.
	st.simSetAvailability("bali-alaya-ubud", false)

	// POST with available:true should restore.
	rr := mcsim_postJSON(h, `{"hotel_id": "bali-alaya-ubud", "available": true}`)
	mcsim_assertStatus(t, rr, http.StatusOK)
	m := mcsim_body(t, rr)
	mcsim_assertField(t, m, "available", true)
	mcsim_assertField(t, m, "status", "ok")

	if !st.simIsAvailable("bali-alaya-ubud") {
		t.Fatal("store should mark hotel available after restore")
	}
}

// TestMCSim_AdminHandlerDefaultAvailable verifies that omitting "available"
// defaults to true (restoration semantics).
func TestMCSim_AdminHandlerDefaultAvailable(t *testing.T) {
	st := mcsim_newStore()
	h := simAdminHandler(st)

	// Mark unavailable first.
	st.simSetAvailability("bali-alaya-ubud", false)

	// POST without "available" field.
	rr := mcsim_postJSON(h, `{"hotel_id": "bali-alaya-ubud"}`)
	mcsim_assertStatus(t, rr, http.StatusOK)
	m := mcsim_body(t, rr)
	mcsim_assertField(t, m, "available", true)

	if !st.simIsAvailable("bali-alaya-ubud") {
		t.Fatal("omitting 'available' should default to true (restore)")
	}
}

// ---------------------------------------------------------------------------
// simStatusHandler — GET /admin/sim/availability
// ---------------------------------------------------------------------------

// TestMCSim_StatusHandlerMethodNotAllowed verifies that POST is rejected.
func TestMCSim_StatusHandlerMethodNotAllowed(t *testing.T) {
	st := mcsim_newStore()
	h := simStatusHandler(st)

	rr := mcsim_methodReq(h, http.MethodPost)
	mcsim_assertStatus(t, rr, http.StatusMethodNotAllowed)
	m := mcsim_body(t, rr)
	if m["error"] == nil {
		t.Fatalf("simStatusHandler POST: expected error field, got %v", m)
	}
}

// TestMCSim_StatusHandlerEmptyList verifies GET returns an empty list when no
// hotels have been flipped.
func TestMCSim_StatusHandlerEmptyList(t *testing.T) {
	st := mcsim_newStore()
	h := simStatusHandler(st)

	rr := mcsim_getReq(h)
	mcsim_assertStatus(t, rr, http.StatusOK)
	m := mcsim_body(t, rr)
	hotels, ok := m["unavailable_hotels"]
	if !ok {
		t.Fatalf("simStatusHandler: missing unavailable_hotels key: %v", m)
	}
	list, _ := hotels.([]any)
	if len(list) != 0 {
		t.Fatalf("simStatusHandler: expected empty list, got %v", list)
	}
}

// TestMCSim_StatusHandlerListsUnavailable verifies GET returns the hotel(s)
// marked unavailable via simListUnavailable (exercising that function too).
func TestMCSim_StatusHandlerListsUnavailable(t *testing.T) {
	st := mcsim_newStore()
	h := simStatusHandler(st)

	// Mark two hotels unavailable.
	st.simSetAvailability("bali-legian-beach", false)
	st.simSetAvailability("bali-alaya-ubud", false)

	// simListUnavailable should return both.
	unavail := st.simListUnavailable()
	if len(unavail) != 2 {
		t.Fatalf("simListUnavailable: want 2, got %d: %v", len(unavail), unavail)
	}

	rr := mcsim_getReq(h)
	mcsim_assertStatus(t, rr, http.StatusOK)
	m := mcsim_body(t, rr)
	list, _ := m["unavailable_hotels"].([]any)
	if len(list) != 2 {
		t.Fatalf("simStatusHandler GET: want 2 unavailable hotels, got %d: %v", len(list), list)
	}

	// Restore one and verify the list shrinks.
	st.simSetAvailability("bali-alaya-ubud", true)
	rr2 := mcsim_getReq(h)
	m2 := mcsim_body(t, rr2)
	list2, _ := m2["unavailable_hotels"].([]any)
	if len(list2) != 1 {
		t.Fatalf("simStatusHandler GET after restore: want 1, got %d: %v", len(list2), list2)
	}
	if list2[0] != "bali-legian-beach" {
		t.Fatalf("remaining unavailable hotel should be bali-legian-beach, got %v", list2[0])
	}
}

// TestMCSim_SimListUnavailableEmpty verifies simListUnavailable on a fresh
// store returns an empty (non-nil) slice.
func TestMCSim_SimListUnavailableEmpty(t *testing.T) {
	st := mcsim_newStore()
	got := st.simListUnavailable()
	if got == nil {
		t.Fatal("simListUnavailable: should return non-nil slice on fresh store")
	}
	if len(got) != 0 {
		t.Fatalf("simListUnavailable: expected empty, got %v", got)
	}
}

// TestMCSim_SimListUnavailableMultiple adds several hotels and verifies all
// appear (order-independent) in the returned slice.
func TestMCSim_SimListUnavailableMultiple(t *testing.T) {
	st := mcsim_newStore()
	hotels := []string{"bali-legian-beach", "bali-alaya-ubud", "bali-alila-seminyak"}
	for _, h := range hotels {
		st.simSetAvailability(h, false)
	}
	got := st.simListUnavailable()
	if len(got) != len(hotels) {
		t.Fatalf("simListUnavailable: want %d, got %d: %v", len(hotels), len(got), got)
	}
	set := map[string]bool{}
	for _, id := range got {
		set[id] = true
	}
	for _, h := range hotels {
		if !set[h] {
			t.Fatalf("simListUnavailable: missing hotel %q", h)
		}
	}
}

// ---------------------------------------------------------------------------
// simTransferAdminHandler — POST/GET /admin/sim/transfer
// ---------------------------------------------------------------------------

// TestMCSim_TransferAdminHandlerMethodNotAllowed verifies that unexpected methods
// (PUT, DELETE, PATCH) return 405.
func TestMCSim_TransferAdminHandlerMethodNotAllowed(t *testing.T) {
	st := mcsim_newStore()
	h := simTransferAdminHandler(st)

	for _, method := range []string{http.MethodPut, http.MethodDelete, http.MethodPatch} {
		rr := mcsim_methodReq(h, method)
		mcsim_assertStatus(t, rr, http.StatusMethodNotAllowed)
		m := mcsim_body(t, rr)
		if m["error"] == nil {
			t.Fatalf("simTransferAdminHandler %s: expected error, got %v", method, m)
		}
	}
}

// TestMCSim_TransferAdminHandlerGETEmpty verifies GET returns empty cancelled list
// when no transfers have been flipped.
func TestMCSim_TransferAdminHandlerGETEmpty(t *testing.T) {
	st := mcsim_newStore()
	h := simTransferAdminHandler(st)

	rr := mcsim_getReq(h)
	mcsim_assertStatus(t, rr, http.StatusOK)
	m := mcsim_body(t, rr)
	list, ok := m["cancelled_transfers"]
	if !ok {
		t.Fatalf("simTransferAdminHandler GET: missing cancelled_transfers: %v", m)
	}
	if arr, _ := list.([]any); len(arr) != 0 {
		t.Fatalf("expected empty cancelled_transfers, got %v", arr)
	}
}

// TestMCSim_TransferAdminHandlerPostBadJSON verifies that malformed POST bodies
// or missing required fields return 400.
func TestMCSim_TransferAdminHandlerPostBadJSON(t *testing.T) {
	st := mcsim_newStore()
	h := simTransferAdminHandler(st)

	cases := []struct {
		name string
		body string
	}{
		{"empty_body", ""},
		{"not_json", "garbage"},
		{"missing_both_cities", `{"feasible": false}`},
		{"missing_to_city", `{"from_city": "gondar"}`},
		{"missing_from_city", `{"to_city": "lalibela"}`},
		{"empty_from_city", `{"from_city": "", "to_city": "lalibela"}`},
		{"empty_to_city", `{"from_city": "gondar", "to_city": ""}`},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			rr := mcsim_postJSON(h, tc.body)
			mcsim_assertStatus(t, rr, http.StatusBadRequest)
			m := mcsim_body(t, rr)
			if m["error"] == nil {
				t.Fatalf("simTransferAdminHandler %s: expected error, got %v", tc.name, m)
			}
		})
	}
}

// TestMCSim_TransferAdminHandlerPostCancel verifies that a valid POST
// cancels the transfer and the response is correct.
func TestMCSim_TransferAdminHandlerPostCancel(t *testing.T) {
	st := mcsim_newStore()
	h := simTransferAdminHandler(st)

	rr := mcsim_postJSON(h, `{"from_city": "gondar", "to_city": "lalibela", "feasible": false}`)
	mcsim_assertStatus(t, rr, http.StatusOK)
	m := mcsim_body(t, rr)
	mcsim_assertField(t, m, "from_city", "gondar")
	mcsim_assertField(t, m, "to_city", "lalibela")
	mcsim_assertField(t, m, "feasible", false)
	mcsim_assertField(t, m, "status", "ok")

	// Confirm the in-process state was mutated.
	if st.simTransferFeasible("gondar", "lalibela") {
		t.Fatal("transfer should be infeasible after admin POST cancel")
	}
}

// TestMCSim_TransferAdminHandlerPostRestore verifies that feasible:true restores
// a previously cancelled transfer.
func TestMCSim_TransferAdminHandlerPostRestore(t *testing.T) {
	st := mcsim_newStore()
	h := simTransferAdminHandler(st)

	// Cancel first.
	st.simSetTransfer("gondar", "lalibela", false)
	if st.simTransferFeasible("gondar", "lalibela") {
		t.Fatal("setup: should be infeasible")
	}

	rr := mcsim_postJSON(h, `{"from_city": "gondar", "to_city": "lalibela", "feasible": true}`)
	mcsim_assertStatus(t, rr, http.StatusOK)
	m := mcsim_body(t, rr)
	mcsim_assertField(t, m, "feasible", true)
	mcsim_assertField(t, m, "status", "ok")

	if !st.simTransferFeasible("gondar", "lalibela") {
		t.Fatal("transfer should be feasible after restore")
	}
}

// TestMCSim_TransferAdminHandlerDefaultFeasible verifies that omitting "feasible"
// defaults to true (restore semantics).
func TestMCSim_TransferAdminHandlerDefaultFeasible(t *testing.T) {
	st := mcsim_newStore()
	h := simTransferAdminHandler(st)

	// Cancel first.
	st.simSetTransfer("addis ababa", "semera", false)

	rr := mcsim_postJSON(h, `{"from_city": "addis ababa", "to_city": "semera"}`)
	mcsim_assertStatus(t, rr, http.StatusOK)
	m := mcsim_body(t, rr)
	mcsim_assertField(t, m, "feasible", true)

	if !st.simTransferFeasible("addis ababa", "semera") {
		t.Fatal("omitting 'feasible' should default to true")
	}
}

// TestMCSim_TransferAdminHandlerGETListsCancelled verifies that GET correctly
// lists cancelled transfers after a POST cancel.
func TestMCSim_TransferAdminHandlerGETListsCancelled(t *testing.T) {
	st := mcsim_newStore()
	h := simTransferAdminHandler(st)

	// Cancel two transfers directly.
	st.simSetTransfer("gondar", "lalibela", false)
	st.simSetTransfer("addis ababa", "semera", false)

	rr := mcsim_getReq(h)
	mcsim_assertStatus(t, rr, http.StatusOK)
	m := mcsim_body(t, rr)
	list, _ := m["cancelled_transfers"].([]any)
	if len(list) != 2 {
		t.Fatalf("simTransferAdminHandler GET: want 2 cancelled transfers, got %d: %v", len(list), list)
	}
}

// TestMCSim_TransferAdminHandlerDirectional verifies that cancelling from→to
// does NOT cancel to→from (the directional invariant).
func TestMCSim_TransferAdminHandlerDirectional(t *testing.T) {
	st := mcsim_newStore()
	h := simTransferAdminHandler(st)

	rr := mcsim_postJSON(h, `{"from_city": "gondar", "to_city": "lalibela", "feasible": false}`)
	mcsim_assertStatus(t, rr, http.StatusOK)

	// Reverse edge must still be feasible.
	if !st.simTransferFeasible("lalibela", "gondar") {
		t.Fatal("cancel gondar→lalibela must NOT cancel lalibela→gondar (directional)")
	}
}

// ---------------------------------------------------------------------------
// simCounterpartyAdminHandler — POST/GET /admin/sim/counterparty
// ---------------------------------------------------------------------------

// TestMCSim_CounterpartyAdminHandlerMethodNotAllowed verifies unexpected methods
// return 405.
func TestMCSim_CounterpartyAdminHandlerMethodNotAllowed(t *testing.T) {
	st := mcsim_newStore()
	h := simCounterpartyAdminHandler(st)

	for _, method := range []string{http.MethodPut, http.MethodDelete, http.MethodPatch} {
		rr := mcsim_methodReq(h, method)
		mcsim_assertStatus(t, rr, http.StatusMethodNotAllowed)
		m := mcsim_body(t, rr)
		if m["error"] == nil {
			t.Fatalf("simCounterpartyAdminHandler %s: expected error, got %v", method, m)
		}
	}
}

// TestMCSim_CounterpartyAdminHandlerGETEmpty verifies GET returns an empty list
// on a fresh store.
func TestMCSim_CounterpartyAdminHandlerGETEmpty(t *testing.T) {
	st := mcsim_newStore()
	h := simCounterpartyAdminHandler(st)

	rr := mcsim_getReq(h)
	mcsim_assertStatus(t, rr, http.StatusOK)
	m := mcsim_body(t, rr)
	list, ok := m["insolvent_counterparties"]
	if !ok {
		t.Fatalf("simCounterpartyAdminHandler GET: missing insolvent_counterparties: %v", m)
	}
	if arr, _ := list.([]any); len(arr) != 0 {
		t.Fatalf("expected empty insolvent_counterparties, got %v", arr)
	}
}

// TestMCSim_CounterpartyAdminHandlerPostBadInput verifies that missing or empty
// counterparty_id returns 400.
func TestMCSim_CounterpartyAdminHandlerPostBadInput(t *testing.T) {
	st := mcsim_newStore()
	h := simCounterpartyAdminHandler(st)

	cases := []struct {
		name string
		body string
	}{
		{"empty_body", ""},
		{"not_json", "!json"},
		{"missing_id", `{"solvent": false}`},
		{"empty_id", `{"counterparty_id": "", "solvent": false}`},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			rr := mcsim_postJSON(h, tc.body)
			mcsim_assertStatus(t, rr, http.StatusBadRequest)
			m := mcsim_body(t, rr)
			if m["error"] == nil {
				t.Fatalf("simCounterpartyAdminHandler %s: expected error, got %v", tc.name, m)
			}
		})
	}
}

// TestMCSim_CounterpartyAdminHandlerPostInsolvent verifies that a valid POST
// with solvent:false marks the counterparty insolvent and echoes the state.
func TestMCSim_CounterpartyAdminHandlerPostInsolvent(t *testing.T) {
	st := mcsim_newStore()
	h := simCounterpartyAdminHandler(st)

	rr := mcsim_postJSON(h, `{"counterparty_id": "carrier-skylark-budget-air", "solvent": false}`)
	mcsim_assertStatus(t, rr, http.StatusOK)
	m := mcsim_body(t, rr)
	mcsim_assertField(t, m, "counterparty_id", "carrier-skylark-budget-air")
	mcsim_assertField(t, m, "solvent", false)
	mcsim_assertField(t, m, "status", "ok")

	if st.simCounterpartySolvent("carrier-skylark-budget-air") {
		t.Fatal("counterparty should be insolvent after POST")
	}
}

// TestMCSim_CounterpartyAdminHandlerPostRestore verifies that solvent:true
// restores a previously insolvent counterparty.
func TestMCSim_CounterpartyAdminHandlerPostRestore(t *testing.T) {
	st := mcsim_newStore()
	h := simCounterpartyAdminHandler(st)

	// First make insolvent.
	st.simSetCounterparty("carrier-skylark-budget-air", false)
	if st.simCounterpartySolvent("carrier-skylark-budget-air") {
		t.Fatal("setup: should be insolvent")
	}

	rr := mcsim_postJSON(h, `{"counterparty_id": "carrier-skylark-budget-air", "solvent": true}`)
	mcsim_assertStatus(t, rr, http.StatusOK)
	m := mcsim_body(t, rr)
	mcsim_assertField(t, m, "solvent", true)
	mcsim_assertField(t, m, "status", "ok")

	if !st.simCounterpartySolvent("carrier-skylark-budget-air") {
		t.Fatal("counterparty should be solvent after restore")
	}
}

// TestMCSim_CounterpartyAdminHandlerDefaultSolvent verifies that omitting
// "solvent" defaults to true.
func TestMCSim_CounterpartyAdminHandlerDefaultSolvent(t *testing.T) {
	st := mcsim_newStore()
	h := simCounterpartyAdminHandler(st)

	// Make insolvent first.
	st.simSetCounterparty("carrier-skylark-budget-air", false)

	rr := mcsim_postJSON(h, `{"counterparty_id": "carrier-skylark-budget-air"}`)
	mcsim_assertStatus(t, rr, http.StatusOK)
	m := mcsim_body(t, rr)
	mcsim_assertField(t, m, "solvent", true)

	if !st.simCounterpartySolvent("carrier-skylark-budget-air") {
		t.Fatal("omitting 'solvent' should default to true")
	}
}

// TestMCSim_CounterpartyAdminHandlerGETListsInsolvent verifies that GET lists
// all insolvent counterparties after multiple POST flips.
func TestMCSim_CounterpartyAdminHandlerGETListsInsolvent(t *testing.T) {
	st := mcsim_newStore()
	h := simCounterpartyAdminHandler(st)

	st.simSetCounterparty("carrier-a", false)
	st.simSetCounterparty("carrier-b", false)

	rr := mcsim_getReq(h)
	mcsim_assertStatus(t, rr, http.StatusOK)
	m := mcsim_body(t, rr)
	list, _ := m["insolvent_counterparties"].([]any)
	if len(list) != 2 {
		t.Fatalf("simCounterpartyAdminHandler GET: want 2 insolvent, got %d: %v", len(list), list)
	}
}

// TestMCSim_CounterpartyAdminHandlerCaseNormalized verifies that the handler
// normalises counterparty_id case on POST (the §19.1 join-key invariant).
func TestMCSim_CounterpartyAdminHandlerCaseNormalized(t *testing.T) {
	st := mcsim_newStore()
	h := simCounterpartyAdminHandler(st)

	rr := mcsim_postJSON(h, `{"counterparty_id": "  Carrier-Mixed-Case  ", "solvent": false}`)
	mcsim_assertStatus(t, rr, http.StatusOK)

	// Lookup with lower-case normalised id must find it insolvent.
	if st.simCounterpartySolvent("carrier-mixed-case") {
		t.Fatal("counterparty should be insolvent: normalisation should have lowercased the id")
	}
}

// ---------------------------------------------------------------------------
// simCounterpartySolventLocked — the lock-free inner body
// ---------------------------------------------------------------------------

// TestMCSim_CounterpartySolventLockedFreshMap verifies that simCounterpartySolventLocked
// returns true when counterpartyInsolvent map is nil (fresh store field).
func TestMCSim_CounterpartySolventLockedFreshMap(t *testing.T) {
	// Build a store with a nil counterpartyInsolvent map to exercise the nil guard.
	st := &store{
		sessions:              map[string]*checkoutSession{},
		idempotencyMap:        map[string]string{},
		userKeys:              map[string]*ecdsa.PublicKey{},
		unavailable:           map[string]bool{},
		unavailableTransfers:  map[transferKey]bool{},
		counterpartyInsolvent: nil, // explicitly nil
		alipaySettlements:     map[string]alipaySettlement{},
	}
	// Caller holds the lock per contract; we hold it here for correctness.
	st.mu.Lock()
	solvent := st.simCounterpartySolventLocked("any-id")
	st.mu.Unlock()
	if !solvent {
		t.Fatal("simCounterpartySolventLocked: nil map should return true (default solvent)")
	}
}

// TestMCSim_CounterpartySolventLockedInsolvent verifies that simCounterpartySolventLocked
// returns false for a known-insolvent id (map present, entry set).
func TestMCSim_CounterpartySolventLockedInsolvent(t *testing.T) {
	st := mcsim_newStore()
	st.simSetCounterparty("hotel-insolvent", false)

	st.mu.Lock()
	solvent := st.simCounterpartySolventLocked("hotel-insolvent")
	st.mu.Unlock()

	if solvent {
		t.Fatal("simCounterpartySolventLocked: should return false for insolvent counterparty")
	}
}

// TestMCSim_CounterpartySolventLockedSolvent verifies that a solvent id returns
// true even when the map contains other insolvent entries.
func TestMCSim_CounterpartySolventLockedSolvent(t *testing.T) {
	st := mcsim_newStore()
	st.simSetCounterparty("carrier-bad", false)
	st.simSetCounterparty("carrier-good", true) // explicitly solvent

	st.mu.Lock()
	solvent := st.simCounterpartySolventLocked("carrier-good")
	st.mu.Unlock()

	if !solvent {
		t.Fatal("simCounterpartySolventLocked: carrier-good should be solvent")
	}
}

// TestMCSim_CounterpartySolventLockedCaseNormalized verifies that the lock-free
// body also normalises the key via normCounterparty.
func TestMCSim_CounterpartySolventLockedCaseNormalized(t *testing.T) {
	st := mcsim_newStore()
	st.simSetCounterparty("MixedCase-Carrier", false) // stored as "mixedcase-carrier"

	st.mu.Lock()
	solvent := st.simCounterpartySolventLocked("MIXEDCASE-CARRIER")
	st.mu.Unlock()

	if solvent {
		t.Fatal("simCounterpartySolventLocked: key normalisation should match regardless of input case")
	}
}

// ---------------------------------------------------------------------------
// ucpProfileHandler — GET /.well-known/ucp
// ---------------------------------------------------------------------------

// TestMCSim_UCPProfileHandlerShape verifies that the handler returns 200 with
// the mandatory "ucp" and "signing_keys" fields and that the capability
// manifest is populated.
func TestMCSim_UCPProfileHandlerShape(t *testing.T) {
	mk, _ := loadOrCreateKey()
	cfg := config{
		BaseURL:     "http://localhost:8090",
		DefaultTier: "L2",
		TierGrants:  map[string]string{},
	}
	h := ucpProfileHandler(cfg, mk)

	req := httptest.NewRequest(http.MethodGet, "/.well-known/ucp", http.NoBody)
	rr := httptest.NewRecorder()
	h(rr, req)

	mcsim_assertStatus(t, rr, http.StatusOK)
	m := mcsim_body(t, rr)

	// Top-level keys.
	if _, ok := m["ucp"]; !ok {
		t.Fatalf("ucpProfileHandler: missing 'ucp' key: %v", m)
	}
	if _, ok := m["signing_keys"]; !ok {
		t.Fatalf("ucpProfileHandler: missing 'signing_keys' key: %v", m)
	}

	ucpBlock, _ := m["ucp"].(map[string]any)
	if ucpBlock["version"] != ucpVersion {
		t.Fatalf("ucpProfileHandler: ucp.version should be %q, got %v", ucpVersion, ucpBlock["version"])
	}

	// services block.
	services, _ := ucpBlock["services"].(map[string]any)
	if _, ok := services["dev.ucp.shopping"]; !ok {
		t.Fatalf("ucpProfileHandler: missing dev.ucp.shopping in services: %v", services)
	}

	// capabilities block — should contain the canonical set.
	caps, _ := ucpBlock["capabilities"].(map[string]any)
	requiredCaps := []string{
		"dev.ucp.shopping.catalog",
		"dev.ucp.shopping.checkout",
		"dev.ucp.shopping.order",
		"dev.ucp.shopping.ap2_mandate",
		"dev.ucp.common.identity_linking",
	}
	for _, cap := range requiredCaps {
		if _, ok := caps[cap]; !ok {
			t.Fatalf("ucpProfileHandler: missing capability %q in manifest", cap)
		}
	}
}

// TestMCSim_UCPProfileHandlerSigningKeys verifies the signing_keys array
// contains at least one key with kty, crv, x, y, kid fields.
func TestMCSim_UCPProfileHandlerSigningKeys(t *testing.T) {
	mk, _ := loadOrCreateKey()
	cfg := config{BaseURL: "http://localhost:8090", TierGrants: map[string]string{}}
	h := ucpProfileHandler(cfg, mk)

	req := httptest.NewRequest(http.MethodGet, "/.well-known/ucp", http.NoBody)
	rr := httptest.NewRecorder()
	h(rr, req)

	m := mcsim_body(t, rr)
	keys, _ := m["signing_keys"].([]any)
	if len(keys) == 0 {
		t.Fatal("ucpProfileHandler: signing_keys must be non-empty")
	}
	key, _ := keys[0].(map[string]any)
	for _, field := range []string{"kty", "crv", "x", "y", "kid"} {
		if key[field] == nil || key[field] == "" {
			t.Fatalf("ucpProfileHandler: signing key missing or empty field %q: %v", field, key)
		}
	}
	if key["kty"] != "EC" {
		t.Fatalf("ucpProfileHandler: expected kty=EC, got %v", key["kty"])
	}
	if key["crv"] != "P-256" {
		t.Fatalf("ucpProfileHandler: expected crv=P-256, got %v", key["crv"])
	}
}

// TestMCSim_UCPProfileHandlerEndpointUsesBaseURL verifies that the MCP
// endpoint field in the profile reflects the configured BaseURL.
func TestMCSim_UCPProfileHandlerEndpointUsesBaseURL(t *testing.T) {
	mk, _ := loadOrCreateKey()
	baseURL := "https://merchant.example.com"
	cfg := config{BaseURL: baseURL, TierGrants: map[string]string{}}
	h := ucpProfileHandler(cfg, mk)

	req := httptest.NewRequest(http.MethodGet, "/.well-known/ucp", http.NoBody)
	rr := httptest.NewRecorder()
	h(rr, req)

	m := mcsim_body(t, rr)
	ucpBlock, _ := m["ucp"].(map[string]any)
	services, _ := ucpBlock["services"].(map[string]any)
	shoppingArr, _ := services["dev.ucp.shopping"].([]any)
	if len(shoppingArr) == 0 {
		t.Fatalf("ucpProfileHandler: dev.ucp.shopping should be non-empty array")
	}
	svc, _ := shoppingArr[0].(map[string]any)
	endpoint, _ := svc["endpoint"].(string)
	wantEndpoint := baseURL + "/api/ucp/mcp"
	if endpoint != wantEndpoint {
		t.Fatalf("ucpProfileHandler: endpoint = %q, want %q", endpoint, wantEndpoint)
	}
}

// TestMCSim_UCPProfileHandlerCapabilitiesHaveSpecAndSchema verifies that each
// capability entry in the manifest carries spec and schema URLs.
func TestMCSim_UCPProfileHandlerCapabilitiesHaveSpecAndSchema(t *testing.T) {
	mk, _ := loadOrCreateKey()
	cfg := config{BaseURL: "http://localhost:8090", TierGrants: map[string]string{}}
	h := ucpProfileHandler(cfg, mk)

	req := httptest.NewRequest(http.MethodGet, "/.well-known/ucp", http.NoBody)
	rr := httptest.NewRecorder()
	h(rr, req)

	m := mcsim_body(t, rr)
	ucpBlock, _ := m["ucp"].(map[string]any)
	caps, _ := ucpBlock["capabilities"].(map[string]any)
	if len(caps) == 0 {
		t.Fatal("ucpProfileHandler: capabilities should not be empty")
	}
	for capID, v := range caps {
		arr, _ := v.([]any)
		if len(arr) == 0 {
			t.Fatalf("ucpProfileHandler: capability %q should have at least one entry", capID)
		}
		entry, _ := arr[0].(map[string]any)
		if entry["spec"] == nil || entry["spec"] == "" {
			t.Fatalf("ucpProfileHandler: capability %q missing spec URL", capID)
		}
		if entry["schema"] == nil || entry["schema"] == "" {
			t.Fatalf("ucpProfileHandler: capability %q missing schema URL", capID)
		}
		if entry["version"] != ucpVersion {
			t.Fatalf("ucpProfileHandler: capability %q version = %v, want %q", capID, entry["version"], ucpVersion)
		}
	}
}

// TestMCSim_UCPProfileHandlerAP2MandateExtendsCheckout verifies the special
// "extends" relationship: ap2_mandate extends checkout.
func TestMCSim_UCPProfileHandlerAP2MandateExtendsCheckout(t *testing.T) {
	mk, _ := loadOrCreateKey()
	cfg := config{BaseURL: "http://localhost:8090", TierGrants: map[string]string{}}
	h := ucpProfileHandler(cfg, mk)

	req := httptest.NewRequest(http.MethodGet, "/.well-known/ucp", http.NoBody)
	rr := httptest.NewRecorder()
	h(rr, req)

	m := mcsim_body(t, rr)
	ucpBlock, _ := m["ucp"].(map[string]any)
	caps, _ := ucpBlock["capabilities"].(map[string]any)
	ap2, _ := caps["dev.ucp.shopping.ap2_mandate"].([]any)
	if len(ap2) == 0 {
		t.Fatal("ucpProfileHandler: dev.ucp.shopping.ap2_mandate missing from capabilities")
	}
	entry, _ := ap2[0].(map[string]any)
	ext, _ := entry["extends"].(string)
	if ext != "dev.ucp.shopping.checkout" {
		t.Fatalf("ucpProfileHandler: ap2_mandate extends = %q, want dev.ucp.shopping.checkout", ext)
	}
}

// TestMCSim_UCPProfileHandlerContentType verifies the response Content-Type
// is application/json.
func TestMCSim_UCPProfileHandlerContentType(t *testing.T) {
	mk, _ := loadOrCreateKey()
	cfg := config{BaseURL: "http://localhost:8090", TierGrants: map[string]string{}}
	h := ucpProfileHandler(cfg, mk)

	req := httptest.NewRequest(http.MethodGet, "/.well-known/ucp", http.NoBody)
	rr := httptest.NewRecorder()
	h(rr, req)

	ct := rr.Header().Get("Content-Type")
	if !strings.HasPrefix(ct, "application/json") {
		t.Fatalf("ucpProfileHandler: Content-Type = %q, want application/json", ct)
	}
}
