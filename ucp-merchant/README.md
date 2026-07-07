# ucp-merchant (Go) — UCP merchant server

A UCP-compliant merchant the engine calls via MCP. **Status: aligned to the
canonical UCP spec** (see SPEC-NOTES.md) on sample data — canonical manifest,
MCP tool names, `create/update/complete_checkout` flow, `structuredContent`
responses, budget/HITL/idempotency enforced + tested. UCP signing/auth is
complete (RFC 9421 HTTP Message Signatures + ES256 JWK manifest); a live
catalog backend swap is a documented seam (via `TG_EDITION`), not wired in
this showcase.

## Endpoints
- `GET /.well-known/ucp` — capability manifest + signing keys (JWK).
- `POST /api/ucp/mcp` — MCP JSON-RPC 2.0 (tools/list, tools/call).
- `GET /health`.

## Security — the enforcement core
- **`checkoutTool()`'s `complete_checkout` branch is the enforcement core**:
  re-fetch live price → compare to a **server-side** budget ceiling → check
  idempotency → pending-state. The agent can only *request*; it cannot raise
  the ceiling.
- HTTP Message Signatures (RFC 9421) on every request when
  `UCP_REQUIRE_SIGNATURES=1`; capability intersection per autonomy tier;
  `ap2_mandate` (signed by the user's device key) required for L3 autonomous
  booking.
- No payment tokens / PII / credentials in outbound responses.

## Dev / testing
- `go build ./... && go run .` — defaults to `127.0.0.1:8090`
  (override via `UCP_LISTEN_ADDR`).
- `go test ./...` runs the full test suite (no external services needed).
- Demo convenience: `UCP_UNSIGNED_TIER=L2 go run .` lets unsigned requests
  reach checkout-tier for the demo; production deployments require signed
  requests instead.
- Skeleton is stdlib-only — no external Go dependencies.

## Status
- [x] Canonical manifest (`/.well-known/ucp`) — `catalog`, `checkout`, `order`, `ap2_mandate` (extends checkout), `common.identity_linking`; per-cap spec+schema
- [x] MCP `tools/list` + `tools/call` with canonical `meta.ucp-agent` args + `structuredContent`/`content[]` responses
- [x] Catalog tools: `search_catalog`, `lookup_catalog` (sample data)
- [x] Checkout flow: `create_checkout` → `update_checkout` → `complete_checkout`
- [x] `complete_checkout` server-side **budget ceiling + buyer_consent (HITL) + idempotency** (simulated booking)
- [x] EC P-256 (ES256) **signing_keys** JWK in manifest
- [x] **RFC 9421** HTTP Message Signature verification (verify-if-present; `UCP_REQUIRE_SIGNATURES=1` enforces) + Content-Digest + agent-profile key resolution
- [x] **Capability-intersection negotiation** wired as the **L1/L2/L3 autonomy tier gate** (tool dispatch gated on the active set; active caps returned in every response)
- [x] **`ap2_mandate`** — W3C-VC 2.0 two-tier envelope (CheckoutMandate VC + simulated PaymentMandate); signed budget/expiry consent verified server-side for L3 autonomous `complete_checkout`; settlement is SIMULATED (`simulated:true` labeled in every response)
- [ ] Swap the sample catalog for a live backend (documented seam, not wired here)
- [~] W3C-VC 2.0 two-tier mandate shape (CheckoutMandate VC + simulated PaymentMandate) done. Full JsonWebSignature2020 / SD-JWT + RFC 8785 JCS canonicalization and a real payment rail remain future work (needs a live payment-provider business account).

## Test
Start a server on `:8090` (`go run .`) and hit `/api/ucp/mcp`, or just run
`go test ./...` — the test suite exercises every flow above (budget-denied,
consent-gated, idempotent, sold-out recovery) without needing a live server.
