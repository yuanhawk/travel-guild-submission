"""
providers.insurance_link — InsuranceLinkProvider protocol + SeededInsuranceLinkProvider.

Follows the EXACT same seam pattern as ``providers.pricing`` and ``providers.feeds``:

    uat (default) → SeededInsuranceLinkProvider
                    Returns the existing #45-safe compare_note (booking_url=None).
                    Identical to the current ``booking_links.insurance_note()``
                    output — UAT/var-0 path is BYTE-IDENTICAL to today.

    prod            → LiveInsuranceLinkProvider from ``tg_prod.providers``
                    (make_insurance_provider factory). Returns a nationality-keyed
                    affiliate deeplink via a third-party affiliate network. DISPLAY-ONLY
                    — never touches the planning digest, day_plans, or any cache key.

VAR-0 CONTRACT
--------------
This module is DISPLAY-ONLY. No call to ``get_insurance_link_provider().build_link()``
may appear on the deterministic plan path. The output lands in
``result["booking_links"]["insurance"]`` only — a display-only block explicitly
excluded from the var-0 digest.

SECRETS
-------
No secret value ever appears here. The affiliate marker is read by NAME from the
env at call time in the PROD provider (``AFFILIATE_MARKER``). The affiliate
program ID is a public constant, NOT a secret.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from providers.edition import is_prod, load_prod_factory

# ---------------------------------------------------------------------------
# Kind constants — must be kept in sync with booking_links.ALLOWED_KINDS.
# ---------------------------------------------------------------------------
KIND_COMPARE_NOTE = "compare_note"
KIND_AFFILIATE_HANDOFF = "affiliate_handoff"   # PROD-only affiliate handoff link

# ---------------------------------------------------------------------------
# Protocol
# ---------------------------------------------------------------------------

@runtime_checkable
class InsuranceLinkProvider(Protocol):
    """Display-only insurance link overlay.

    The single method ``build_link`` returns one link dict compatible with the
    ``booking_links._link()`` schema:
        {"booking_url", "kind", "label", "providers", "provenance"}

    UAT: booking_url is None, kind == "compare_note".
    PROD: booking_url is the affiliate deeplink,
          kind == "affiliate_handoff".
    """

    def build_link(
        self,
        *,
        nationality: str | None = None,
        destination_iso2: str | None = None,
        trip_start: str | None = None,
        trip_end: str | None = None,
    ) -> dict[str, Any]:
        """Build the insurance booking-link entry.

        All parameters are optional — the provider degrades gracefully to the
        compare_note fallback when inputs are absent or the affiliate marker is
        not configured.
        """
        ...


# ---------------------------------------------------------------------------
# Seeded (UAT) provider — the #45-safe compare_note
# ---------------------------------------------------------------------------

class SeededInsuranceLinkProvider:
    """UAT/seeded insurance overlay.

    Returns the same compare_note that ``booking_links.insurance_note()``
    returns today — booking_url is None, no vendor plan is offered (#45
    boundary). Byte-identical to the current UAT output.
    """

    def build_link(
        self,
        *,
        nationality: str | None = None,
        destination_iso2: str | None = None,
        trip_start: str | None = None,
        trip_end: str | None = None,
    ) -> dict[str, Any]:
        # Deferred local import — keeps this module import-light and avoids a
        # circular import (booking_links imports from contracts; we stay above that).
        from utils.booking_links import insurance_note
        return insurance_note()


# ---------------------------------------------------------------------------
# Edition-keyed factory (mirrors get_price_provider / get_feed_provider)
# ---------------------------------------------------------------------------

_PROD_FACTORY = "make_insurance_provider"


def get_insurance_link_provider() -> InsuranceLinkProvider:
    """Return the active insurance-link overlay provider for the current edition.

    uat (default) → SeededInsuranceLinkProvider (byte-identical to today).
    prod → tries in order:
             1. The external live provider from tg_prod.providers (if importable).
             2. The in-repo prod fallback provider from providers.insurance_link_prod
                (not shipped in this public showcase repo).
             3. SeededInsuranceLinkProvider (public-repo fallback, never raises).
    """
    factory = load_prod_factory(_PROD_FACTORY)
    if factory is not None:
        try:
            provider = factory()
        except Exception:  # noqa: BLE001 — prod-construction failure → try next
            provider = None
        if provider is not None:
            return provider

    # Fallback: the prod-only provider module (insurance_link_prod.py) -- not
    # shipped in this public showcase repo; this import fails safely here and
    # falls through to the seeded UAT path.
    if is_prod():
        try:
            from providers.insurance_link_prod import InRepoLiveInsuranceLinkProvider  # noqa: PLC0415
            return InRepoLiveInsuranceLinkProvider()
        except Exception:  # noqa: BLE001 — absent or broken module → seeded fallback
            pass

    return SeededInsuranceLinkProvider()
