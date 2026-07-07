package main

import (
	"bytes"
	"crypto/ecdsa"
	"encoding/json"
	"errors"
	"net/http"
	"net/http/httptest"
	"testing"
)

func newTestServer() *httptest.Server {
	cfg := config{BudgetCeilingCents: 80000, BudgetHardMaxCents: 200000, DefaultTier: "L2", TierGrants: map[string]string{}}
	v := newSigVerifier(func(string, string) (map[string]*ecdsa.PublicKey, []string, error) {
		return nil, nil, errors.New("none")
	})
	return httptest.NewServer(mcpHandler(cfg, newStore(), v))
}

func post(t *testing.T, base, name string, args map[string]any) map[string]any {
	t.Helper()
	body, _ := json.Marshal(map[string]any{
		"jsonrpc": "2.0", "id": 1, "method": "tools/call",
		"params": map[string]any{"name": name, "arguments": args},
	})
	resp, err := http.Post(base+"/api/ucp/mcp", "application/json", bytes.NewReader(body))
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	var out struct {
		Result struct {
			StructuredContent map[string]any `json:"structuredContent"`
		} `json:"result"`
	}
	if err := json.NewDecoder(resp.Body).Decode(&out); err != nil {
		t.Fatal(err)
	}
	return out.Result.StructuredContent
}

// TestHandlerUnsignedCapsToL1 is the C2 property: an unsigned request cannot
// escalate its tier by claiming autonomy_level — it is capped to L1.
func TestHandlerUnsignedCapsToL1(t *testing.T) {
	ts := newTestServer()
	defer ts.Close()

	sc := post(t, ts.URL, "create_checkout", map[string]any{
		"meta": map[string]any{"autonomy_level": "L3"},
		"checkout": map[string]any{"user_id": "u1", "line_items": []map[string]any{
			{"hotel_id": "bali-alaya-ubud", "checkin": "2026-07-01", "checkout": "2026-07-03"},
		}},
	})
	if sc["status"] != "denied" || sc["reason"] != "capability_not_active" {
		t.Fatalf("unsigned L3 must be capped + denied: %v", sc)
	}
	if sc["autonomy_tier"] != "L1" {
		t.Fatalf("effective tier should be L1, got %v", sc["autonomy_tier"])
	}

	// Catalog still works at L1.
	sc = post(t, ts.URL, "search_catalog", map[string]any{
		"query": map[string]any{"city": "bali", "checkin": "2026-07-01", "checkout": "2026-07-03"},
	})
	if c, _ := sc["count"].(float64); c < 1 {
		t.Fatalf("L1 search should return results: %v", sc)
	}
}
