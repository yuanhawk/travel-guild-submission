# AliCloud / Alibaba Ecosystem Usage — Travel Guild Submission Proof

**Last updated:** 2026-07-09
**Purpose:** Verifiable evidence of Alibaba Cloud service integration in the Travel Guild submission. Every claim below maps to a file path and line number a judge can open in under 2 minutes.

---

## Table of Contents

1. [Qwen / DashScope — Primary LLM](#1-qwen--dashscope--primary-llm)
2. [SLS (Simple Log Service) — Trip Telemetry Pipeline](#2-sls-simple-log-service--trip-telemetry-pipeline)
3. [KMS (Key Management Service) — Agent Signing Key Seam](#3-kms-key-management-service--agent-signing-key-seam)
4. [ECS (Elastic Compute Service) — Dev / Demo Host](#4-ecs-elastic-compute-service--dev--demo-host)
5. [AMap / Gaode Maps — Map Data Layer](#5-amap--gaode-maps--map-data-layer)
6. [Alibaba Ecosystem Positioning — Alipay AP2 + UCP](#6-alibaba-ecosystem-positioning--alipay-ap2--ucp)
7. [Honesty Disclosures (KIV / Activate-Late items)](#7-honesty-disclosures-kiv--activate-late-items)

---

## 1. Qwen / DashScope — Primary LLM

**Status: LIVE (gated on `DASHSCOPE_API_KEY`)**

Qwen models power the fuzzy / LLM-on execution path. Every LLM call goes to the DashScope international endpoint via the OpenAI-compatible REST API. The reasoning extension (`enable_thinking`) is disabled globally — qwen3.x reasoning models take 110 s with thinking on; disabling it brings latency to ~6 s.

### API endpoint

```
https://dashscope-intl.aliyuncs.com/compatible-mode/v1
```

File: `society/utils/model_router.py`, line 52.

### Model routing

| Profile | Models | When |
|---|---|---|
| `test` | `qwen-flash` | CI / unit tests (cheap, fast) |
| `demo` (default) | `qwen-plus`, `qwen-max` | Live demo runs |
| fast tier | `qwen-flash` | `narrator` + `translate` roles |

Env vars controlling routing: `SOCIETY_MODEL_PROFILE`, `SOCIETY_DEMO_MODELS`, `SOCIETY_TEST_MODELS`, `SOCIETY_FAST_MODELS`, `SOCIETY_FAST_ROLES`, `SOCIETY_MODEL_TOKEN_CAP` (default 950 000 tokens per model before auto-rotate).

File: `society/utils/model_router.py`, lines 57–76 (profile definitions), 204–260 (`dashscope_chat` dispatcher).

### Three clamped-edge LLM calls

All three calls share the same design contract: closed-set output, one retry, determinism preserved (LLM only picks from a pre-filtered real-catalog set, never invents data).

1. **Intent parse** — free-text trip request → structured `TripRequest`.
   File: `society/utils/intent_parser.py`, line 65 (model constant), line 5809 (LLM call with `enable_thinking: False`).

2. **Vibe-to-area ranking** — qualitative vibe tokens → ranked real catalog areas.
   File: `society/agents/destination_agent.py`, line 81 (model constant), line 432 (LLM call with `enable_thinking: False`).

3. **Accommodation candidate ranking** — ranked shortlist of seeded hotels by (vibe, preference).
   File: `society/agents/accommodation_agent.py`, line 184 (model constant), line 248 (LLM call with `enable_thinking: False`).

Additional LLM roles (same endpoint, same `enable_thinking: False` guard): itinerary narrator (`society/utils/itinerary_narrator.py` line 114), follow-up parser (`society/utils/followup_parser.py` line 158), aftercare translation (`society/utils/aftercare_lang.py` line 103), coverage-gap notes (`society/utils/coverage_gap.py` line 483), fraud agent (`society/agents/fraud_agent.py` line 665), health agent (`society/agents/health_agent.py` line 3876), risk agent (`society/agents/risk_agent.py` line 7691), insurance agent (`society/agents/insurance_agent.py` line 794).

### `enable_thinking: False` invariant test

A dedicated CI test verifies that every file that makes a DashScope `chat/completions` call carries `"enable_thinking": False` in the request body. Fails if a new LLM call is added without the flag.

File: `society/tests/test_llm_enable_thinking_cov3.py`, lines 41–76.

### Token tracking + auto-rotate

Per-model token counts are persisted to `.token_counts.json` under an `fcntl` exclusive lock. When a model crosses `SOCIETY_MODEL_TOKEN_CAP`, `mark_exhausted()` rotates to the next model in the profile list automatically.

File: `society/utils/model_router.py`, lines 100–200 (state primitives).

### Key environment variable

```
DASHSCOPE_API_KEY   # required; never hardcoded; read from env only
```

---

## 2. SLS (Simple Log Service) — Trip Telemetry Pipeline

**Status: LIVE local sink; SLS cloud sink activate-late (set `SLS_ENABLED=1`)**

Every planning run emits a structured trip-summary event (trip type, outcome, cities + coordinates, agent timing, total cost) through a fire-and-forget telemetry pipeline. The pipeline is var-0-firewalled: it is a pure EMIT-ONLY side effect. The deterministic itinerary output is byte-identical whether telemetry is on or off. Every failure is swallowed — a SLS error can never break a booking or change a result.

### Sink order (best-effort)

1. **Local JSONL** (`society/telemetry.jsonl`) — always written when `TELEMETRY_ENABLED=1`; no cloud credentials needed. The demo AMap heat-map dashboard reads this file.
2. **Alibaba SLS** (`aliyun.log` PutLogs) — additionally written when `SLS_ENABLED=1` and all five SLS env vars are present. Falls back silently to JSONL if the `aliyun-log-python-sdk` is not installed.

### File

`society/utils/telemetry.py`, lines 1–60.

Key functions:
- `telemetry_enabled()` — line 27; gate check (`TELEMETRY_ENABLED=1`)
- `emit_trip(event)` — line 31; main public API, no-op by default
- `_emit_sls(event)` — line 47; SLS path via `aliyun.log.LogClient` + `PutLogsRequest`

### Environment variables

```
TELEMETRY_ENABLED                   # set to "1" to enable the local JSONL sink
SLS_ENABLED                         # set to "1" to additionally emit to Alibaba SLS
ALIBABA_CLOUD_ACCESS_KEY_ID         # SLS auth (read from env only)
ALIBABA_CLOUD_ACCESS_KEY_SECRET     # SLS auth (read from env only)
SLS_ENDPOINT                        # e.g. cn-hangzhou.log.aliyuncs.com
SLS_PROJECT                         # SLS project name
SLS_LOGSTORE                        # SLS logstore name
```

### Test coverage

`society/tests/test_telemetry_sls_cov.py` and `society/tests/test_telemetry_cov3.py`.

---

## 3. KMS (Key Management Service) — Agent Signing Key Seam

**Status: SEAM COMMENT IN PLACE IN THIS REPO; NO LIVE KMS CODE SHIPPED HERE.** The NHI (Non-Human Identity) agent signing key (RFC 9421 ES256 / EC P-256) is loaded from a plaintext PEM on disk (or an ephemeral in-memory key if no path is configured) — an honestly-acknowledged "long-lived key at rest" gap.

### Seam location (as it actually exists in this repo)

`society/utils/ucp_signing.py`, `load_or_create_key()` docstring (lines 56–63): describes where a KMS-backed implementation would plug in (envelope-decrypt a KMS-wrapped PEM, or an asymmetric KMS `Sign` so the key never leaves KMS), and states plainly that "the live KMS call is intentionally NOT scaffolded here." That sentence is accurate for this exact file today — nothing more should be assumed from it.

### What we actually attempted (in a separate, private repo — not shipped here)

In the project's private backend repo, we attempted to activate real KMS envelope-encryption for this seam. The account's KMS 3.0 instance turned out to be a **Dedicated KMS** instance, requiring a completely different SDK and auth model than the generic KMS SDK — `alibabacloud-dkms-gcs` with mutual-TLS client-certificate authentication, not AccessKey/Secret auth. The integration was rewritten correctly against the real, verified SDK API in both Python and Go, and passed an independent code audit. A live connection attempt with fully correct, real credentials confirmed the code and auth were right — the request reached AliCloud's infrastructure and was rejected for documented, specific reasons (`InvalidHeader` on one auth path, `UnsupportedOperation` on another), not a vague timeout or a bug in the code. What we could not do in the time available was complete a live encrypt→decrypt round-trip, which needs genuine private-network access to the instance.

Rather than merge unverified security-critical code anywhere — public or private — we closed that work, released the KMS instance (ongoing cost, not earning its keep for this submission), and are documenting the attempt honestly here and in the Devpost submission's Challenges section, instead of either staying silent about it or claiming more progress than a live-verified round-trip.

---

## 4. ECS (Elastic Compute Service) — Dev / Demo Host

**Status: LIVE**

The backend engine, LLM-on runtime container, and staging demo all run on an AliCloud ECS instance. The runtime container runs rootless Docker, config and secrets injected via environment variables at deploy time (never committed to any repo).

The Qwen model served through the runtime container pulls from DashScope via `DASHSCOPE_API_KEY`, injected from a deploy-time env file (not committed). The ECS box is the only infrastructure needed for the full backend stack.

### Evidence

- `DASHSCOPE_API_KEY` is read from the environment; see `society/utils/model_router.py` and `society/utils/intent_parser.py` for the read sites (env var name only, never a value).
- **Live deployment, verifiable directly:** `curl https://api-staging.itinerario.io/health` returns `{"status":"ok","society":"ready","git_sha":"<commit>"}` from `society/orchestration/server.py`'s `/health` handler — a judge can hit this URL right now and see a real, currently-running backend respond, not a static claim.
- A cloud-console screenshot was considered as evidence but is deliberately not shipped, even redacted: any capture of live infrastructure carries residual risk not worth taking in a repo that will eventually go public. See SECURITY-ADVISORY.md #10 for the full writeup — an earlier version contained real infra identifiers (this repo has always been private, so it was never externally exposed); it was redacted, then removed entirely, and its git history was rewritten so no version of it remains reachable from this repo.

---

## 5. AMap / Gaode Maps — Map Data Layer

**Status: KEY-GATED OFF BY DEFAULT (`AMAP_ENABLED=0`); activates for live demo**

AMap serves two roles:

### 5a. CN dining-reviews provider seam

Inside China (`iso2 == "CN"`), the dining-reviews dispatch prefers AMap over Google as the map data provider.

File: `society/orchestration/orchestrator.py`, line 4239:

```python
provider = "amap" if iso2 == "CN" else "google"
```

### 5b. AMap search handoff link

`booking_links.py` builds a deterministic AMap search URL (`https://www.amap.com/search?query=...`) for CN-destined maps queries.

File: `society/utils/booking_links.py`, line 437.

### 5c. AMap heat-map dashboard (SLS telemetry overlay)

The SLS telemetry pipeline (`telemetry.py`) describes the AMap integration as the visual dashboard layer: trip coordinates emitted to `telemetry.jsonl` (or SLS) are consumed by an AMap heat-map overlay in the demo UI.

File: `society/utils/telemetry.py`, lines 1–9.

### 5d. Key gate

The live AMap and Google Places enrichment (POI + dining) is disabled by default and activates only at the demo window.

```
AMAP_ENABLED=0      # deploy-time env, not committed
PLACES_ENABLED=0    # same, flip to 1 for the live demo
```

Test coverage: `society/tests/test_dining_reviews.py`, `society/tests/test_booking_links.py`.

---

## 6. Alibaba Ecosystem Positioning — Alipay AP2 + UCP

**Status: UCP + W3C-VC + RFC 9421 LIVE; Alipay real-rail KIV (needs business account)**

The Travel Guild pitches itself as the safe-execution layer for China/SEA travel via the Alibaba ecosystem (Qwen + Alipay + Fliggy/Ctrip + AMap). The technical stack is described below.

### 6a. UCP (Universal Commerce Protocol) — prepaid checkout

The Go merchant (`ucp-merchant/`) implements RFC 9421 HTTP Message Signatures for request signing, W3C-VC 2.0 mandate envelopes (two-tier: CheckoutMandate + PaymentMandate), HITL consent gates, integer-cents budget vetoes, and all-or-none atomic multi-leg checkout.

Key files:

| File | Purpose |
|---|---|
| `ucp-merchant/rfc9421.go` | RFC 9421 HTTP signature verification (Go side) |
| `ucp-merchant/signing.go` | ES256 P-256 signing key load + JWK export |
| `ucp-merchant/mandate_vc.go` | W3C-VC 2.0 envelope over the checkout mandate (AP2 tier 1) |
| `ucp-merchant/wallet.go` | Prepaid wallet: deposit/hold/commit/release (integer cents, atomic) |
| `ucp-merchant/checkout.go` | Multi-leg checkout: one mandate per booking, all-or-none |
| `ucp-merchant/ucp.go` | Core UCP types, budget-veto logic |
| `ucp-merchant/negotiate.go` | Agent ↔ merchant negotiation protocol |
| `ucp-merchant/mcp.go` | MCP tool surface (search_catalog, initiate_checkout, complete_checkout) |
| `society/utils/ucp_signing.py` | Python client-side RFC 9421 signing (NHI agent identity) |

Two checkout kinds are wired end-to-end:
- **`LODGING`** — hotel prepaid checkout (the primary kind)
- **`FOOD_DELIVERY`** — late-night supper (Ele.me-native concept, demo-simulated); see `society/utils/supper_order.py` lines 37–42 for the honesty disclosure (simulated inventory, no live Ele.me partnership claimed).

### 6b. AP2 (Agent Payments Protocol) — simulated Alipay settlement

AP2 tier-2 (PSP fund authorization / Alipay settlement) is **SIMULATED** in `ucp-merchant/alipay_sim.go`. Every response carries `"simulated": true` and a note that real Alipay requires a business-account keypair.

File: `ucp-merchant/alipay_sim.go`, lines 1–30 (module docstring + constant `alipaySimNote`).

The W3C-VC mandate envelope explicitly labels the PaymentMandate tier as a stub:

> "paymentMandate is AP2 tier 2 (PSP fund authorization). STUB: settlement is the SIMULATED Alipay rail (alipay_sim.go); a real SD-JWT PSP credential is KIV."

File: `ucp-merchant/mandate_vc.go`, lines 39–40.

**What "KIV" means:** Alipay real-rail requires a registered Chinese business account with an approved PSP keypair (not available for an individual developer submitting a hackathon project). The simulation is honestly labeled throughout; a real integration is architecturally wired and would activate when the business account is available.

### 6c. Go UCP merchant test coverage

| Test file | What it proves |
|---|---|
| `ucp-merchant/alipay_sim_test.go` | Simulated settlement determinism + idempotency |
| `ucp-merchant/mcp_signed_e2e_test.go` | Full signed agent → merchant → checkout round-trip |
| `ucp-merchant/security_test.go` | Replay-attack, signature tamper, unsigned-tier floor |
| `ucp-merchant/interop_live_test.go` | Python-agent → Go-merchant interop |
| `ucp-merchant/morecover_simucp_test.go` | Wallet atomicity + budget-veto edge cases |
| `ucp-merchant/morecover_misc_test.go` | Miscellaneous UCP invariants |

---

## 7. Honesty Disclosures (KIV / Activate-Late items)

This section collects every "not yet live" claim to let judges verify we never overclaim.

| Service | Current state | What activates it | File / evidence |
|---|---|---|---|
| **KMS** | Seam comment only in this repo; activation attempted in the private backend repo, deferred (see §3) | Genuine VPC-network-verified live round-trip, then `UCP_KMS_ENABLED=1` + creds | `society/utils/ucp_signing.py` L56–63 |
| **SLS cloud sink** | Local JSONL always works; SLS additionally when enabled | `SLS_ENABLED=1` + five SLS env vars | `society/utils/telemetry.py` L40–60 |
| **AMap live enrichment** | `AMAP_ENABLED=0` (key-gated off) | Flip `AMAP_ENABLED=1` + insert API key | deploy-time env, not committed |
| **Alipay real settlement** | Simulated (`"simulated": true` in every response) | Business-account PSP keypair (KIV) | `ucp-merchant/alipay_sim.go`; `ucp-merchant/mandate_vc.go` |
| **Ele.me FOOD_DELIVERY** | Demo-simulated; no Ele.me partnership | Formal Ele.me/AliCloud partnership | `society/utils/supper_order.py` L37–42 |

All KIV items are labeled inline in code — the project's honesty contract ("fail-honest") prohibits silent gaps.

**Security posture note:** trip-action ownership (STORE-002/VULN-AUTH-002/VULN-AUTH-003 — IDOR on `/confirm`, `/cancel`, `/replan`, `/refine`) is closed via a two-tier `session_token`/`owner_token` gate (its honest limitations: passwordless demo login, an ephemeral-store fail-open window for legacy pre-gate rows — the private design spec has the full model, not shipped in this repo). `GET /trips/{idempotency_key}` (read-only, same vuln class) is also closed — `trips_detail` reuses the same ownership gate with 404 existence-hiding. The same gate now also covers `/aftercare/check` and `/reconsider_leg` (2026-07-04), alongside CORS allowlisting, dependency floor-pinning, and a global daily denial-of-wallet cost breaker.

**UCP merchant end-user ownership binding (task #161):** checkout `UserID` — the `session_token`/`owner_token`-verified end-user identity threaded from the orchestrator — is verified against the orchestrator-verified session owner at `update_checkout`/`complete_checkout`/`cancel_checkout`/`wallet_get`, gated on `RequireSignatures` (prod); regression-tested at the merchant layer (`ucp-merchant/enduser_ownership_test.go`: `TestCompleteCheckoutUserOwnershipVeto` et al.), independent of the orchestrator-layer IDOR fix above. Honest caveat: anonymous end users get a derived one-way pseudo-id (`"anon:" + sha256(owner_token)[:24]`, never the raw secret), and legacy pre-`owner_token` rows fail open in the ephemeral in-memory Go store — same rollout-window shape as the STORE-002 fix.

---

## Quick verification checklist for judges

Open each file at the referenced line to confirm the claim in under 2 minutes:

1. **DashScope API URL** → `society/utils/model_router.py:52` — `dashscope-intl.aliyuncs.com`
2. **`enable_thinking: False` guard** → `society/utils/intent_parser.py:5809` (intent parse call)
3. **SLS `PutLogs` call** → `society/utils/telemetry.py:47–60` (`_emit_sls` function)
4. **KMS seam comment** → `society/utils/ucp_signing.py:56–62`
5. **AMap CN routing** → `society/orchestration/orchestrator.py:4239`
6. **AMap URL builder** → `society/utils/booking_links.py:434–438`
7. **Alipay sim note** → `ucp-merchant/alipay_sim.go:30` (`alipaySimNote` constant)
8. **W3C-VC tier-2 label** → `ucp-merchant/mandate_vc.go:39–40` (KIV note in comment)
9. **RFC 9421 signing** → `ucp-merchant/rfc9421.go` (full verifier); `society/utils/ucp_signing.py` (Python client)
10. **Live AliCloud ECS deployment** → `curl https://api-staging.itinerario.io/health` — a real, currently-running backend responds with its `git_sha` (not a static claim); see SECURITY-ADVISORY.md #10 for why no console screenshot ships in this repo
