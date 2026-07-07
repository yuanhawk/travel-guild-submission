# society/core

Phase-0 shared contracts, types, and constants for the Travel Guild (Track-3 A2A)
orchestrator. Deliberately holds NO domain logic — only the seams, closed
vocabularies, and deterministic validators that every specialist agent composes
against, so correctness-variance stays ≈ 0 (var-0).

| File | Purpose |
| --- | --- |
| `contracts.py` | The one locked set of cross-agent seams: provenance envelope, source-tier enum, currency-cents tagging (FX shape only — provider lives elsewhere), canonical peril/risk reason-code enums, critical-bag classifier, YF-cert handoff, frontier-event shape, place keyspace, counterparty/traveler identity. Shared vocabulary + deterministic validators only; numbers never come from the LLM. |
| `cost_basis.py` | Pure stdlib basis-disclosure layer (#42). Stamps each cost line with an honest, closed-set basis discriminator (`ucp_prepaid` \| `deterministic_estimate` \| `handoff` \| `unknown`) plus a human label. Only `ucp_prepaid` (a SIMULATED merchant checkout) may say "prepaid"; seeded fees are honest demo estimates, transport is handoff. Adds only deterministic strings — no cents recomputed (var-0). |
| `trace.py` | Side-channel trace-event layer for the orchestrator. `NoOpTracer` (default, zero-overhead) keeps `negotiate()` output byte-identical; `CollectingTracer` buffers immutable `TraceEvent`s for observers. Includes the `wallet` event for the SIMULATED prepaid wallet. |

(`__init__.py` / `__pycache__` omitted.)
