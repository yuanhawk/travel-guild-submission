"""
fx_provider.py — PHASE-0 FX PROVIDER SEAM (§18 provider layer, §19.5 audit).

Design contract: the internal design spec §18 (the sourcing substrate —
reasoning ⊥ sourcing; LayeredProvider Tier-1 cache → Tier-2 live → Tier-3
exhausted; provenance; never-hallucinate), §19.1 (FX/currency normalization has
NO owner — GAP), §19.5 ("FX-owner = §18 provider seam confirmed: provider emits
{native_cents, fx_rate, fx_source, fetched_at, usd_cents}; Budget consumes
usd_cents; Critic verifies rate freshness").

WHAT THIS IS
------------
The §19.1 reconcile found currency normalization had no owner: Compliance,
Health, Loyalty, Connectivity all emit non-USD integer cents, but nothing
normalized them to the merchant's USD-cents veto. This module is the owner: a
§18 provider-layer adapter that converts a native-currency amount to a
provenance-tagged USD-cents value.

  Budget consumes `usd_cents` for the veto. Budget NEVER decides a rate.

RECONCILED WITH multi-city's currency rider (NOT duplicated)
------------------------------------------------------------
The merged multi-city build (intent_parser.py §16.1) added a LIGHT seeded FX
table (SEEDED_FX_USD_PER_UNIT) + a currency rider that normalises a free-text
budget ("AUD 3000") to USD cents at the front door. Phase-0 builds the FX seam
ON that table — it does NOT define a second set of rates. This module IMPORTS
intent_parser.SEEDED_FX_USD_PER_UNIT as the single source of truth and wraps it
in the §18 provider shape (provenance, tier, the full emit contract). The
front-door rider is then re-pointed at this seam via a thin shim
(`convert_to_usd_cents`) that preserves intent_parser's EXACT prior numeric
behaviour (same int(round(usd*100)) math) → multi-city stays variance-0.

Tier model (§18): rates are Tier-1 SEEDED today (reproducible, §13). A Tier-2
live-rate swap is deferred — it changes only the rate SOURCE behind this seam,
not the emit shape or any consumer. Tier-3 EXHAUSTED is an honest decline
(unknown currency → None / FXResult.exhausted), NEVER a silent treat-as-USD.

Security: no secrets, no network (Tier-2 deferred), pure deterministic Python.
"""

from __future__ import annotations

import logging
from typing import Any

from core.contracts import SourceTier, make_money, make_provenance

# Single source of truth for the seeded rates: the multi-city currency rider's
# table. We IMPORT it (not re-declare) so there is exactly ONE FX table in the
# codebase and multi-city behaviour is preserved by construction.
from utils.intent_parser import FX_PROVENANCE, SEEDED_FX_USD_PER_UNIT

logger = logging.getLogger(__name__)

# The fixed capture date of the seeded snapshot (matches FX_PROVENANCE text).
# Seeded → deterministic → variance-0 (§13). Tier-2 live swap would set this
# per-fetch instead.
FX_FETCHED_AT = "2026-01-01"
FX_SOURCE_URL: str | None = None  # purely-internal seeded table (no external URL)


class FXResult:
    """
    The result of an FX normalization — the §19.5 provider emit contract.

    Fields:
        native_cents : int   — the amount in its native currency, integer cents.
        currency     : str   — upper-case ISO-4217 code.
        fx_rate      : float  — USD per 1 native unit (1.0 for USD).
        fx_source    : str    — provenance string for the rate.
        fetched_at   : str    — ISO date the rate was captured (seeded snapshot).
        usd_cents    : int    — the value Budget feeds to the merchant veto.
        tier         : str    — SourceTier (cache/seeded today; live later).
        exhausted    : bool    — True iff the currency is unknown (honest decline).
    """

    __slots__ = (
        "native_cents", "currency", "fx_rate", "fx_source",
        "fetched_at", "usd_cents", "tier", "exhausted",
    )

    def __init__(
        self,
        native_cents: int,
        currency: str,
        fx_rate: float,
        fx_source: str,
        fetched_at: str,
        usd_cents: int,
        tier: str,
        exhausted: bool = False,
    ) -> None:
        self.native_cents = native_cents
        self.currency = currency
        self.fx_rate = fx_rate
        self.fx_source = fx_source
        self.fetched_at = fetched_at
        self.usd_cents = usd_cents
        self.tier = tier
        self.exhausted = exhausted

    def to_emit(self) -> dict[str, Any]:
        """
        The flat §19.5 provider emit shape:
          {native_cents, fx_rate, fx_source, fetched_at, usd_cents}
        (+ currency, tier, exhausted for completeness). Budget reads usd_cents.
        """
        return {
            "native_cents": self.native_cents,
            "currency": self.currency,
            "fx_rate": self.fx_rate,
            "fx_source": self.fx_source,
            "fetched_at": self.fetched_at,
            "usd_cents": self.usd_cents,
            "tier": self.tier,
            "exhausted": self.exhausted,
        }

    def to_money(self) -> dict[str, Any]:
        """
        The canonical contracts.make_money() currency-cents tag — the shared
        shape (§19.1) other agents validate with contracts.validate_money().
        """
        return make_money(
            native_cents=self.native_cents,
            currency=self.currency,
            usd_cents=self.usd_cents,
            fx_rate=self.fx_rate,
            fx_source=self.fx_source,
            fetched_at=self.fetched_at,
        )

    def provenance(self) -> dict[str, Any]:
        """The canonical contracts.make_provenance() envelope for this rate."""
        return make_provenance(
            source=self.fx_source,
            fetched_at=self.fetched_at,
            tier=self.tier,
            source_url=FX_SOURCE_URL,
            ttl=None,  # fixed seeded constant (no expiry); Tier-2 would set a TTL
        )


def supported_currencies() -> tuple[str, ...]:
    """The closed set of currencies the seed knows, as upper-case ISO codes."""
    return tuple(sorted(c.upper() for c in SEEDED_FX_USD_PER_UNIT))


def normalize(native_cents: int, currency: str) -> FXResult:
    """
    Normalize a native-currency integer-cents amount to provenance-tagged USD
    cents — the FX provider seam's single entry point.

    Args:
        native_cents: amount in the native currency, integer cents (>= 0).
        currency:     ISO-4217 code (case-insensitive); "USD" is identity.

    Returns:
        FXResult. For an UNKNOWN currency → FXResult.exhausted=True, usd_cents=0,
        tier="exhausted" (Tier-3 honest decline — NEVER silently USD).

    Determinism: fixed seeded rates → same input → byte-identical FXResult.
    The USD-cents math intentionally mirrors intent_parser._convert_to_usd_cents
    so the front-door rider routed through here yields IDENTICAL numbers.
    """
    code = str(currency).strip().lower()
    rate = SEEDED_FX_USD_PER_UNIT.get(code)

    if native_cents < 0:
        native_cents = 0

    if rate is None:
        # Tier-3 EXHAUSTED — honest decline, never treat as USD (§18 / §16.1).
        logger.warning(
            "utils.fx_provider.normalize: unknown currency %r — Tier-3 exhausted "
            "(honest decline, NOT treated as USD)", currency,
        )
        return FXResult(
            native_cents=native_cents,
            currency=code.upper(),
            fx_rate=0.0,
            fx_source=FX_PROVENANCE,
            fetched_at=FX_FETCHED_AT,
            usd_cents=0,
            tier=SourceTier.EXHAUSTED.value,
            exhausted=True,
        )

    # USD-cents = native_dollars * rate * 100, rounded — identical to the
    # front-door rider's int(round(amount * rate * 100)) (native_cents/100 = amount).
    usd_cents = int(round((native_cents / 100.0) * rate * 100))

    if code != "usd":
        logger.info(
            "utils.fx_provider.normalize: %d %s¢ → %d USD¢ (rate=%g, tier=seeded, %s)",
            native_cents, code.upper(), usd_cents, rate, FX_PROVENANCE,
        )

    return FXResult(
        native_cents=native_cents,
        currency=code.upper(),
        fx_rate=float(rate),
        fx_source=FX_PROVENANCE,
        fetched_at=FX_FETCHED_AT,
        usd_cents=usd_cents,
        tier=SourceTier.SEEDED.value,
        exhausted=False,
    )


def convert_to_usd_cents(amount: float, currency: str) -> int | None:
    """
    Front-door RECONCILIATION shim — the exact contract of
    intent_parser._convert_to_usd_cents, now backed by this seam.

    intent_parser routes its currency rider through THIS function so there is a
    single FX implementation. Preserves the prior behaviour BYTE-FOR-BYTE:
      - amount is in native DOLLARS (not cents).
      - returns positive USD cents, or None for unknown currency / non-positive.

    This is what keeps the merged multi-city behaviour variance-0 while removing
    the duplicate conversion path.
    """
    if amount <= 0:
        return None
    native_cents = int(round(amount * 100))
    result = normalize(native_cents, currency)
    if result.exhausted or result.usd_cents <= 0:
        return None
    return result.usd_cents
