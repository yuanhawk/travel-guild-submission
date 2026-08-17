# Travel Guild — Web App

The judge-facing user dashboard for **Travel Guild**. A lightweight
**Svelte + TypeScript + Vite** single-page app that renders an agentic
travel-planning experience: chat the trip you want, watch the multi-agent
plan stream in live, review an itinerary on an interactive **MapLibre GL**
map, edit it, and give **one** consent to book.

This is the **end-user product surface**. It is decoupled from the planning
engine by a typed HTTP/SSE contract (`src/lib/api.ts`), so the two halves of
this repo can be developed and reasoned about independently. It talks to the
engine at this repo's root (11 specialist agents + an orchestrator-level
consent gate — see the root `README.md`) — the frontend is **pure
presentation** and never recomputes prices, risk, or scores client-side; the
engine is the single source of truth.

---

## What it is

| | |
|---|---|
| **Framework** | Svelte 4 + TypeScript, built with Vite 5 |
| **Map** | MapLibre GL JS — WebGL vector/raster maps on **free OSM tiles, no API key** |
| **Output** | `vite build` → static `dist/` (relative `base`) hostable behind any CDN |
| **Backend** | Talks to the engine at this repo's root over HTTP + SSE |
| **Contract** | One typed client, `src/lib/api.ts` — canonical served field names, money in **cents** everywhere |

Core surfaces: 3-pane dashboard (chat / itinerary day-slider / map + right
rail), live agent-progress stream, per-hazard risk chips, Safety Watch tab,
drag-drop itinerary edit lane, place-detail map popups, the consent
Review → Confirm booking flow, a Monitor/aftercare tab, and a save-to-phone
`.ics` export.

---

## Quickstart

```bash
npm install

npm run dev          # http://localhost:5173 (talks to VITE_API_BASE; proxies to :8080 in dev)
npm run check        # svelte-check / tsc — type gate
npm test             # Vitest — unit + contract-shape tests (chromium-free)
npm run test:e2e     # Playwright — hermetic browser e2e (installs chromium)
npm run build        # → dist/  (static bundle for CDN/OSS)
npm run preview      # serve the built dist/ locally
npm run contract-check  # same-box drift gate vs the live backend (VITE_API_BASE)
```

Run the engine first (see the root `README.md`'s Quickstart), then this app
talks to it on `http://localhost:8080` by default.

- **Unit (`vitest`)** — `src/**/*.test.ts`: unit helpers (`centsToUsd`,
  `bpToPct`/`fmtIndicative`), the SSE event parser, `uiState` outcome mapping,
  ICS folding, and a `risk_signals.per_leg` + `decisions{}`/`advisory[]`
  contract-shape guard.
- **E2E (`playwright`)** — `e2e/*.spec.ts`: hermetic specs that mock
  `/negotiate_text` via `page.route` (no live backend) covering smoke, live
  progress, place card, edit lane, cancel, reload-restore, aftercare,
  remediation, session, preview.
- **Contract sync-check** — `scripts/contract-check.mjs` curls the live backend
  and fails on drift if it stops serving a field `api.ts` depends on. Run it
  whenever either half of this repo changes.
- **Local pre-commit gate** — `.pre-commit-config.yaml` runs a gitleaks scan
  before each commit; a fresh clone must run
  `pip install pre-commit && pre-commit install` once to activate it (see
  CONTRIBUTING.md). This showcase repo does not ship a CI workflow
  (`.github/workflows/`) — the local pre-commit hook is the only automated
  scan gate here.

---

## API contract it speaks

All endpoints live on the engine (this repo's root). HTTP status is usually
`200` even for terminal business outcomes — **truth lives in the response
body** (`outcome` / `status`). `402` (insufficient funds) and `403` (budget
veto) carry structured bodies the client still parses.

| Endpoint | Method | Purpose |
|---|---|---|
| `/negotiate_text` | POST | Plan from natural-language text. `{plan:true}` → held `plan_ready` (no charge); `{stream:true}` → returns a `stream_id`. |
| `/stream/{id}` | GET (SSE) | Live agent-progress event stream after a `{stream:true}` plan. Frames: `agent`/`layer`/`status`/`wallet`/`risk`/`emergency`/`narrate`/`negotiate_finished`. |
| `/confirm` | POST | **The one human consent** — commit a held plan → booked. Idempotent: retrying the same `idempotency_key` never double-books or double-charges. |
| `/replan` | POST | Item-level deterministic edit lane (remove/add/swap/variant/reflow) on a held OR booked plan. Pure server-side envelope transform; never books or debits. |
| `/refine` | POST | Conversational follow-up on a held plan (re-plan with truthful diff summary; returns a new `idempotency_key`). |
| `/cancel` | POST | Void a booked trip and refund the SIMULATED wallet. Idempotent. **Known gap:** a real Circle USDC settlement (see below) is **not** auto-reversed on cancel. The Go merchant's raw response flags this (`circle_settlement_not_reversed: true`), but that flag is currently dropped by `budget_agent`/`server.py` before it reaches this API — **the web client and UI do not yet disclose it.** Do not cancel a Circle-settled booking expecting an on-chain refund. |
| `/trips/{key}` | GET | Restore the stored plan envelope after a page reload. |
| `/session` · `/preferences` | POST · GET/PUT | Pre-seeded demo travellers (no registration); selecting one replays `user_id` so the backend applies its currency / nationality / persona preset. |
| `/emergencies` | GET | Live active-emergency overlay (opt-in). |
| `/place_card` · `/place_photo` | POST · GET | Server-proxied Google Places detail / photo for map pins. **The frontend never holds the Places key**; photo refs are opaque and resolve server-side only. |

**Streaming with graceful degrade** (`planStream.ts`): POST
`/negotiate_text {stream:true}` → open `/stream/{id}` → forward frames → resolve
on `negotiate_finished`. Any failure (503 server-busy, missing `stream_id`,
stream error, 120s timeout, POST throw) falls back to **one** blocking
`/negotiate_text`. Because the deterministic core is var-0 (stable), the
fallback yields the same plan.

---

## Configuration / env vars

| Var | Where | Notes |
|---|---|---|
| `VITE_API_BASE` | build/runtime | Backend base URL. Unset → defaults to `http://localhost:8080`. In **production builds the client throws unless it is `https://`** (no plaintext API traffic). |

See `.env.example`. For local development, an empty base (`VITE_API_BASE=''`)
makes the client use relative paths that hit the Vite dev-server `proxy`
(`vite.config.ts`), routing everything through the single `:5173` port
without exposing `:8080` directly — useful if you want one port to reach the
whole demo.

---

## Flags & runtime behaviour

- **`{plan:true}`** — plan-only: produce a held plan (`plan_ready`), no charge.
  Booking requires the explicit `/confirm` consent.
- **`{stream:true}`** — request the live SSE progress stream (returns a
  `stream_id`); degrades to a blocking plan on any stream failure.
- **`{live_emergency:{check:true}}`** — opt-in active-emergency overlay.
- **LLM-on vs LLM-off** — `itinerary_narrative` (a structured object, not a flat
  string) is present only when the backend runs LLM-on; it is **honestly
  absent** in deterministic/demo (LLM-off) mode and the UI renders without it.
- **`currency_review`** — additive, non-USD users only: an **indicative**
  display-currency line formatted from a server-computed integer. The FE does no
  FX math; charges are always in USD and the figure is labelled indicative,
  backed by a seeded snapshot with a disclaimer.
- **Dev proxy** — `vite.config.ts` `server.proxy` (local-test only) routes API +
  SSE to the local backend on `:8080`.

---

## Honesty notes (must read — these are claims we do NOT overstate)

- **Settlement is SIMULATED by default.** The prepaid wallet, debits,
  refunds, and `booking_ref` run in a sandbox — **not a real payment rail** —
  unless the caller opts into the real rail below. The UI labels the default
  wallet and booking as simulated.
- **A real settlement rail also exists (Circle Agentic Economy Prize).**
  Checking the "Settle with real USDC (testnet)" toggle before confirming a
  booking sends `settlement_rail: "circle_usdc"`, which triggers a genuine
  Circle Developer-Controlled Wallets USDC transfer on Ethereum Sepolia
  testnet — real on-chain funds move, verifiable via the clickable
  block-explorer link the UI renders on success. This is opt-in only; nothing
  moves unless the toggle is checked. See `ucp-merchant/README.md` § *Circle
  Agentic Economy Prize integration* for the full mechanics. The toggle works
  on both the app's streaming and blocking-fallback negotiate paths.
- **AP2 = mandate protocol + simulated settle (Alipay rail only).** AP2
  mandate protocol (W3C-VC two-tier) is fully implemented; the **Alipay**
  settlement leg specifically is **SIMULATED** (no real payment rail wired —
  a real Alipay integration is pending a business account). This is
  independent of the real Circle USDC rail above, which does not go through
  AP2/Alipay. Full AP2-over-Alipay rail compliance requires real Alipay
  settlement, which is not in scope for this submission.
- **NHI = RFC 9421 request signing.** Key material is currently loaded from
  an on-disk EC P-256 PEM (or an ephemeral in-memory key); a KMS-hardening
  seam exists but is not activated in this repo (see the root `ALICLOUD-PROOF.md`
  §3). No **SPIFFE / OIDC / DID / RFC 8693** token exchange yet — those remain
  acknowledged open edges.
- **This showcase repo ships a small, hand-authored sample dataset**, not the
  full production catalog — see the root `README.md`'s "What's not here" and
  `DATA-ATTRIBUTIONS.md`. City coverage in this repo is limited to that sample;
  thin/unknown cities degrade honestly rather than fabricate.
- **International airfare is NOT in the enforced budget.** The enforced,
  budget-checked package is lodging + insurance + fees + entry-required items.
  Intl airfare is a requirements-bounded gap (no OTA/GDS access) —
  the package is a bookable package, **not the full trip total**.
- **Pure presentation.** Prices, risk, and scores are rendered as served, never
  recomputed client-side. "Monitoring ≠ do-not-travel" — advisory tiers are
  surfaced as the backend classifies them.

---

## Relationship to other repos & sites

- The engine this app speaks to lives at this repo's root (`society/` +
  `ucp-merchant/`). Contract is the source of truth; keep both sides in sync.
- **itinerario.io** — a **separate consumer / production site**, not part of this
  submission. It is mentioned here only to disambiguate; the submission
  product is **Travel Guild**, served by this app against the engine at this
  repo's root.
