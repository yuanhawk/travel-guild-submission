# society/utils

Shared, mostly-pure helper modules used by the orchestrator and agents. Unless noted,
modules on the booking path are deterministic (variance-0): no LLM, clock, network, or
randomness, so the same input yields byte-identical output. LLM/live/notify modules are
deliberately firewalled OFF the deterministic path. Per-file index below.

## NL parsing & LLM seams (fuzzy front only; numbers never come from the LLM)
| File | Purpose |
|------|---------|
| `intent_parser.py` | Free-text request → validated `trip_request`; LLM (DashScope qwen) gives qualitative leg/vibe only, nights/budget/dates derived deterministically; regex fallback. |
| `followup_parser.py` | Parses conversational follow-ups into a bounded, clamped delta; `apply_delta` is a pure re-plan over the unchanged core. |
| `itinerary_narration.py` | Grounded data contract + anti-fabrication validator: only real booked place ids may surface in the narrative (honesty firewall). |
| `itinerary_narrator.py` | LLM narrator seam (DashScope qwen3.x) producing day-by-day prose; cosmetic, opt-in, re-validated against the corpus; None on any failure. |
| `aftercare_lang.py` | Nationality→language map + optional DASHSCOPE translation of alerts; returns None (English original) on failure, never fabricates. |
| `vertex_gemini.py` | OFFLINE build-time only: grounded Vertex Gemini client to seed/enrich the SIMULATED demo catalog; provenance-tagged; not in the runtime stack. |

## Geo, places & transport
| File | Purpose |
|------|---------|
| `hotel_geocode.py` | Disk-cached Nominatim geocoder attaching hotel map pins (additive); live lookups gated, city-centroid fallback. |
| `intracity_transport.py` | Deterministic intra-city transfer hops with honest estimate labels (hotel uses city centroid, never reverse-geocoded). |
| `region_expansion.py` | Curated registry expanding a regional phrase ("central japan") into route-ordered bookable catalog cities; pace-aware count. |
| `places_card.py` | Server-proxied Google Places detail card/autocomplete for the map popup; key stays server-side, gated, cached, degrades to "unavailable". |
| `places_status.py` | OFFLINE catalog-freshness check via Google Places business-status; gated; conservative "unverified" matching. |
| `booking_links.py` | Pure builder of OUTBOUND handoff/deep links (official site, meta-search, maps, gov/CDC portals); never implies a confirmed booking, no fares embedded. |

## Budget, currency & money assembly
| File | Purpose |
|------|---------|
| `allocator.py` | Exact-optimal multiple-choice-knapsack DP picking the globally budget-optimal hotel-per-leg combination. |
| `budget_estimate.py` | Deterministic budget-guidance helpers (suggest a range / shortfall) over pre-computed USD cents; no price re-derivation. |
| `line_item_assembler.py` | Budget's idempotent, keyed assembler ordering multi-agent fee line items into one checkout so re-emission can't double-charge. |
| `currency_advisory.py` | INDICATIVE seeded FX rates (snapshot 2026-06) for display/timing guidance only; never affects the USD booking veto. |
| `fx_provider.py` | Provider-seam adapter normalizing a native-currency amount to provenance-tagged USD cents for the budget veto. |
| `supper_order.py` | Opt-in deterministic selector for the FOOD_DELIVERY UCP checkout kind; selects only real seeded merchant rows (simulated inventory), never fabricates. |

## Risk, safety, insurance & recovery
| File | Purpose |
|------|---------|
| `emergency_feed.py` | LIVE active-emergency provider clients (stub demo + real GDACS) firewalled off var-0; off by default; honest "unavailable", never a fabricated all-clear. |
| `aftercare_monitor.py` | Read-only proactive risk monitoring for already-BOOKED trips; suggest-only, no transactional calls; honest unavailable/beta. |
| `peril_crosswalk.py` | Total deterministic map from Risk reason-codes to canonical insurance peril classes (vocabulary translation only). |
| `coverage_gap.py` | Informational gap analysis of the user's OWN declared policy vs trip perils; generic coverage-type hints, no vendor/quote/advice. |
| `recovery.py` | Reactive disruption recovery (hotel sold-out): activate pre-vetted secondary, re-verify, enforce budget, require ONE fresh human re-consent + new mandate. |

## Identity, signing & plan editing
| File | Purpose |
|------|---------|
| `ucp_signing.py` | Client-side RFC 9421 / ES256 request signing, byte-compatible with the Go merchant verifier; OFF by default, additive (unsigned stays byte-identical). |
| `profile_hydrate.py` | Anonymous-first hydration of identity defaults from an opt-in saved profile; explicit request always wins. |
| `replan_ops.py` | Pure item-level edit ops for `/replan` over the held plan; never fabricates a POI or touches the money path. |

## Telemetry, logging & notifications (off-path, best-effort)
| File | Purpose |
|------|---------|
| `telemetry.py` | Off-by-default emit-only trip telemetry → local JSONL + optional Alibaba SLS; never read back, every failure swallowed. |
| `gap_demand_log.py` | Observe-only JSONL of requests the system honestly couldn't satisfy (anonymized request shape); read only by the offline aggregator. |
| `telegram_notify.py` | Suggest-only, send-only Telegram sender (URL buttons, no callback/inbound command path); allowlisted; no transactional symbols. |
