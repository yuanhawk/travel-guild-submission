package main

// morecover_mcp_test.go — table-driven coverage for low-coverage functions in mcp.go:
//   dispatchSimTool (line 110), dispatchTool (line 208), mcpErr (line 259),
//   toolSchemas (line 270), rawOrNull (line 263).
//
// All test funcs: TestMCMcp_... All helpers/vars: mcmcp_...
// Uses net/http/httptest in-process; no network to real hosts; deterministic.

import (
	"bytes"
	"crypto/ecdsa"
	"encoding/json"
	"errors"
	"net/http"
	"net/http/httptest"
	"testing"
)

// mcmcp_noopResolver is a KeyResolver that always errors (no real network).
func mcmcp_noopResolver(_ string, _ string) (map[string]*ecdsa.PublicKey, []string, error) {
	return nil, nil, errors.New("none")
}

// mcmcp_newUnsignedServer builds a test httptest server with UnsignedTier=L2
// (no signatures required), suitable for plain tool-dispatch tests.
func mcmcp_newUnsignedServer(t *testing.T) *httptest.Server {
	t.Helper()
	cfg := config{
		BudgetCeilingCents: 80000, BudgetHardMaxCents: 200000,
		DefaultTier: "L2", UnsignedTier: "L2",
		TierGrants: map[string]string{},
	}
	return httptest.NewServer(mcpHandler(cfg, newStore(), newSigVerifier(mcmcp_noopResolver)))
}

// mcmcp_postJSON sends a JSON body to /api/ucp/mcp on the given server
// and returns the HTTP status code + decoded top-level JSON map.
func mcmcp_postJSON(t *testing.T, srv *httptest.Server, body []byte) (int, map[string]any) {
	t.Helper()
	resp, err := http.Post(srv.URL+"/api/ucp/mcp", "application/json", bytes.NewReader(body))
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	var out map[string]any
	if err := json.NewDecoder(resp.Body).Decode(&out); err != nil {
		t.Fatal(err)
	}
	return resp.StatusCode, out
}

// ---------------------------------------------------------------------------
// rawOrNull
// ---------------------------------------------------------------------------

func TestMCMcp_RawOrNull_EmptyReturnsNil(t *testing.T) {
	if rawOrNull(nil) != nil {
		t.Fatal("nil RawMessage must return nil")
	}
	if rawOrNull(json.RawMessage{}) != nil {
		t.Fatal("empty RawMessage must return nil")
	}
}

func TestMCMcp_RawOrNull_NonEmptyReturnsValue(t *testing.T) {
	raw := json.RawMessage(`42`)
	got := rawOrNull(raw)
	if got == nil {
		t.Fatal("non-empty RawMessage must NOT return nil")
	}
	b, ok := got.(json.RawMessage)
	if !ok {
		t.Fatalf("expected json.RawMessage back, got %T", got)
	}
	if string(b) != `42` {
		t.Fatalf("expected `42`, got %s", b)
	}
}

func TestMCMcp_RawOrNull_StringIDRoundtrips(t *testing.T) {
	raw := json.RawMessage(`"req-1"`)
	if rawOrNull(raw) == nil {
		t.Fatal("string id must be non-nil")
	}
}

// ---------------------------------------------------------------------------
// mcpErr — envelope shape
// ---------------------------------------------------------------------------

func TestMCMcp_McpErr_ShapeWithNilID(t *testing.T) {
	env := mcpErr(nil, -32600, "POST required")
	if env["jsonrpc"] != "2.0" {
		t.Fatalf("jsonrpc must be 2.0, got %v", env["jsonrpc"])
	}
	if env["id"] != nil {
		t.Fatalf("id must be nil when no id provided, got %v", env["id"])
	}
	errObj, ok := env["error"].(map[string]any)
	if !ok {
		t.Fatalf("error field must be a map, got %T", env["error"])
	}
	if errObj["code"] != -32600 {
		t.Fatalf("code must be -32600, got %v", errObj["code"])
	}
	if errObj["message"] != "POST required" {
		t.Fatalf("message must be 'POST required', got %v", errObj["message"])
	}
}

func TestMCMcp_McpErr_ShapeWithID(t *testing.T) {
	id := json.RawMessage(`99`)
	env := mcpErr(id, -32601, "method not found")
	if env["id"] == nil {
		t.Fatal("id must not be nil when an id is supplied")
	}
	errObj, ok := env["error"].(map[string]any)
	if !ok {
		t.Fatalf("error must be a map, got %T", env["error"])
	}
	if errObj["code"] != -32601 {
		t.Fatalf("expected code -32601, got %v", errObj["code"])
	}
	if errObj["message"] != "method not found" {
		t.Fatalf("expected message 'method not found', got %v", errObj["message"])
	}
}

func TestMCMcp_McpErr_TableDriven(t *testing.T) {
	cases := []struct {
		label   string
		id      json.RawMessage
		code    int
		message string
	}{
		{"nil id parse error", nil, -32700, "parse error"},
		{"nil id method not found", nil, -32601, "method not found"},
		{"id=1 sig rejected", json.RawMessage(`1`), -32001, "signature rejected: bad sig"},
		{"id=null explicit", json.RawMessage(`null`), -32600, "POST required"},
	}
	for _, tc := range cases {
		t.Run(tc.label, func(t *testing.T) {
			env := mcpErr(tc.id, tc.code, tc.message)
			if env["jsonrpc"] != "2.0" {
				t.Errorf("jsonrpc must be 2.0")
			}
			errObj, ok := env["error"].(map[string]any)
			if !ok {
				t.Fatalf("error must be a map")
			}
			if errObj["code"] != tc.code {
				t.Errorf("code: want %d got %v", tc.code, errObj["code"])
			}
			if errObj["message"] != tc.message {
				t.Errorf("message: want %q got %v", tc.message, errObj["message"])
			}
		})
	}
}

// ---------------------------------------------------------------------------
// toolSchemas — enumeration
// ---------------------------------------------------------------------------

// mcmcp_expectedToolNames lists every tool name the schema must expose.
var mcmcp_expectedToolNames = []string{
	"sim_set_availability",
	"sim_set_transfer",
	"sim_get_transfers",
	"sim_set_counterparty",
	"sim_get_counterparties",
	"search_catalog",
	"lookup_catalog",
	"create_checkout",
	"update_checkout",
	"complete_checkout",
	"cancel_checkout",
	"wallet_fund",
	"wallet_get",
}

func TestMCMcp_ToolSchemas_ContainsAllExpectedTools(t *testing.T) {
	schemas := toolSchemas()
	index := map[string]map[string]any{}
	for _, s := range schemas {
		name, _ := s["name"].(string)
		index[name] = s
	}
	for _, want := range mcmcp_expectedToolNames {
		if _, ok := index[want]; !ok {
			t.Errorf("toolSchemas missing tool %q", want)
		}
	}
}

func TestMCMcp_ToolSchemas_EachEntryHasNameDescriptionInputSchema(t *testing.T) {
	for _, s := range toolSchemas() {
		name, ok := s["name"].(string)
		if !ok || name == "" {
			t.Errorf("schema entry missing string name: %v", s)
			continue
		}
		if desc, _ := s["description"].(string); desc == "" {
			t.Errorf("tool %q has empty description", name)
		}
		if is := s["inputSchema"]; is == nil {
			t.Errorf("tool %q missing inputSchema", name)
		}
	}
}

func TestMCMcp_ToolSchemas_SimSetAvailabilityRequiredFields(t *testing.T) {
	var found map[string]any
	for _, s := range toolSchemas() {
		if s["name"] == "sim_set_availability" {
			found = s
			break
		}
	}
	if found == nil {
		t.Fatal("sim_set_availability not found in toolSchemas")
	}
	is, ok := found["inputSchema"].(map[string]any)
	if !ok {
		t.Fatal("inputSchema not a map")
	}
	// required is []string
	required, _ := is["required"].([]string)
	reqSet := map[string]bool{}
	for _, r := range required {
		reqSet[r] = true
	}
	if !reqSet["hotel_id"] {
		t.Error("sim_set_availability inputSchema.required must include hotel_id")
	}
	if !reqSet["available"] {
		t.Error("sim_set_availability inputSchema.required must include available")
	}
}

func TestMCMcp_ToolSchemas_Count(t *testing.T) {
	schemas := toolSchemas()
	if len(schemas) != len(mcmcp_expectedToolNames) {
		t.Errorf("toolSchemas: expected %d schemas, got %d", len(mcmcp_expectedToolNames), len(schemas))
	}
}

// ---------------------------------------------------------------------------
// dispatchSimTool — hotel availability MCP dispatch
// ---------------------------------------------------------------------------

func TestMCMcp_DispatchSimTool_MissingHotelID(t *testing.T) {
	st := newStore()
	cases := []struct {
		label string
		args  json.RawMessage
	}{
		{"empty object", json.RawMessage(`{}`)},
		{"null", json.RawMessage(`null`)},
		{"hotel_id blank", json.RawMessage(`{"hotel_id":""}`)},
	}
	for _, tc := range cases {
		p := toolCallParams{Name: "sim_set_availability", Arguments: tc.args}
		d, code := dispatchSimTool(st, p)
		if code != http.StatusBadRequest {
			t.Errorf("[%s] want 400, got %d: %v", tc.label, code, d)
		}
		if d["error"] == nil {
			t.Errorf("[%s] want error field in response, got %v", tc.label, d)
		}
	}
}

func TestMCMcp_DispatchSimTool_UnknownHotelID(t *testing.T) {
	st := newStore()
	args, _ := json.Marshal(map[string]any{"hotel_id": "does-not-exist-xyz", "available": false})
	d, code := dispatchSimTool(st, toolCallParams{Name: "sim_set_availability", Arguments: args})
	if code != http.StatusNotFound {
		t.Fatalf("unknown hotel_id must return 404, got %d: %v", code, d)
	}
	if d["error"] != "hotel_not_found" {
		t.Fatalf("expected error=hotel_not_found, got %v", d)
	}
	if d["hotel_id"] != "does-not-exist-xyz" {
		t.Fatalf("hotel_id must be echoed in error, got %v", d)
	}
}

func TestMCMcp_DispatchSimTool_SetUnavailableAndRestore(t *testing.T) {
	st := newStore()
	const hotel = "bali-alaya-ubud"

	// Mark unavailable.
	args, _ := json.Marshal(map[string]any{"hotel_id": hotel, "available": false})
	d, code := dispatchSimTool(st, toolCallParams{Name: "sim_set_availability", Arguments: args})
	if code != http.StatusOK {
		t.Fatalf("set unavailable: want 200, got %d: %v", code, d)
	}
	if d["available"] != false {
		t.Fatalf("available should be false, got %v", d["available"])
	}
	if d["status"] != "ok" {
		t.Fatalf("status should be ok, got %v", d["status"])
	}
	if d["hotel_id"] != hotel {
		t.Fatalf("hotel_id should be echoed, got %v", d["hotel_id"])
	}
	if st.simIsAvailable(hotel) {
		t.Fatal("store: hotel should be unavailable after dispatchSimTool")
	}

	// Restore.
	args2, _ := json.Marshal(map[string]any{"hotel_id": hotel, "available": true})
	d2, code2 := dispatchSimTool(st, toolCallParams{Name: "sim_set_availability", Arguments: args2})
	if code2 != http.StatusOK || d2["available"] != true {
		t.Fatalf("restore: want 200 + available=true, got %d: %v", code2, d2)
	}
	if !st.simIsAvailable(hotel) {
		t.Fatal("store: hotel should be available after restore via dispatchSimTool")
	}
}

func TestMCMcp_DispatchSimTool_AbsentAvailableDefaultsTrue(t *testing.T) {
	// When 'available' key is absent, the default must be true (restore path).
	st := newStore()
	const hotel = "bali-alaya-ubud"
	// First mark unavailable directly.
	st.simSetAvailability(hotel, false)

	// Call dispatchSimTool with no 'available' field → should default to true.
	args, _ := json.Marshal(map[string]any{"hotel_id": hotel})
	d, code := dispatchSimTool(st, toolCallParams{Name: "sim_set_availability", Arguments: args})
	if code != http.StatusOK {
		t.Fatalf("absent available: want 200, got %d: %v", code, d)
	}
	if d["available"] != true {
		t.Fatalf("absent available should default to true, got %v", d["available"])
	}
	if !st.simIsAvailable(hotel) {
		t.Fatal("hotel should be available after absent-available restore")
	}
}

func TestMCMcp_DispatchSimTool_TableDriven(t *testing.T) {
	const hotel = "bali-alaya-ubud"
	cases := []struct {
		label    string
		args     map[string]any
		wantCode int
		wantErr  bool
	}{
		{
			label:    "valid set unavailable",
			args:     map[string]any{"hotel_id": hotel, "available": false},
			wantCode: http.StatusOK,
		},
		{
			label:    "valid set available",
			args:     map[string]any{"hotel_id": hotel, "available": true},
			wantCode: http.StatusOK,
		},
		{
			label:    "missing hotel_id → 400",
			args:     map[string]any{"available": false},
			wantCode: http.StatusBadRequest,
			wantErr:  true,
		},
		{
			label:    "unknown hotel_id → 404",
			args:     map[string]any{"hotel_id": "fantasy-hotel-99", "available": false},
			wantCode: http.StatusNotFound,
			wantErr:  true,
		},
	}
	for _, tc := range cases {
		t.Run(tc.label, func(t *testing.T) {
			st := newStore()
			args, _ := json.Marshal(tc.args)
			d, code := dispatchSimTool(st, toolCallParams{Name: "sim_set_availability", Arguments: args})
			if code != tc.wantCode {
				t.Fatalf("want %d got %d: %v", tc.wantCode, code, d)
			}
			if tc.wantErr && d["error"] == nil {
				t.Fatalf("expected error field in response, got %v", d)
			}
			if !tc.wantErr && d["status"] != "ok" {
				t.Fatalf("expected status=ok, got %v", d)
			}
		})
	}
}

// ---------------------------------------------------------------------------
// dispatchTool — main MCP tool router
// ---------------------------------------------------------------------------

func TestMCMcp_DispatchTool_UnknownToolReturns400(t *testing.T) {
	st := newStore()
	cfg := config{BudgetCeilingCents: 80000, BudgetHardMaxCents: 200000, DefaultTier: "L2"}
	unknowns := []string{"fly_me_to_the_moon", "admin_override", "sim_nuke_everything", "totally_bogus"}
	for _, name := range unknowns {
		p := toolCallParams{Name: name, Arguments: json.RawMessage(`{}`)}
		d, code := dispatchTool(cfg, st, "L2", "agentX", p)
		if code != http.StatusBadRequest {
			t.Errorf("unknown tool %q: want 400, got %d: %v", name, code, d)
		}
		errStr, _ := d["error"].(string)
		if errStr == "" {
			t.Errorf("unknown tool %q: want non-empty error string, got %v", name, d)
		}
	}
}

func TestMCMcp_DispatchTool_SimSetAvailabilityRoute(t *testing.T) {
	st := newStore()
	cfg := config{BudgetCeilingCents: 80000, BudgetHardMaxCents: 200000, DefaultTier: "L2"}
	args, _ := json.Marshal(map[string]any{"hotel_id": "bali-alaya-ubud", "available": false})
	d, code := dispatchTool(cfg, st, "L2", "agent1",
		toolCallParams{Name: "sim_set_availability", Arguments: args})
	if code != http.StatusOK || d["status"] != "ok" {
		t.Fatalf("sim_set_availability via dispatchTool: got code=%d %v", code, d)
	}
	if st.simIsAvailable("bali-alaya-ubud") {
		t.Fatal("hotel should be unavailable after dispatchTool sim_set_availability")
	}
}

func TestMCMcp_DispatchTool_SimSetTransferRoute(t *testing.T) {
	st := newStore()
	cfg := config{BudgetCeilingCents: 80000, BudgetHardMaxCents: 200000, DefaultTier: "L2"}
	args, _ := json.Marshal(map[string]any{"from_city": "Singapore", "to_city": "Bali", "feasible": false})
	d, code := dispatchTool(cfg, st, "L2", "agent1",
		toolCallParams{Name: "sim_set_transfer", Arguments: args})
	if code != http.StatusOK || d["status"] != "ok" {
		t.Fatalf("sim_set_transfer via dispatchTool: code=%d %v", code, d)
	}
	if st.simTransferFeasible("singapore", "bali") {
		t.Fatal("transfer should be infeasible after sim_set_transfer(feasible=false)")
	}
}

func TestMCMcp_DispatchTool_SimGetTransfersRoute(t *testing.T) {
	st := newStore()
	cfg := config{BudgetCeilingCents: 80000, BudgetHardMaxCents: 200000, DefaultTier: "L2"}
	st.simSetTransfer("Kuala Lumpur", "Jakarta", false)
	d, code := dispatchTool(cfg, st, "L2", "agent1",
		toolCallParams{Name: "sim_get_transfers", Arguments: json.RawMessage(`{}`)})
	if code != http.StatusOK {
		t.Fatalf("sim_get_transfers: want 200, got %d: %v", code, d)
	}
	ct, ok := d["cancelled_transfers"]
	if !ok {
		t.Fatalf("sim_get_transfers: missing cancelled_transfers key: %v", d)
	}
	list, _ := ct.([]map[string]string)
	if len(list) != 1 {
		t.Fatalf("sim_get_transfers: expected 1 cancelled transfer, got %d: %v", len(list), list)
	}
}

func TestMCMcp_DispatchTool_SimSetCounterpartyRoute(t *testing.T) {
	st := newStore()
	cfg := config{BudgetCeilingCents: 80000, BudgetHardMaxCents: 200000, DefaultTier: "L2"}
	args, _ := json.Marshal(map[string]any{"counterparty_id": "carrier-skylark-budget-air", "solvent": false})
	d, code := dispatchTool(cfg, st, "L2", "agent1",
		toolCallParams{Name: "sim_set_counterparty", Arguments: args})
	if code != http.StatusOK || d["status"] != "ok" {
		t.Fatalf("sim_set_counterparty via dispatchTool: code=%d %v", code, d)
	}
	if st.simCounterpartySolvent("carrier-skylark-budget-air") {
		t.Fatal("counterparty should be insolvent after sim_set_counterparty(solvent=false)")
	}
}

func TestMCMcp_DispatchTool_SimGetCounterpartiesRoute(t *testing.T) {
	st := newStore()
	cfg := config{BudgetCeilingCents: 80000, BudgetHardMaxCents: 200000, DefaultTier: "L2"}
	st.simSetCounterparty("carrier-bad-air", false)
	d, code := dispatchTool(cfg, st, "L2", "agent1",
		toolCallParams{Name: "sim_get_counterparties", Arguments: json.RawMessage(`{}`)})
	if code != http.StatusOK {
		t.Fatalf("sim_get_counterparties: want 200, got %d: %v", code, d)
	}
	ic, ok := d["insolvent_counterparties"]
	if !ok {
		t.Fatalf("sim_get_counterparties: missing insolvent_counterparties key: %v", d)
	}
	list, _ := ic.([]string)
	if len(list) != 1 || list[0] != "carrier-bad-air" {
		t.Fatalf("sim_get_counterparties: expected [carrier-bad-air], got %v", list)
	}
}

func TestMCMcp_DispatchTool_SearchCatalogRoute(t *testing.T) {
	st := newStore()
	cfg := config{BudgetCeilingCents: 80000, BudgetHardMaxCents: 200000, DefaultTier: "L2"}
	args, _ := json.Marshal(map[string]any{"query": map[string]any{
		"city": "bali", "checkin": "2026-07-01", "checkout": "2026-07-03",
	}})
	d, code := dispatchTool(cfg, st, "L1", "agent1",
		toolCallParams{Name: "search_catalog", Arguments: args})
	if code != http.StatusOK {
		t.Fatalf("search_catalog via dispatchTool: code=%d %v", code, d)
	}
	if _, ok := d["results"]; !ok {
		t.Fatalf("search_catalog: missing results key: %v", d)
	}
}

func TestMCMcp_DispatchTool_CheckoutToolRoute(t *testing.T) {
	st := newStore()
	cfg := config{BudgetCeilingCents: 80000, BudgetHardMaxCents: 200000, DefaultTier: "L2"}
	args, _ := json.Marshal(map[string]any{"checkout": map[string]any{
		"user_id": "u1",
		"line_items": []map[string]any{
			{"hotel_id": "bali-alaya-ubud", "checkin": "2026-07-01", "checkout": "2026-07-03"},
		},
	}})
	d, code := dispatchTool(cfg, st, "L2", "agent1",
		toolCallParams{Name: "create_checkout", Arguments: args})
	if code != http.StatusOK {
		t.Fatalf("create_checkout via dispatchTool: code=%d %v", code, d)
	}
	if d["status"] != "incomplete" {
		t.Fatalf("create_checkout: expected status=incomplete, got %v", d["status"])
	}
}

func TestMCMcp_DispatchTool_SimSetAvailabilityMissingHotelID(t *testing.T) {
	// dispatchTool routes to dispatchSimTool which returns 400 for missing hotel_id.
	st := newStore()
	cfg := config{BudgetCeilingCents: 80000, BudgetHardMaxCents: 200000, DefaultTier: "L2"}
	d, code := dispatchTool(cfg, st, "L2", "agent1",
		toolCallParams{Name: "sim_set_availability", Arguments: json.RawMessage(`{}`)})
	if code != http.StatusBadRequest {
		t.Fatalf("sim_set_availability with missing hotel_id: want 400, got %d: %v", code, d)
	}
}

func TestMCMcp_DispatchTool_TableDriven(t *testing.T) {
	cfg := config{BudgetCeilingCents: 80000, BudgetHardMaxCents: 200000, DefaultTier: "L2"}

	cases := []struct {
		label    string
		toolName string
		args     map[string]any
		wantCode int
		wantKey  string
	}{
		{
			label:    "search_catalog hotel path",
			toolName: "search_catalog",
			args:     map[string]any{"query": map[string]any{"city": "bali", "checkin": "2026-07-01", "checkout": "2026-07-03"}},
			wantCode: http.StatusOK,
			wantKey:  "results",
		},
		{
			label:    "lookup_catalog hotel path",
			toolName: "lookup_catalog",
			args:     map[string]any{"product": map[string]any{"hotel_id": "bali-alaya-ubud", "checkin": "2026-07-01", "checkout": "2026-07-03"}},
			wantCode: http.StatusOK,
			wantKey:  "hotel", // lookup_catalog success returns the matched hotel under "hotel" (nested), not a top-level hotel_id
		},
		{
			label:    "sim_get_transfers empty",
			toolName: "sim_get_transfers",
			args:     map[string]any{},
			wantCode: http.StatusOK,
			wantKey:  "cancelled_transfers",
		},
		{
			label:    "sim_get_counterparties empty",
			toolName: "sim_get_counterparties",
			args:     map[string]any{},
			wantCode: http.StatusOK,
			wantKey:  "insolvent_counterparties",
		},
		{
			label:    "unknown tool → error",
			toolName: "bogus_tool_xyz",
			args:     map[string]any{},
			wantCode: http.StatusBadRequest,
			wantKey:  "error",
		},
	}
	for _, tc := range cases {
		t.Run(tc.label, func(t *testing.T) {
			st := newStore()
			args, _ := json.Marshal(tc.args)
			p := toolCallParams{Name: tc.toolName, Arguments: args}
			d, code := dispatchTool(cfg, st, "L2", "agent1", p)
			if code != tc.wantCode {
				t.Fatalf("want code %d, got %d: %v", tc.wantCode, code, d)
			}
			if tc.wantKey != "" {
				if _, ok := d[tc.wantKey]; !ok {
					t.Fatalf("expected key %q in result, got %v", tc.wantKey, d)
				}
			}
		})
	}
}

// ---------------------------------------------------------------------------
// MCP HTTP handler surface — via httptest (house pattern)
// ---------------------------------------------------------------------------

func TestMCMcp_Handler_MethodNotAllowed(t *testing.T) {
	srv := mcmcp_newUnsignedServer(t)
	defer srv.Close()

	resp, err := http.Get(srv.URL + "/api/ucp/mcp")
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusMethodNotAllowed {
		t.Fatalf("GET must return 405, got %d", resp.StatusCode)
	}
	var out map[string]any
	_ = json.NewDecoder(resp.Body).Decode(&out)
	if out["error"] == nil {
		t.Fatalf("GET on /api/ucp/mcp must return a JSON-RPC error envelope: %v", out)
	}
}

func TestMCMcp_Handler_ParseError(t *testing.T) {
	srv := mcmcp_newUnsignedServer(t)
	defer srv.Close()

	_, out := mcmcp_postJSON(t, srv, []byte("not json {"))
	// Note: mcmcp_postJSON asserts successful decode of the outer envelope.
	// The HTTP layer returns 400 with a JSON-RPC error object.
	if out["error"] == nil {
		t.Fatalf("parse error must return JSON-RPC error envelope: %v", out)
	}
	errObj, _ := out["error"].(map[string]any)
	if code, _ := errObj["code"].(float64); code != -32700 {
		t.Fatalf("parse error code must be -32700, got %v", errObj["code"])
	}
}

func TestMCMcp_Handler_ParseError_HTTPStatus(t *testing.T) {
	// Separate test: the HTTP status code for parse error must be 400.
	srv := mcmcp_newUnsignedServer(t)
	defer srv.Close()

	resp, err := http.Post(srv.URL+"/api/ucp/mcp", "application/json", bytes.NewReader([]byte("{{{bad")))
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusBadRequest {
		t.Fatalf("malformed JSON must return 400, got %d", resp.StatusCode)
	}
}

func TestMCMcp_Handler_MethodNotFound(t *testing.T) {
	srv := mcmcp_newUnsignedServer(t)
	defer srv.Close()

	body, _ := json.Marshal(map[string]any{
		"jsonrpc": "2.0", "id": 7, "method": "unknown/method", "params": nil,
	})
	statusCode, out := mcmcp_postJSON(t, srv, body)
	if statusCode != http.StatusBadRequest {
		t.Fatalf("unknown method must return 400, got %d", statusCode)
	}
	errObj, _ := out["error"].(map[string]any)
	if code, _ := errObj["code"].(float64); code != -32601 {
		t.Fatalf("method not found code must be -32601, got %v", errObj["code"])
	}
}

func TestMCMcp_Handler_ToolsList(t *testing.T) {
	srv := mcmcp_newUnsignedServer(t)
	defer srv.Close()

	body, _ := json.Marshal(map[string]any{
		"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": nil,
	})
	statusCode, out := mcmcp_postJSON(t, srv, body)
	if statusCode != http.StatusOK {
		t.Fatalf("tools/list must return 200, got %d", statusCode)
	}
	result, _ := out["result"].(map[string]any)
	rawTools := result["tools"]
	tools, _ := rawTools.([]any)
	if len(tools) == 0 {
		t.Fatal("tools/list must return at least one tool schema")
	}
	// Spot-check that sim + catalog tools appear.
	names := map[string]bool{}
	for _, rawTool := range tools {
		if tool, ok := rawTool.(map[string]any); ok {
			if n, _ := tool["name"].(string); n != "" {
				names[n] = true
			}
		}
	}
	for _, want := range []string{"sim_set_availability", "search_catalog", "create_checkout"} {
		if !names[want] {
			t.Errorf("tools/list: expected tool %q in response", want)
		}
	}
}

func TestMCMcp_Handler_UnknownToolCall(t *testing.T) {
	// tools/call with an unknown tool name → structuredContent.error contains "unknown tool"
	srv := mcmcp_newUnsignedServer(t)
	defer srv.Close()

	body, _ := json.Marshal(map[string]any{
		"jsonrpc": "2.0", "id": 2, "method": "tools/call",
		"params": map[string]any{"name": "totally_unknown_tool", "arguments": map[string]any{}},
	})
	statusCode, out := mcmcp_postJSON(t, srv, body)
	if statusCode != http.StatusBadRequest {
		t.Fatalf("unknown tool call: want 400, got %d", statusCode)
	}
	result, _ := out["result"].(map[string]any)
	sc, _ := result["structuredContent"].(map[string]any)
	if sc["error"] == nil {
		t.Fatalf("unknown tool: structuredContent must contain error field: %v", sc)
	}
}

func TestMCMcp_Handler_SimSetAvailabilityUnknownHotel(t *testing.T) {
	// tools/call sim_set_availability with a non-existent hotel_id → 404
	srv := mcmcp_newUnsignedServer(t)
	defer srv.Close()

	body, _ := json.Marshal(map[string]any{
		"jsonrpc": "2.0", "id": 3, "method": "tools/call",
		"params": map[string]any{
			"name":      "sim_set_availability",
			"arguments": map[string]any{"hotel_id": "ghost-hotel-99999", "available": false},
		},
	})
	statusCode, out := mcmcp_postJSON(t, srv, body)
	if statusCode != http.StatusNotFound {
		t.Fatalf("unknown hotel_id: want 404, got %d", statusCode)
	}
	result, _ := out["result"].(map[string]any)
	sc, _ := result["structuredContent"].(map[string]any)
	if sc["error"] != "hotel_not_found" {
		t.Fatalf("unknown hotel: structuredContent.error must be hotel_not_found: %v", sc)
	}
}

func TestMCMcp_Handler_RequireSignaturesRejectsUnsigned(t *testing.T) {
	// When RequireSignatures=true, an unsigned request must get 401.
	cfg := config{
		BudgetCeilingCents: 80000, BudgetHardMaxCents: 200000,
		DefaultTier: "L2", RequireSignatures: true,
		TierGrants: map[string]string{},
	}
	srv := httptest.NewServer(mcpHandler(cfg, newStore(), newSigVerifier(mcmcp_noopResolver)))
	defer srv.Close()

	body, _ := json.Marshal(map[string]any{
		"jsonrpc": "2.0", "id": 4, "method": "tools/list", "params": nil,
	})
	statusCode, out := mcmcp_postJSON(t, srv, body)
	if statusCode != http.StatusUnauthorized {
		t.Fatalf("require signatures: unsigned request must get 401, got %d", statusCode)
	}
	errObj, _ := out["error"].(map[string]any)
	if code, _ := errObj["code"].(float64); code != -32001 {
		t.Fatalf("signature required error code must be -32001, got %v", errObj["code"])
	}
}

func TestMCMcp_Handler_SimSetAvailabilityKnownHotel(t *testing.T) {
	// tools/call sim_set_availability with a valid hotel → 200 + status:ok
	srv := mcmcp_newUnsignedServer(t)
	defer srv.Close()

	body, _ := json.Marshal(map[string]any{
		"jsonrpc": "2.0", "id": 5, "method": "tools/call",
		"params": map[string]any{
			"name":      "sim_set_availability",
			"arguments": map[string]any{"hotel_id": "bali-alaya-ubud", "available": false},
		},
	})
	statusCode, out := mcmcp_postJSON(t, srv, body)
	if statusCode != http.StatusOK {
		t.Fatalf("valid sim_set_availability: want 200, got %d: %v", statusCode, out)
	}
	result, _ := out["result"].(map[string]any)
	sc, _ := result["structuredContent"].(map[string]any)
	if sc["status"] != "ok" {
		t.Fatalf("valid sim_set_availability: structuredContent.status must be ok: %v", sc)
	}
}
