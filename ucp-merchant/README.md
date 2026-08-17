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
- [x] **`ap2_mandate`** — W3C-VC 2.0 two-tier envelope (CheckoutMandate VC + simulated PaymentMandate); signed budget/expiry consent verified server-side for L3 autonomous `complete_checkout`; settlement via the alipay sim rail is SIMULATED (`simulated:true` labeled in every response) — **a REAL settlement rail now also exists, see below**
- [ ] Swap the sample catalog for a live backend (documented seam, not wired here)
- [~] W3C-VC 2.0 two-tier mandate shape (CheckoutMandate VC + simulated PaymentMandate) done. Full JsonWebSignature2020 / SD-JWT + RFC 8785 JCS canonicalization remain future work. A real payment rail is **no longer** future work for USDC specifically — see Circle Agentic Economy Prize integration below.

## Circle Agentic Economy Prize integration — REAL, not simulated

Unlike the alipay sim rail above, `circle_usdc.go` makes genuine HTTP calls to
Circle's Developer-Controlled Wallets API (`api.circle.com`) — a real USDC
transfer between two developer-controlled wallets on a public testnet
(Ethereum Sepolia), not a hash-derived fake id. Live-verified end-to-end
2026-08-17: entity-secret registration, wallet-set/wallet creation, a live
transfer call, and on-chain confirmation via an independent third-party RPC
call all completed successfully.

**Proof of live settlement (Circle Agentic Economy Prize eligibility artifacts):**
- Demo video (genuinely agent-driven, toggle-triggered — no admin endpoint involved): [youtu.be/h2mEdbEKWRw](https://youtu.be/h2mEdbEKWRw)
- Agent (source) Circle wallet address: `0x776244b38e4f99cd24bbecf7047be6309ffad787`
- The settlement shown in the video, verifiable on-chain: [`sepolia.etherscan.io/tx/0x4f17b70b0083d8b6a8b2e4f45fd08a805f7966405d6d492e2e8d44dd405c2001`](https://sepolia.etherscan.io/tx/0x4f17b70b0083d8b6a8b2e4f45fd08a805f7966405d6d492e2e8d44dd405c2001)
  (42.000000 USDC, confirmed independently via a direct `eth_getTransactionReceipt`
  RPC call showing `status: 0x1` and a genuine ERC-20 `Transfer` event — not
  merely Circle's own reporting). Fired automatically by `maybeCircleSettle`
  the instant a real, toggle-driven web UI booking completed checkout — no
  admin endpoint, no manual trigger.
- Network: Ethereum Sepolia testnet (`ETH-SEPOLIA`).
- An earlier transaction (5.000000 USDC, [`sepolia.etherscan.io/tx/0x2c3e39d9b10e5a8159273ca9abea1ce6928d227a3ee23dee19a90e8f879c2e61`](https://sepolia.etherscan.io/tx/0x2c3e39d9b10e5a8159273ca9abea1ce6928d227a3ee23dee19a90e8f879c2e61))
  was fired via the admin entry point during integration testing, confirming
  the same rail before the end-to-end UI flow was exercised.

**Two entry points**, both fail-closed by construction (see `main.go`'s
`checkCircleStartupSafety` — the server refuses to start if the rail is
configured without an admin token, or without required signatures when the
unsigned-caller tier grants checkout capability):

1. `POST /admin/circle/settle` (admin-token-gated direct entry point).
2. **Genuinely agent-driven**: set `checkout.settlement_rail: "circle_usdc"` on
   `create_checkout`. When that booking's `complete_checkout` commits, a real
   transfer for the booking's own (budget-enforced) `total_cents` fires
   automatically — no human manually hits any settlement endpoint.

**Config** (all four required to enable the rail — unset means the rail
returns an honest `CIRCLE_NOT_CONFIGURED` rather than faking a settlement):
`CIRCLE_API_KEY`, `CIRCLE_ENTITY_SECRET`, `CIRCLE_SOURCE_WALLET_ID`,
`CIRCLE_MERCHANT_WALLET_ID`.

Startup safety refuses to boot when Circle credentials are set, signatures
aren't required, and the unsigned tier grants checkout — because normally
that means any unauthenticated caller could drive a real settlement. A
narrow exemption exists for a co-located single-host demo: set
`UCP_CIRCLE_ALLOW_UNSIGNED_LOOPBACK=1` **and** bind `UCP_LISTEN_ADDR` to a
loopback address (`127.0.0.1`, `::1`, or `localhost`) and startup proceeds
with a warning. Both are required — loopback alone would silently change
behavior for the common default config, and the flag alone can't weaken a
public bind. This does **not** relax the admin-token requirement, and it
only makes sense when nothing else (no reverse proxy, no forwarder) is
routing external traffic to this port — unset the flag once you're done.

Optional: `CIRCLE_AGGREGATE_CAP_USD` — the process-lifetime ceiling on the
**total** USDC this rail may move across all bookings (default `10000`, i.e.
$10,000; `0` refuses every settlement; an unparseable value falls back to the
default rather than to "unbounded" — there is deliberately no unbounded
setting).

**Spend ceilings — two independent limits.** *Per booking*: a settlement can
never exceed `BUDGET_HARD_MAX_USD` — the admin endpoint rejects an over-cap
request outright, and the agent-driven path is bounded by
`min(user_budget_cents, BUDGET_HARD_MAX_USD)` before the `booking_ref` is even
generated (same enforcement core as above). *Across bookings*:
`CIRCLE_AGGREGATE_CAP_USD` caps the cumulative cents this rail may ever move,
enforced inside `circleSettle` — the single choke point both entry points
funnel through — by reserving the amount in the same critical section that
guards against double transfers, i.e. *before* the network call, so N
concurrent settlements for N different bookings cannot collectively overshoot.
Idempotent replays of an already-settled `booking_ref` consume no additional
headroom; attempts that fail upstream or find the rail unconfigured release
theirs. `GET /admin/circle/settle` reports `aggregate_cap_cents`,
`aggregate_committed_cents`, and `aggregate_remaining_cents`; a refused
settlement returns `403 exceeds_circle_aggregate_cap` (admin) or
`circle_settlement: {"error":"exceeds_circle_aggregate_cap"}` (checkout path).

**Known limitations**: the aggregate counter lives in the in-memory store, so
it resets on process restart — same lifetime as every other piece of state
here. It is a per-process ceiling, not a durable lifetime budget; a mainnet
deployment would need persistent accounting. A booking whose settlement is
refused by the aggregate cap still completes as a booking — no transfer fires
and the response says so, the same shape as any other settlement failure,
rather than silently implying payment. `cancel_checkout` on a booking with a
real settlement does not reverse the on-chain transfer — it says so
explicitly in the response (`circle_settlement_not_reversed: true`) rather
than silently implying otherwise.

## Test
Start a server on `:8090` (`go run .`) and hit `/api/ucp/mcp`, or just run
`go test ./...` — the test suite exercises every flow above (budget-denied,
consent-gated, idempotent, sold-out recovery) without needing a live server.
