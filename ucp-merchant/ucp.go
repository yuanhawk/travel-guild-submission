package main

import (
	"net/http"
	"strings"
)

const ucpVersion = "2026-04-08"

// leaf: last dotted segment, e.g. dev.ucp.shopping.checkout -> "checkout".
func leaf(capID string) string {
	parts := strings.Split(capID, ".")
	return parts[len(parts)-1]
}

// nsPath: drop the dev.ucp prefix -> "shopping/checkout", "common/identity_linking".
func nsPath(capID string) string {
	parts := strings.Split(capID, ".")
	if len(parts) >= 4 {
		return strings.Join(parts[2:], "/")
	}
	return strings.Join(parts, "/")
}

// capID -> whether it's an extension of another capability (canonical UCP set).
// Catalog search+lookup are ONE capability (tools live under it). buyer_consent
// is NOT a UCP capability — consent is AP2 mandate (L3) / app-level HITL (L2).
var capabilities = []struct {
	ID      string
	Extends string
}{
	{"dev.ucp.shopping.catalog", ""},
	{"dev.ucp.shopping.checkout", ""},
	{"dev.ucp.shopping.order", ""},
	{"dev.ucp.shopping.ap2_mandate", "dev.ucp.shopping.checkout"},
	{"dev.ucp.common.identity_linking", ""},
	// RFC9421-001: sim/wallet control capability — gates sim_*/wallet_* tools.
	// Must be in this list so merchantCapIDs() recognises it and negotiate()
	// can activate it for L2/L3 tiers. Without this entry, negotiate() prunes
	// it as an unknown cap and the tools are ALWAYS denied (demo break).
	{"dev.ucp.sim.control", ""},
}

func specURL(capID string) string  { return "https://ucp.dev/" + ucpVersion + "/specification/" + leaf(capID) }
func schemaURL(capID string) string {
	return "https://ucp.dev/" + ucpVersion + "/schemas/" + nsPath(capID) + ".json"
}

func capabilityManifest() map[string]any {
	out := map[string]any{}
	for _, c := range capabilities {
		entry := map[string]any{"version": ucpVersion, "spec": specURL(c.ID), "schema": schemaURL(c.ID)}
		if c.Extends != "" {
			entry["extends"] = c.Extends
		}
		out[c.ID] = []map[string]any{entry}
	}
	return out
}

// GET /.well-known/ucp — capability manifest (canonical shape, plan §7.2).
func ucpProfileHandler(cfg config, mk *merchantKey) http.HandlerFunc {
	return func(w http.ResponseWriter, _ *http.Request) {
		writeJSON(w, http.StatusOK, map[string]any{
			"ucp": map[string]any{
				"version": ucpVersion,
				"services": map[string]any{
					"dev.ucp.shopping": []map[string]any{{
						"version":   ucpVersion,
						"spec":      "https://ucp.dev/" + ucpVersion + "/specification/overview",
						"transport": "mcp",
						"endpoint":  cfg.BaseURL + "/api/ucp/mcp",
						"schema":    "https://ucp.dev/" + ucpVersion + "/services/shopping/mcp.openrpc.json",
					}},
				},
				"capabilities": capabilityManifest(),
			},
			// EC P-256 (ES256) public key for RFC 9421 signature verification (§8.1).
			"signing_keys": []any{mk.publicJWK()},
		})
	}
}
