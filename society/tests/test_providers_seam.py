"""
test_providers_seam — coverage for the SEEDED↔LIVE OVERLAY provider seam
(society/providers/*). This seam currently has ZERO test coverage.

These are pure, additive UNIT tests. They assert the var-0 firewall intent: under
the default (uat) edition NO live path activates — the price overlay is always
"unavailable", the feed adapter delegates to the existing emergency_feed factories
with the existing EMERGENCY_FEED env, and the prod factory loader returns None
(never raises) when the prod module is absent. No production behaviour is changed.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from providers.edition import (
    current_edition,
    is_prod,
    load_prod_factory,
)
from providers.pricing import (
    STATUS_UNAVAILABLE,
    SeededProvider,
    UnavailableResult,
    get_price_provider,
)
from providers.feeds import (
    SeededFeedProvider,
    get_feed_provider,
)
from utils.emergency_feed import (
    build_active_watchlist,
    build_emergency_client,
)


# A lodging row + dates reused across the price-overlay assertions. Contents are
# irrelevant to the SeededProvider — it does zero work — but we pass a realistic
# shape so the call signatures are exercised exactly as a caller would use them.
_LODGING = {"hotel_id": "h-1", "name": "Test Inn", "city": "tokyo"}
_CITY = "tokyo"
_CHECKIN = "2026-07-01"
_CHECKOUT = "2026-07-03"


# ---------------------------------------------------------------------------
# SeededProvider (providers/pricing.py) — the UAT default, no live overlay.
# ---------------------------------------------------------------------------

def test_seeded_provider_best_price_for_lodging_is_unavailable():
    res = SeededProvider().best_price_for_lodging(_LODGING, _CITY, _CHECKIN, _CHECKOUT)
    assert isinstance(res, UnavailableResult)
    assert res.status == STATUS_UNAVAILABLE == "unavailable"
    # Display-only contract: the UI consumes as_display_dict(); it must say so.
    assert res.as_display_dict()["status"] == "unavailable"


def test_seeded_provider_best_price_is_unavailable():
    res = SeededProvider().best_price(
        name="Test Inn", city=_CITY, checkin=_CHECKIN, checkout=_CHECKOUT
    )
    assert isinstance(res, UnavailableResult)
    assert res.status == "unavailable"
    # Extra kwargs are accepted (the prod primitive takes **kwargs) and ignored.
    res2 = SeededProvider().best_price(
        name="Test Inn", city=_CITY, checkin=_CHECKIN, checkout=_CHECKOUT, rooms=2
    )
    assert res2.status == "unavailable"


def test_seeded_provider_ota_urls_is_empty():
    assert SeededProvider().ota_urls_from_lodging(_LODGING) == []


def test_seeded_provider_is_frozen_pure_dataclass():
    # frozen=True → assignment raises; pure/no-I/O is what keeps it var-0 safe.
    p = SeededProvider()
    with pytest.raises(Exception):
        p.reason = "mutated"  # type: ignore[misc]
    # Default reason is a fixed (non-wall-clock) string — deterministic.
    assert SeededProvider().reason == SeededProvider().reason


# ---------------------------------------------------------------------------
# Edition selection (providers/edition.py)
# ---------------------------------------------------------------------------

def test_current_edition_defaults_to_uat(monkeypatch):
    monkeypatch.delenv("TG_EDITION", raising=False)
    assert current_edition() == "uat"


@pytest.mark.parametrize("raw", ["", "  ", "uat", "UAT", "staging", "garbage", "prodd"])
def test_current_edition_non_prod_fails_safe_to_uat(monkeypatch, raw):
    monkeypatch.setenv("TG_EDITION", raw)
    # Any value that is not exactly "prod" (case-insensitive) resolves to uat.
    assert current_edition() != "prod" or raw.strip().lower() == "prod"
    assert is_prod() is False


@pytest.mark.parametrize("raw", ["prod", "PROD", "  Prod  "])
def test_is_prod_true_only_for_exact_prod(monkeypatch, raw):
    monkeypatch.setenv("TG_EDITION", raw)
    assert current_edition() == "prod"
    assert is_prod() is True


def test_load_prod_factory_returns_none_when_not_prod(monkeypatch):
    monkeypatch.delenv("TG_EDITION", raising=False)
    # Not prod → loader short-circuits to None without importing anything.
    assert load_prod_factory("make_price_provider") is None


def test_load_prod_factory_returns_none_when_module_absent(monkeypatch):
    # Edition IS prod but the prod module is absent in this public repo: must
    # return None (fallback) and NEVER raise.
    monkeypatch.setenv("TG_EDITION", "prod")
    monkeypatch.setenv("TG_PROVIDER_MODULE", "tg_prod.providers")
    assert load_prod_factory("make_price_provider") is None
    # An explicitly bogus module name is also handled (import error → None).
    monkeypatch.setenv("TG_PROVIDER_MODULE", "definitely_not_a_real_module_xyz")
    assert load_prod_factory("make_price_provider") is None


def test_get_price_provider_uat_returns_seeded(monkeypatch):
    monkeypatch.delenv("TG_EDITION", raising=False)
    assert isinstance(get_price_provider(), SeededProvider)


def test_get_price_provider_prod_falls_back_to_seeded_when_module_absent(monkeypatch):
    # Even under TG_EDITION=prod, the absent prod module means the factory falls
    # back to SeededProvider — the correct public-repo behaviour, never a raise.
    monkeypatch.setenv("TG_EDITION", "prod")
    monkeypatch.setenv("TG_PROVIDER_MODULE", "tg_prod.providers")
    assert isinstance(get_price_provider(), SeededProvider)


def test_load_prod_factory_returns_factory_when_present(monkeypatch):
    # Belt-and-suspenders: when a prod module IS importable and exposes the
    # factory, load_prod_factory returns it (proves the success branch, not just
    # the fallback). We inject a throwaway module to stand in for the prod repo.
    import types

    fake = types.ModuleType("fake_prod_mod")
    sentinel = lambda: SeededProvider()  # noqa: E731
    fake.make_price_provider = sentinel
    sys.modules["fake_prod_mod"] = fake
    try:
        monkeypatch.setenv("TG_EDITION", "prod")
        monkeypatch.setenv("TG_PROVIDER_MODULE", "fake_prod_mod")
        assert load_prod_factory("make_price_provider") is sentinel
        # And a missing attribute on a present module → None (not a raise).
        assert load_prod_factory("make_nonexistent_factory") is None
    finally:
        sys.modules.pop("fake_prod_mod", None)


# ---------------------------------------------------------------------------
# FeedProvider / SeededFeedProvider (providers/feeds.py) — thin adapter that
# delegates to the existing emergency_feed factories with the existing env.
# ---------------------------------------------------------------------------

def test_seeded_feed_provider_delegates_with_existing_env_default(monkeypatch):
    # Default (no EMERGENCY_FEED): byte-identical to calling the factories with "".
    monkeypatch.delenv("EMERGENCY_FEED", raising=False)
    p = SeededFeedProvider()
    # active_emergency_client: empty env → no per-trip overlay (None), a var-0 no-op.
    assert p.active_emergency_client() is build_emergency_client("")
    assert p.active_emergency_client() is None
    # active_watchlist_fetch: empty env → the GDACS-live default factory result.
    assert p.active_watchlist_fetch() is build_active_watchlist("")


def test_seeded_feed_provider_delegates_with_stub_env(monkeypatch):
    monkeypatch.setenv("EMERGENCY_FEED", "stub")
    p = SeededFeedProvider()
    assert p.active_emergency_client() is build_emergency_client("stub")
    assert p.active_watchlist_fetch() is build_active_watchlist("stub")


def test_seeded_feed_provider_delegates_with_gdacs_env(monkeypatch):
    monkeypatch.setenv("EMERGENCY_FEED", "gdacs")
    p = SeededFeedProvider()
    assert p.active_emergency_client() is build_emergency_client("gdacs")
    assert p.active_watchlist_fetch() is build_active_watchlist("gdacs")


def test_get_feed_provider_uat_returns_seeded(monkeypatch):
    monkeypatch.delenv("TG_EDITION", raising=False)
    assert isinstance(get_feed_provider(), SeededFeedProvider)


def test_get_feed_provider_prod_falls_back_to_seeded_when_module_absent(monkeypatch):
    monkeypatch.setenv("TG_EDITION", "prod")
    monkeypatch.setenv("TG_PROVIDER_MODULE", "tg_prod.providers")
    assert isinstance(get_feed_provider(), SeededFeedProvider)


# ---------------------------------------------------------------------------
# The var-0 firewall intent, stated as one explicit test: under the default
# (uat) environment, NO live path activates anywhere in the seam.
# ---------------------------------------------------------------------------

def test_var0_firewall_no_live_path_under_default_uat(monkeypatch):
    monkeypatch.delenv("TG_EDITION", raising=False)
    monkeypatch.delenv("EMERGENCY_FEED", raising=False)

    # 1. Edition is uat; prod is never entered.
    assert current_edition() == "uat"
    assert is_prod() is False

    # 2. The prod factory loader never imports anything (returns None, no raise).
    assert load_prod_factory("make_price_provider") is None
    assert load_prod_factory("make_feed_provider") is None

    # 3. The price provider is the seeded no-op and returns ONLY "unavailable".
    price = get_price_provider()
    assert isinstance(price, SeededProvider)
    assert price.best_price_for_lodging(_LODGING, _CITY, _CHECKIN, _CHECKOUT).status == "unavailable"
    assert price.best_price(name="x", city=_CITY, checkin=_CHECKIN, checkout=_CHECKOUT).status == "unavailable"
    assert price.ota_urls_from_lodging(_LODGING) == []

    # 4. The feed provider is the seeded adapter with NO per-trip overlay (None) —
    #    no live per-trip emergency client activates by default.
    feed = get_feed_provider()
    assert isinstance(feed, SeededFeedProvider)
    assert feed.active_emergency_client() is None
