"""
test_booking_seam.py — the fail-CLOSED, deny-by-default BOOKING AUTHORITY seam.

Locks the #1 safety property: with NO prod booking provider loaded (this public /
judging repo, or TG_EDITION != prod, or the prod import failing), the seam DENIES
every mandate — it never executes, never charges, never fabricates a success, and
never raises. The refusal IS the safety property.

Coverage:
  (a) default get_booking_provider() (non-prod env) → DenyingBookingProvider
  (b) it DENIES every request — no execution, nothing booked (fail-closed sentinel)
  (c) a fake prod factory IS available → get_booking_provider uses it
  (d) a prod module that fails to import → STILL denies (never raises)
  (e) value types are immutable and carry the spend cap

Pure: no network, no credentials, no secrets, no merchant calls.
"""

from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dataclasses import FrozenInstanceError

from providers import booking as B
from providers.booking import (
    AuthorityGrant,
    BookingProvider,
    BookingRequest,
    BookingResult,
    DenyingBookingProvider,
    GrantConstraints,
    Identity,
    REASON_NO_PROVIDER,
    STATUS_DENIED,
    STATUS_EXECUTED,
    authorization_denial_reason,
    get_booking_provider,
    make_booking_provider,
)


# ---------------------------------------------------------------------------
# Fixtures — a representative grant + a request that WOULD book if allowed.
# ---------------------------------------------------------------------------

def _grant(spend_cap_cents: int = 500_00, **kw) -> AuthorityGrant:
    return AuthorityGrant(
        grant_id="g-1",
        principal=Identity(user_id="u-1", display_name="Test Traveler"),
        scope="hotel",
        spend_cap_cents=spend_cap_cents,
        currency="USD",
        valid_until="2099-12-31",
        constraints=GrantConstraints(**kw) if kw else GrantConstraints(),
    )


def _request(amount_cents: int = 100_00, **kw) -> BookingRequest:
    base = dict(
        request_id="req-1",
        scope="hotel",
        merchant="acme-hotels",
        item_ref="offer-abc",
        amount_cents=amount_cents,
        currency="USD",
        refundable=False,
    )
    base.update(kw)
    return BookingRequest(**base)


@pytest.fixture(autouse=True)
def _non_prod_env(monkeypatch):
    """Guarantee a non-prod edition for the default-path tests (the public repo)."""
    monkeypatch.delenv("TG_EDITION", raising=False)
    monkeypatch.delenv("TG_PROVIDER_MODULE", raising=False)


# ===========================================================================
# (a) default provider in a non-prod env is the DenyingBookingProvider
# ===========================================================================

def test_default_provider_is_denying():
    provider = get_booking_provider()
    assert isinstance(provider, DenyingBookingProvider)
    assert isinstance(provider, BookingProvider)


def test_make_booking_provider_symbol_returns_denying_default():
    # The in-repo factory symbol (name symmetry with the prod module) returns
    # the fail-closed default in the public repo.
    assert isinstance(make_booking_provider(), DenyingBookingProvider)


# ===========================================================================
# (b) it DENIES every request — no execution, nothing charged/booked
# ===========================================================================

def test_authorize_denies_with_sentinel():
    result = get_booking_provider().authorize(_grant(), _request())
    assert result.status == STATUS_DENIED
    assert result.reason == REASON_NO_PROVIDER
    assert result.request_id == "req-1"
    assert result.confirmation_ref is None
    assert result.is_executed is False


def test_execute_denies_and_books_nothing():
    """The fail-closed sentinel: a request that WOULD book produces 'denied',
    nothing is executed, and there is no confirmation ref (nothing booked)."""
    result = get_booking_provider().execute(_grant(), _request())
    assert result.status == STATUS_DENIED
    assert result.status != STATUS_EXECUTED
    assert result.reason == REASON_NO_PROVIDER
    assert result.confirmation_ref is None  # nothing was booked
    assert result.is_executed is False


def test_execute_denies_even_when_grant_would_permit():
    """Even a request WELL within the cap/scope/expiry is denied — the default
    never consults the grant to 'allow'; it denies unconditionally."""
    tiny = _request(amount_cents=1)  # trivially under any cap
    result = get_booking_provider().execute(_grant(spend_cap_cents=10_000_00), tiny)
    assert result.status == STATUS_DENIED
    assert result.confirmation_ref is None


def test_denying_provider_never_raises():
    """Must never raise into the caller, even with a degenerate request."""
    provider = DenyingBookingProvider()
    weird = BookingRequest(
        request_id="", scope="", merchant="", item_ref="",
        amount_cents=0, currency="", refundable=False,
    )
    r1 = provider.authorize(_grant(), weird)
    r2 = provider.execute(_grant(), weird)
    assert r1.status == STATUS_DENIED
    assert r2.status == STATUS_DENIED


# ===========================================================================
# (c) when a fake prod factory IS available → get_booking_provider uses it
# ===========================================================================

class _FakeLiveProvider(BookingProvider):
    """A stand-in for the private prod LiveBookingProvider (test-only)."""

    def authorize(self, grant, request):
        return BookingResult(status="pending", reason="authorized",
                             request_id=request.request_id)

    def execute(self, grant, request):
        return BookingResult(status=STATUS_EXECUTED, reason="ok",
                             request_id=request.request_id,
                             confirmation_ref="CONF-XYZ")


def test_uses_injected_prod_factory(monkeypatch):
    sentinel = _FakeLiveProvider()
    # Patch the factory loader in the booking module's namespace to return a
    # factory that yields our fake prod provider — exactly what the prod module's
    # make_booking_provider would do.
    monkeypatch.setattr(B, "load_prod_factory", lambda name: (lambda: sentinel))
    provider = get_booking_provider()
    assert provider is sentinel
    # And it can actually 'execute' (proving the seam handed control to prod).
    out = provider.execute(_grant(), _request())
    assert out.status == STATUS_EXECUTED
    assert out.confirmation_ref == "CONF-XYZ"


def test_prod_factory_requested_by_exact_name(monkeypatch):
    """The seam asks the prod module for the attribute named
    'make_booking_provider' — the exact name the prod repo must expose."""
    seen = {}

    def _spy(name):
        seen["name"] = name
        return None  # simulate "not available" → falls back to deny

    monkeypatch.setattr(B, "load_prod_factory", _spy)
    provider = get_booking_provider()
    assert seen["name"] == "make_booking_provider"
    assert isinstance(provider, DenyingBookingProvider)


# ===========================================================================
# (d) a prod module that fails to import → STILL denies (fail-closed, no raise)
# ===========================================================================

def test_prod_import_failure_still_denies(monkeypatch):
    monkeypatch.setenv("TG_EDITION", "prod")
    monkeypatch.setenv("TG_PROVIDER_MODULE", "tg_prod_does_not_exist_zzz")
    # Real load_prod_factory runs: import fails → returns None → we DENY.
    provider = get_booking_provider()
    assert isinstance(provider, DenyingBookingProvider)
    result = provider.execute(_grant(), _request())
    assert result.status == STATUS_DENIED
    assert result.confirmation_ref is None


def test_prod_factory_raising_on_construction_still_denies(monkeypatch):
    """If the prod factory is present but raises when constructing the provider,
    the seam still fails CLOSED (never propagates the exception)."""
    def _boom():
        raise RuntimeError("prod construction blew up")

    monkeypatch.setattr(B, "load_prod_factory", lambda name: _boom)
    provider = get_booking_provider()
    assert isinstance(provider, DenyingBookingProvider)


def test_prod_factory_returning_none_still_denies(monkeypatch):
    monkeypatch.setattr(B, "load_prod_factory", lambda name: (lambda: None))
    provider = get_booking_provider()
    assert isinstance(provider, DenyingBookingProvider)


# ===========================================================================
# (e) value types are immutable and carry the spend cap
# ===========================================================================

def test_authority_grant_is_immutable_and_carries_cap():
    g = _grant(spend_cap_cents=250_00)
    assert g.spend_cap_cents == 250_00
    assert g.currency == "USD"
    with pytest.raises(FrozenInstanceError):
        g.spend_cap_cents = 9_999_99  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        g.scope = "flight"  # type: ignore[misc]


def test_identity_and_constraints_and_request_immutable():
    ident = Identity(user_id="u-1")
    cons = GrantConstraints(refundable_only=True, allowed_merchants=("acme",))
    req = _request()
    with pytest.raises(FrozenInstanceError):
        ident.user_id = "u-2"  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        cons.refundable_only = False  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        req.amount_cents = 1  # type: ignore[misc]


def test_booking_result_immutable():
    r = BookingResult(status=STATUS_DENIED, reason=REASON_NO_PROVIDER)
    with pytest.raises(FrozenInstanceError):
        r.status = STATUS_EXECUTED  # type: ignore[misc]


def test_value_types_as_dict_are_non_secret():
    g = _grant()
    d = g.as_dict()
    assert d["spend_cap_cents"] == 500_00
    assert d["principal"]["user_id"] == "u-1"
    # No credential-shaped keys anywhere in the serialized grant.
    flat = repr(d).lower()
    for banned in ("password", "secret", "token", "card", "cvv", "api_key"):
        assert banned not in flat


# ===========================================================================
# Bonus: the shared PURE authorization predicate (what prod reuses)
# ===========================================================================

def test_authorization_predicate_permits_valid_request():
    assert authorization_denial_reason(_grant(), _request(), now="2026-07-03") is None


def test_authorization_predicate_flags_over_cap():
    over = _request(amount_cents=999_999_99)
    assert authorization_denial_reason(_grant(spend_cap_cents=500_00), over,
                                       now="2026-07-03") == "spend_cap_exceeded"


def test_authorization_predicate_flags_scope_currency_expiry():
    assert authorization_denial_reason(_grant(), _request(scope="flight"),
                                       now="2026-07-03") == "scope_mismatch"
    assert authorization_denial_reason(_grant(), _request(currency="EUR"),
                                       now="2026-07-03") == "currency_mismatch"
    g_exp = _grant()
    object.__setattr__(g_exp, "valid_until", "2020-01-01")  # force an expired grant
    assert authorization_denial_reason(g_exp, _request(), now="2026-07-03") == "grant_expired"


def test_authorization_predicate_honours_constraints():
    g = _grant(refundable_only=True)
    assert authorization_denial_reason(g, _request(refundable=False),
                                       now="2026-07-03") == "refundable_required"
    assert authorization_denial_reason(g, _request(refundable=True),
                                       now="2026-07-03") is None
    g2 = _grant(allowed_merchants=("only-this-one",))
    assert authorization_denial_reason(g2, _request(merchant="acme-hotels"),
                                       now="2026-07-03") == "merchant_not_allowed"
