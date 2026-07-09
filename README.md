# Travel Guild — backend engine (public showcase)

**Safe autonomous agentic commerce, proven on travel: a society of specialist
agents with hard checks & balances on the money path, so autonomous spend is
deterministic and bounded *by construction* — not by trusting a prompt.**

*Track 3 (Agent Society) · Qwen Cloud Global AI Hackathon · AGPL-3.0 · runs cloud-free*

> **Travel Guild** is the submission product name. `society/` is the internal
> codename for the agent core and stays unchanged in the code.

> **Honest scope (read this first):**
> This is a **public showcase extract**, not the full production repo. The
> competitive-moat curated data catalog and the private seeding/enrichment
> pipeline are **withheld** and replaced here with a small, hand-authored,
> honestly-labeled **sample dataset** (Bali + a few other cities) so the whole
> stack still boots and books a real trip end-to-end. Some tuned LLM prompt
> bodies are replaced with generic placeholders (same JSON contract, different
> wording) for the same reason — see `agents/*.py` for the inline notes at
> each redaction. Payment **settlement is SIMULATED** (sandbox checkout, no
> real payment rail). **AP2** here is the **mandate protocol + simulated
> settlement — not full-AP2-compliant**. **NHI** signing uses **long-lived
> on-disk EC P-256 keys** today (a KMS-hardening seam is documented but not
> yet activated — see `ALICLOUD-PROOF.md` §KMS). International airfare is
> **not** in the enforced budget (no GDS/OTA access) — the priced package is
> the in-destination *bookable* package, not the full trip cost.

---

## What it is

One free-text-shaped intent — *"6 days Bali, ~$1500, solo, beaches +
culture"* — goes in; a negotiated, **bookable**, full-package itinerary that
needs **exactly one human consent** comes out. A narrated terminal transcript
(`./demo.sh`) gives a cloud-free, no-keys walkthrough against the sample data.

The contribution is **structure**, not "a bigger LLM." A single LLM agent is a
non-deterministic, self-policing cashier: on the same feasible trip it can
fail to book in up to ~30% of runs, hallucinate a price into a charge, or
silently book 1 of 3 legs and call it done. Travel Guild clamps the LLM
behind deterministic validators and an independent merchant, giving **five
structural checks** no single-prompt agent has:

| # | Check | How it's enforced |
|---|---|---|
| 1 | **Can't overspend** | the **Go merchant** (a real service, not the LLM) enforces the budget via an HTTP **403** the agents cannot override |
| 2 | **Can't hallucinate a charge** | the **Critic** re-verifies every price against the backend before any commit |
| 3 | **Can't silently partial-book** | **all-or-none** + Critic coverage gate; an incomplete package → honest `cannot_satisfy` |
| 4 | **Can't spend without authorization** | **one human consent** per package; any swap/recovery needs fresh re-consent (the AP2 mandate binds the checkout) |
| 5 | **Can't flake** | LLM reasoning is clamped behind deterministic validators → correctness **variance ≈ 0** across runs |

Twelve specialist agents (Planner, Budget, Destination, Accommodation,
Transport, Day-planner, Critic, Risk, Insurance, Visa/Compliance, Health,
Fraud), each a single non-overlapping authority — plus the orchestrator's
one-consent gate as the architecture's thirteenth safety property (not a peer
agent module).

---

## Repository map (orient in 10 seconds)

```
society/                the agent core (internal codename)
  agents/                12 specialist A2A agents — one authority each
  orchestration/         negotiation orchestrator + HTTP server
  providers/             the var-0 firewall seam (edition.py), the fail-closed
                         booking-authority seam (booking.py)
  utils/                 parsers, allocator (exact-DP knapsack), pricing,
                         model routing, RFC 9421 signing
  core/                  shared contracts, cost-basis helpers
  tests/                 curated test subset (green on the sample data;
                         tests requiring the full private catalog/network
                         data are not shipped here — see "What's not here")
  poi_catalog.json       SAMPLE data — hand-authored, honestly labeled
  city_coords.json       SAMPLE data
  city_state.json        SAMPLE data (tiny, attributed — see DATA-ATTRIBUTIONS.md)
ucp-merchant/            Go UCP merchant: real HTTP-403 budget veto, RFC 9421
                         signing, W3C-VC mandate envelopes, simulated
                         checkout/wallet, world-simulator faults
  catalog.json           SAMPLE data (also a Go compile-time embed)
  food_catalog.json      SAMPLE data (also a Go compile-time embed)
demo.py / demo.sh        the narrated end-to-end demo — real merchant, real
                         booking_ref, no LLM key required (LLM-off path)
ALICLOUD-PROOF.md        evidence the backend is deployed live on Alibaba Cloud
DATA-ATTRIBUTIONS.md     licensing/attribution notes for any third-party-
                         derived reference data actually shipped
web/                     the judge-facing frontend — Svelte + TypeScript +
                         Vite + MapLibre GL, talks to the engine above over
                         HTTP/SSE. See web/README.md for its own quickstart.
```

## What's not here (and why)

| Withheld | Why | What's here instead |
|---|---|---|
| The curated 12,000+-city lodging/POI catalog | Competitive moat + it's a derived OSM extract (ODbL share-alike) | A small, hand-authored, honestly-labeled sample (Bali + a few cities) at the same file paths |
| `reference/henley_passport_index.json` (visa/passport-free reference baseline) | Not needed to run the demo; kept out to keep the sample data footprint small | Nothing — compliance degrades honestly (a `Henley index unreadable` warning on startup, passport-strength annotation disabled); doesn't block any other feature |
| `reference/seeding/*` (the enrichment pipeline) | Competitive moat — this is how the catalog gets built | Nothing; not needed to run the demo |
| Tuned LLM prompt bodies for ranking edges | Competitive prompt-engineering surface | Generic placeholders with the identical JSON contract — see the inline note at each one |
| Real HSR/ferry/air network data, region-hazard coverage data | Same moat/license reasoning as the catalog | Not shipped; transport/risk logic degrades honestly (conservative flag) outside the sample cities |
| Benchmark corpora + results | Reveals internal test cases and competitor comparisons | Not shipped in this repo |
| Prod-only deploy overlay, staging infra (both halves of this repo) | Private operational concern, not part of the showcase | Not shipped |
| `web/`'s captured real API-response fixtures (`golden_envelopes`) | Same catalog-moat data, captured via the frontend's own test fixtures | Dropped along with the one coverage test that consumed them (see `web/README.md`) |

None of this is hidden by omission — every withheld category is a deliberate,
documented redaction, not a bug. The point of this repo is to show the
**architecture** running for real, not to reproduce the full private stack.

## Quickstart

```bash
# 1. Go merchant (real HTTP-403 budget veto, simulated wallet/checkout)
cd ucp-merchant && go build -o ucp-merchant . && \
  UCP_UNSIGNED_TIER=L2 ./ucp-merchant   # demo convenience; production requires signed requests

# 2. Python engine (separate terminal)
cd society && pip install -r requirements.txt
UCP_UNSIGNED_TIER=L2 python3 demo.py     # narrated end-to-end terminal demo, LLM-off, no keys needed
```

`UCP_UNSIGNED_TIER=L2` is a documented demo convenience (unsigned requests
default to browse-only L1; the demo needs checkout-tier L2). Production
deployments require RFC 9421-signed requests instead.

To run the LLM-on edges (intent parse / area ranking / accommodation ranking)
live against Qwen: set `DASHSCOPE_API_KEY` and see `society/utils/model_router.py`.

### Running the web dashboard instead of the terminal demo

The terminal demo above (`demo.py`) is one way to exercise the engine. To
drive it from the actual judge-facing web UI instead:

```bash
# with the Go merchant already running (step 1 above):
cd society
UCP_MERCHANT_URL=http://127.0.0.1:8090/api/ucp/mcp UCP_UNSIGNED_TIER=L2 \
  python3 -m uvicorn orchestration.server:app --host 127.0.0.1 --port 8080

# separate terminal:
cd web && npm install && npm run dev   # http://localhost:5173
```

Verified end-to-end: `web/scripts/contract-check.mjs` confirms the frontend's
expected API shape matches this server's live responses.

## Tests

```bash
cd society && pytest tests/ -q
```

2800+ tests pass against the sample data. Tests that assert on the full
private catalog's scale/richness (world-city vocabulary coverage, real
transport-network completeness, real curated POI names) are not included —
see "What's not here" above.

## Deployment

Live on Alibaba Cloud ECS, Qwen served via DashScope — see `ALICLOUD-PROOF.md`
for the evidence and the KMS-hardening roadmap for the signing key.

## License

AGPL-3.0 — see `LICENSE`. Network-copyleft: anyone running a modified version
as a network service must publish their modifications under the same license.
