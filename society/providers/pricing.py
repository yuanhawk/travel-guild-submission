"""
providers.pricing — the PRICE OVERLAY provider seam (interface + SeededProvider).

WHAT THIS IS
------------
The clean, swappable interface for the best-PRICE / availability OVERLAY that the
private PROD edition layers on top of the shared seeded catalog. This public repo
ships:

  * ``PriceProvider``      — the Protocol the prod ``LiveProvider`` must implement.
  * ``BestPriceResult``    — the success result (a real live price + deeplink).
  * ``UnavailableResult``  — the honest "no live price" result.
  * ``SeededProvider``     — the UAT default. Preserves the CURRENT seeded
                             behaviour EXACTLY: there is NO live price overlay in
                             the seeded path today, so every call returns an
                             ``UnavailableResult`` and ``ota_urls_from_lodging``
                             returns ``[]``.
  * ``get_price_provider`` — edition-keyed factory (uat→Seeded, prod→Live|Seeded).

There is deliberately NO live-fetching code in this module — that lives only in
the private prod repo behind the ``PriceProvider`` contract.

THE VARIANCE-0 (var-0) CONTRACT
-------------------------------
A provider's output is a DISPLAY-ONLY overlay. It is rendered for the traveler
but MUST NEVER be fed into:

  * the deterministic engine digest,
  * the day_plans / itinerary structure,
  * any cache key.

The UAT default (SeededProvider) does zero live work and surfaces "unavailable"
for every price, which is byte-identical to today's seeded path (no overlay).
Because this seam is NOT wired into the orchestrator in this commit, the seeded
plan output is unchanged by construction. The prod edition wires the overlay into
its own DISPLAY layer only — see the WIRING TODO at the bottom of this file.

THE PROD CONTRACT (what LiveProvider must implement)
----------------------------------------------------
A ``LiveProvider`` exposing the same three methods with the same signatures:

    best_price_for_lodging(lodging, city, checkin, checkout)
        -> BestPriceResult | UnavailableResult
    ota_urls_from_lodging(lodging) -> list[dict]   # [{"name","url"}...], sorted
    best_price(*, name, city, checkin, checkout, **kw)
        -> BestPriceResult | UnavailableResult

and a module-level ``make_price_provider() -> PriceProvider`` factory in the prod
module named by ``TG_PROVIDER_MODULE`` (default "tg_prod.providers"). Results MUST
be the ``BestPriceResult`` / ``UnavailableResult`` shapes defined here (or any
object exposing the same ``status`` + ``as_display_dict()``), so the UI renders
both editions identically.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from providers.edition import load_prod_factory

# Status discriminators (closed set the UI keys off).
STATUS_OK = "ok"
STATUS_UNAVAILABLE = "unavailable"


# ===========================================================================
# Result shapes (the display contract — both editions emit these)
# ===========================================================================

@dataclass(frozen=True)
class BestPriceResult:
    """A live best-price hit for one lodging — the prod success result.

    Fields mirror the prod pricing contract exactly. ``status`` is always "ok".
    ``as_display_dict()`` is the ONLY thing the UI consumes.
    """

    hotel: str
    lowest_price_cents: int
    currency: str
    deeplink: str
    source: str
    fetched_at: str
    status: str = STATUS_OK

    def as_display_dict(self) -> dict[str, Any]:
        """The flat, UI-facing overlay dict. Display-only — never engine input."""
        return {
            "status": self.status,
            "hotel": self.hotel,
            "lowest_price_cents": int(self.lowest_price_cents),
            "currency": str(self.currency).upper(),
            "deeplink": self.deeplink,
            "source": self.source,
            "fetched_at": self.fetched_at,
        }


@dataclass(frozen=True)
class UnavailableResult:
    """An honest "no live price" result — the seeded default and prod fail-honest.

    ``status`` is always "unavailable". The UI shows the seeded snapshot / a
    "live price unavailable" note; it must NEVER imply a confirmed live fare.
    """

    reason: str
    status: str = STATUS_UNAVAILABLE

    def as_display_dict(self) -> dict[str, Any]:
        """The flat, UI-facing overlay dict. Display-only — never engine input."""
        return {
            "status": self.status,
            "reason": self.reason,
        }


PriceResult = BestPriceResult | UnavailableResult


# ===========================================================================
# The provider interface (what prod LiveProvider implements)
# ===========================================================================

@runtime_checkable
class PriceProvider(Protocol):
    """The swappable best-price OVERLAY interface.

    The seeded edition and the prod live edition both satisfy this Protocol. All
    output is DISPLAY-ONLY (see the var-0 contract in the module docstring).
    """

    def best_price_for_lodging(
        self,
        lodging: dict[str, Any],
        city: str,
        checkin: str,
        checkout: str,
    ) -> PriceResult:
        """Best live price for a seeded lodging row over [checkin, checkout)."""
        ...

    def ota_urls_from_lodging(self, lodging: dict[str, Any]) -> list[dict[str, Any]]:
        """Deterministically-ordered OTA deeplink entries (``[{"name","url"}...]``)."""
        ...

    def best_price(
        self,
        *,
        name: str,
        city: str,
        checkin: str,
        checkout: str,
        **kwargs: Any,
    ) -> PriceResult:
        """Lower-level primitive: best live price by free-text name + dates."""
        ...


# ===========================================================================
# SeededProvider — the UAT default (NO live overlay; preserves today's behaviour)
# ===========================================================================

# A fixed, non-wall-clock reason so the result is deterministic if it is ever
# surfaced. (Seeded edition does no fetch → there is nothing to time-stamp.)
_SEEDED_REASON = (
    "Seeded edition (UAT): no live price overlay — showing the seeded snapshot "
    "only. A live best-price overlay is a prod-edition feature."
)


@dataclass(frozen=True)
class SeededProvider:
    """The UAT default provider.

    Preserves the CURRENT seeded behaviour EXACTLY: the seeded path has NO live
    price overlay today, so every price query returns ``UnavailableResult`` and
    ``ota_urls_from_lodging`` returns ``[]``. Pure, deterministic, no I/O, no
    wall-clock — so it can never perturb var-0.
    """

    reason: str = _SEEDED_REASON

    def best_price_for_lodging(
        self,
        lodging: dict[str, Any],
        city: str,
        checkin: str,
        checkout: str,
    ) -> PriceResult:
        return UnavailableResult(reason=self.reason)

    def ota_urls_from_lodging(self, lodging: dict[str, Any]) -> list[dict[str, Any]]:
        # Seeded edition surfaces NO live OTA price links. (The deterministic
        # booking_links handoff in utils/booking_links.py is a SEPARATE, already
        # var-0 artifact and is intentionally untouched by this overlay seam.)
        return []

    def best_price(
        self,
        *,
        name: str,
        city: str,
        checkin: str,
        checkout: str,
        **kwargs: Any,
    ) -> PriceResult:
        return UnavailableResult(reason=self.reason)


# ===========================================================================
# Edition-keyed factory
# ===========================================================================

# Name of the factory the prod module must expose: make_price_provider().
_PROD_FACTORY = "make_price_provider"


def get_price_provider() -> PriceProvider:
    """Return the active price-overlay provider for the current edition.

    uat (default) → SeededProvider. prod → the prod LiveProvider if its module is
    importable, else SeededProvider (the public-repo fallback). Never raises.
    """
    factory = load_prod_factory(_PROD_FACTORY)
    if factory is not None:
        try:
            provider = factory()
        except Exception:  # noqa: BLE001 — any prod-construction failure → seeded fallback
            return SeededProvider()
        if provider is not None:
            return provider
    return SeededProvider()


# ===========================================================================
# WIRING TODO (deferred — NOT done in this commit to protect var-0)
# ===========================================================================
# This seam is intentionally NOT wired into the orchestrator/server in this
# commit. Wiring the overlay into the served result requires PROVING the output
# only ever lands in a DISPLAY-ONLY field (never the digest / day_plans / cache
# key). That proof is prod-display work and is deferred. When prod wires it:
#   * call get_price_provider().best_price_for_lodging(...) in the DISPLAY/render
#     layer only;
#   * attach result.as_display_dict() under a clearly display-only key
#     (e.g. leg["price_overlay"]) that the engine digest excludes;
#   * never feed lowest_price_cents into Budget's veto or any cache key.
