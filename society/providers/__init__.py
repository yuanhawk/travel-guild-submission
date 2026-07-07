"""
society.providers — the SEEDED↔LIVE OVERLAY provider seam.

WHY THIS PACKAGE EXISTS
----------------------
The public submission edition (UAT) and the private prod edition share the SAME
deterministic core catalog (cities / lodging / POI are seeded in BOTH). The only
difference between the editions is a thin, DISPLAY-ONLY OVERLAY that prod layers
on top:

  (a) a best-PRICE / availability overlay  → providers.pricing
  (b) a live hazard FEED overlay (GDACS…)  → providers.feeds  (seam documented)

Both overlays are already firewalled from the deterministic plan: their output is
rendered for the user but NEVER fed into the engine digest, the day_plans, or any
cache key. That firewall is the VARIANCE-0 (var-0) contract.

THE EDITION SWAP (this seam)
----------------------------
`TG_EDITION` (env, default "uat") selects the provider implementation:

    uat   → SeededProvider          (this repo; preserves current seeded behaviour
                                      EXACTLY — no live overlay → "unavailable")
    prod  → LiveProvider            (dynamically loaded from the PRIVATE prod
                                      module if present; else falls back to
                                      SeededProvider — the prod module is
                                      intentionally ABSENT in this public repo)

This package ships the INTERFACE + the SeededProvider ONLY. There is NO
live-fetching code here (correct for a public repo). The prod LiveProvider lives
in the private repo and implements the contract in providers.pricing.PriceProvider.
"""
