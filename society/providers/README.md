# society/providers — seeded↔live overlay provider seam

The clean, edition-keyed seam between the public **UAT** edition (seeded data only)
and the private **PROD** edition (live overlays). Everything here produces a
**DISPLAY-ONLY overlay**: provider output is rendered for the traveler but is
**never** fed into the deterministic engine digest, the `day_plans`, or any cache
key. That firewall is the **variance-0 (var-0)** contract. This public repo ships
the interfaces + the seeded defaults only — no live-fetching code lives here.

| File | Purpose |
| --- | --- |
| `edition.py` | Reads the `TG_EDITION` env knob (default `uat`) and dynamically loads the prod provider factory; `uat`→in-repo SeededProvider, `prod`→LiveProvider from the private `TG_PROVIDER_MODULE` (absent here → falls back to SeededProvider with a warning). |
| `pricing.py` | The price/availability overlay seam: `PriceProvider` Protocol, `BestPriceResult`/`UnavailableResult` shapes, and `SeededProvider` (UAT default — no live overlay exists in the seeded path, so every call returns "unavailable" and no OTA URLs). Live fetching lives only in the private prod repo. |
| `feeds.py` | The live-hazard-FEED overlay seam: `FeedProvider` Protocol + `SeededFeedProvider`, a thin adapter over the existing `utils/emergency_feed.py` (GDACS) factories. Delegates verbatim to the current `EMERGENCY_FEED` env value; does not re-wire `server.py`/`orchestrator.py`. |
| `__init__.py` | Package docstring explaining the seam, editions, and var-0 firewall (index file). |

Notes for reviewers: the SeededProvider path is byte-identical to today's seeded
behaviour (no live price overlay). Seeded lodging prices elsewhere in the repo are
tagged demo estimates; this seam is the swap point where a real live provider
would replace them in the private prod edition.
