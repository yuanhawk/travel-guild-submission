"""
providers.feeds — the live-hazard-FEED OVERLAY seam (matching FeedProvider).

CONTEXT
-------
The live hazard FEED (GDACS active-emergency + watchlist) is ALREADY a clean,
firewalled provider seam in ``utils/emergency_feed.py``: factory functions
``build_emergency_client(mode)`` and ``build_active_watchlist(mode)`` map an env
value to a provider callable, and the orchestrator/server treat the output as a
DISPLAY-ONLY overlay (it can FLAG/annotate but is fired off the var-0 plan path).

Because that extraction is already clean, this module defines the MATCHING
``FeedProvider`` Protocol + ``SeededFeedProvider`` so the prod edition gets a
symmetric, edition-keyed entry point alongside the price overlay — WITHOUT
re-routing any existing caller.

WHAT THIS DOES — AND DELIBERATELY DOES NOT — DO
-----------------------------------------------
  * It is a THIN ADAPTER over the existing emergency_feed factories. It preserves
    the EXACT current behaviour by delegating to them with the EXISTING
    ``EMERGENCY_FEED`` env value. No defaults change. No var-0 surface moves.
  * It does NOT re-wire ``server.py`` / ``orchestrator.py`` (they keep reading
    ``EMERGENCY_FEED`` directly). Unifying the feed overlay fully under
    ``TG_EDITION`` is the NEXT seam, intentionally NOT forced here (see TODO).

VAR-0 NOTE
----------
The feed overlay is already firewalled from the deterministic plan. This adapter
adds no new behaviour and changes no wiring, so it cannot perturb var-0.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Callable, Protocol, runtime_checkable

from providers.edition import load_prod_factory
from utils.emergency_feed import build_active_watchlist, build_emergency_client

# The existing env knob the live feed already reads. We delegate to it verbatim
# so behaviour is byte-identical to today (NAMES only — never a secret).
_FEED_ENV = "EMERGENCY_FEED"


@runtime_checkable
class FeedProvider(Protocol):
    """The swappable live-hazard-FEED overlay interface (display-only).

    Mirrors the two emergency_feed entry points: a per-trip active-emergency
    check and an always-on active watchlist.
    """

    def active_emergency_client(self) -> Callable[[dict], dict | None] | None:
        """A callable(query)->dict|None for a per-trip active-emergency check,
        or None when no per-trip overlay is configured (var-0 no-op)."""
        ...

    def active_watchlist_fetch(self) -> Callable[..., dict]:
        """A callable()->dict producing the always-on active watchlist."""
        ...


@dataclass(frozen=True)
class SeededFeedProvider:
    """The default feed provider — a thin adapter over emergency_feed.

    Delegates to the EXISTING ``build_emergency_client`` / ``build_active_watchlist``
    factories using the EXISTING ``EMERGENCY_FEED`` env value, so behaviour is
    identical to the current deployment (no per-trip overlay unless configured;
    the always-on watchlist is GDACS-live by default; ``EMERGENCY_FEED=stub``
    swaps the deterministic demo set). Preserves current behaviour EXACTLY.
    """

    def _mode(self) -> str:
        return os.environ.get(_FEED_ENV, "")

    def active_emergency_client(self) -> Callable[[dict], dict | None] | None:
        return build_emergency_client(self._mode())

    def active_watchlist_fetch(self) -> Callable[..., dict]:
        return build_active_watchlist(self._mode())


# Name of the factory the prod module must expose for the feed overlay.
_PROD_FACTORY = "make_feed_provider"


def get_feed_provider() -> FeedProvider:
    """Return the active feed-overlay provider for the current edition.

    uat (default) → SeededFeedProvider (delegates to emergency_feed). prod → the
    prod LiveFeedProvider if its module is importable, else SeededFeedProvider.
    Never raises.
    """
    factory = load_prod_factory(_PROD_FACTORY)
    if factory is not None:
        try:
            provider = factory()
        except Exception:  # noqa: BLE001 — any prod-construction failure → seeded fallback
            return SeededFeedProvider()
        if provider is not None:
            return provider
    return SeededFeedProvider()


# ===========================================================================
# NEXT-SEAM TODO (NOT forced here — documented per the task's feed guidance)
# ===========================================================================
# The live feed is currently selected by the EMERGENCY_FEED env knob, read
# directly in server.py / orchestrator.py. Fully unifying it under TG_EDITION
# (so prod swaps a LiveFeedProvider the same way it swaps the price provider)
# means re-pointing those call sites at get_feed_provider(). That re-route
# touches the emergency overlay path and must be proven var-0-safe (the feed is
# already firewalled, but the call-site change is non-trivial), so it is
# DEFERRED. This adapter gives prod the symmetric provider object today without
# moving any existing wiring.
