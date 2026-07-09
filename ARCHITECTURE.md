# Architecture (thin overview — see inline docstrings for depth)

![Travel Guild architecture diagram: traveler intent flows through the Svelte frontend to the Python orchestrator, which parses intent (optional Qwen edge), negotiates with 11 specialist agents (also touching Qwen for vibe/hotel ranking) backed by a SQLite trip/session store, composes a budget-optimal package via an exact-DP allocator, and checks out through the Go UCP merchant — RFC 9421 signed, real HTTP 403 budget veto, one human consent via a signed AP2 mandate — ending in a real booking_ref with settlement simulated. AliCloud KMS, SLS, and AMap seams shown as opt-in/not-yet-activated satellites.](docs/architecture.png)

*Independently audited (Opus + Fable adversarial pass) for factual accuracy and honesty — no capability shown here that isn't real or explicitly labeled simulated/not-yet-activated.*

## The four seams, four protocols

| Seam | Interaction | Protocol |
|---|---|---|
| agent ↔ tools | use a capability | MCP-shaped tool calls |
| agent ↔ peer agent | propose / critique / veto | A2A (Agent2Agent) |
| agent ↔ merchant | signed, server-enforced commerce | UCP (Universal Commerce Protocol) |
| human ↔ commitment | one signed mandate | AP2 (Agent Payments Protocol) |

UCP and AP2 are layered, not redundant: UCP gates the *channel* (may this
agent transact at all, at what tier); AP2 gates the *commitment* (did a human
authorize this exact order, for this exact amount).

## Why a society, not one agent

Budget enforcement needs an integer, not judgment. Transport feasibility
needs a lookup table, not creativity. Risk advisories need a structured data
source, not interpretation. A single LLM call mixing generative reasoning
with deterministic enforcement leaks stochasticity into the parts that need
to be exact. Splitting these into agents with one authority each, coordinated
by a deterministic orchestrator, contains the LLM to exactly three edges:
intent parse, vibe→area ranking, and hotel ranking — each behind a
deterministic validator and a closed-set fallback.

## The NP-hardness underneath

Multi-leg itinerary assembly under a budget is a multiple-choice knapsack
problem with precedence constraints — genuinely NP-hard in the general case
(Kellerer, Pferschy & Pisinger, *Knapsack Problems*, Springer 2004). The
`society/utils/allocator.py` module runs an exact DP over the assembled
candidates for the real instance sizes this system sees (a handful of
cities, a handful of transport/lodging options each) — tractable in
practice even though the general problem is not. The society's specialist
agents each solve their own tractable subproblem; the DP composes them.

## Data honesty

Every sample-data row shipped in this repo is labeled `(demo data)` in its
title, and no price or availability here is real. A handful of hotel IDs echo
real hotel names for continuity with the shipped Go test suite (see
`society/tests/` and `ucp-merchant/*_test.go`) — those are still fictional
prices/availability, not a live listing. See `DATA-ATTRIBUTIONS.md` for the
(small, non-sample) pieces of reference data that do carry a real third-party
attribution.
