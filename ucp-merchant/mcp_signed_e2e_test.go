package main

import (
	"bytes"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"net/url"
	"testing"
	"time"
)

// #56/#57 — the LIVE signed-request path, end-to-end through the REAL mcpHandler.
//
// A signed agent request flows: sign -> POST -> merchant FETCHES the agent's /.well-known/ucp
// profile over loopback (permitted by UCP_ALLOW_DEV_SSRF) -> verifies the RFC-9421 signature
// against the fetched key -> maps the verified profile URL to its server-granted tier (L3) ->
// the L3-gated tool (create_checkout) is ALLOWED. The unsigned control is capped to L1 + denied.
//
// This is the POSITIVE tier-elevation proof (complements TestHandlerUnsignedCapsToL1, the
// negative) and exercises verify + httpKeyResolver(profile fetch) + grantedTier + the dispatch
// gate together against the real handler — deterministic, in-process, runs in `go test ./...`.
func TestSignedRequestElevatesToL3EndToEnd(t *testing.T) {
	agent, _ := loadOrCreateKey() // ephemeral agent device key
	jwk := agent.publicJWK()

	// Local agent profile publishing its key + the L3 capability set.
	profileDoc := fmt.Sprintf(
		`{"ucp":{"capabilities":{"dev.ucp.shopping.catalog":{},"dev.ucp.shopping.checkout":{},"dev.ucp.shopping.ap2_mandate":{}}},`+
			`"signing_keys":[{"kid":%q,"kty":"EC","crv":"P-256","x":%q,"y":%q}]}`,
		agent.kid, jwk["x"].(string), jwk["y"].(string))
	psrv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		_, _ = w.Write([]byte(profileDoc))
	}))
	defer psrv.Close()
	profileURL := psrv.URL + "/.well-known/ucp"

	cfg := config{
		BudgetCeilingCents: 80000, BudgetHardMaxCents: 200000,
		DefaultTier: "L3", UnsignedTier: "L1", AllowDevSSRF: true,
		TierGrants: map[string]string{profileURL: "L3"},
	}
	merchant := httptest.NewServer(mcpHandler(cfg, newStore(), newSigVerifier(httpKeyResolver(true))))
	defer merchant.Close()

	body, _ := json.Marshal(map[string]any{
		"jsonrpc": "2.0", "id": 1, "method": "tools/call",
		"params": map[string]any{"name": "create_checkout", "arguments": map[string]any{
			"meta": map[string]any{"autonomy_level": "L3"},
			"checkout": map[string]any{"user_id": "u1", "line_items": []map[string]any{
				{"hotel_id": "bali-alaya-ubud", "checkin": "2026-07-01", "checkout": "2026-07-03"},
			}},
		}},
	})

	call := func(signed bool) map[string]any {
		req, _ := http.NewRequest("POST", merchant.URL+"/api/ucp/mcp", bytes.NewReader(body))
		req.Header.Set("Content-Type", "application/json")
		if signed {
			// RFC9421-004: signReq now requires authority; extract host:port from
			// the test server URL so verify() sees a matching @authority value.
			mURL, _ := url.Parse(merchant.URL)
			for k, v := range signReq(agent, profileURL, "POST", mURL.Host, "/api/ucp/mcp", body, time.Now().Unix()) {
				req.Header.Set(k, v)
			}
		}
		resp, err := http.DefaultClient.Do(req)
		if err != nil {
			t.Fatal(err)
		}
		defer resp.Body.Close()
		raw, _ := io.ReadAll(resp.Body)
		var out struct {
			Result struct {
				StructuredContent map[string]any `json:"structuredContent"`
			} `json:"result"`
		}
		if err := json.Unmarshal(raw, &out); err != nil {
			t.Fatalf("decode: %v body=%s", err, raw)
		}
		return out.Result.StructuredContent
	}

	// SIGNED -> verified profile grants L3 -> create_checkout must NOT be denied.
	if sc := call(true); sc["status"] == "denied" {
		t.Fatalf("signed L3 agent must NOT be denied create_checkout (tier elevation via verified profile failed): %v", sc)
	}
	// UNSIGNED control -> capped to L1 -> denied (proves the elevation REQUIRES the signature).
	if uc := call(false); uc["status"] != "denied" || uc["autonomy_tier"] != "L1" {
		t.Fatalf("unsigned control must be denied + capped to L1: %v", uc)
	}
}
