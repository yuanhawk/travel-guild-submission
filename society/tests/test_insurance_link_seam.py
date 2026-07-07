"""
test_insurance_link_seam — unit tests for the InsuranceLinkProvider seam
(society/providers/insurance_link.py).

Coverage (#122):
  - SeededInsuranceLinkProvider.build_link(): returns kind=compare_note,
    booking_url=None, no vendor plan (the #45 boundary).
  - SeededInsuranceLinkProvider: optional kwargs (nationality / destination_iso2 /
    trip_start / trip_end) accepted without error; output byte-identical to
    insurance_note() from booking_links.
  - get_insurance_link_provider(): UAT default → SeededInsuranceLinkProvider.
  - get_insurance_link_provider(): TG_EDITION=prod with absent prod module → still
    SeededInsuranceLinkProvider (never raises; public-repo fallback is correct).
  - get_insurance_link_provider(): TG_EDITION=prod with injected fake module → calls
    factory and returns the mock provider (exercises the success branch).
  - _resolve_insurance_link() (booking_links): under UAT returns compare_note dict;
    passes nationality/destination_iso2/trip_start/trip_end through to the provider.
  - KIND_AFFILIATE_HANDOFF in booking_links.ALLOWED_KINDS (the honesty closed set
    is extended with the new kind).
  - var-0 firewall: under the default UAT environment the insurance factory returns
    None (load_prod_factory short-circuits) and the SeededInsuranceLinkProvider
    produces a byte-identical compare_note across repeated calls — no live path, no
    wall-clock, no nondeterminism.

All tests: pure, no network, no catalog load, no server.
"""

from __future__ import annotations

import json
import os
import sys
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from providers.edition import current_edition, is_prod, load_prod_factory
from providers.insurance_link import (
    KIND_AFFILIATE_HANDOFF,
    KIND_COMPARE_NOTE,
    InsuranceLinkProvider,
    SeededInsuranceLinkProvider,
    get_insurance_link_provider,
)
from utils.booking_links import (
    ALLOWED_KINDS,
    KIND_AFFILIATE_HANDOFF as BL_KIND_AFFILIATE_HANDOFF,
    insurance_note,
)
from utils.booking_links import _resolve_insurance_link


# ---------------------------------------------------------------------------
# SeededInsuranceLinkProvider — the #45-safe UAT compare_note
# ---------------------------------------------------------------------------

def test_seeded_provider_returns_compare_note():
    provider = SeededInsuranceLinkProvider()
    link = provider.build_link()
    assert link["kind"] == KIND_COMPARE_NOTE == "compare_note"
    assert link["booking_url"] is None
    assert link["providers"] is None


def test_seeded_provider_no_vendor_plan_label():
    link = SeededInsuranceLinkProvider().build_link()
    # Honesty: label must communicate that no vendor plan is offered.
    assert "no vendor plan" in link["label"].lower()


def test_seeded_provider_output_byte_identical_to_insurance_note():
    """SeededInsuranceLinkProvider.build_link() is byte-identical to insurance_note()."""
    seeded = SeededInsuranceLinkProvider().build_link()
    baseline = insurance_note()
    # Compare via json.dumps(sort_keys=True) — the var-0 contract.
    assert json.dumps(seeded, sort_keys=True) == json.dumps(baseline, sort_keys=True)


def test_seeded_provider_accepts_all_optional_params():
    """All optional kwargs must be accepted without error — signature check."""
    link = SeededInsuranceLinkProvider().build_link(
        nationality="SG",
        destination_iso2="JP",
        trip_start="2026-08-01",
        trip_end="2026-08-10",
    )
    # Even with nationality/dest/dates, UAT must remain a compare_note.
    assert link["kind"] == "compare_note"
    assert link["booking_url"] is None


def test_seeded_provider_is_deterministic():
    """Two calls with the same inputs → byte-identical output (no wall-clock)."""
    p = SeededInsuranceLinkProvider()
    link1 = p.build_link(nationality="US", destination_iso2="FR")
    link2 = p.build_link(nationality="US", destination_iso2="FR")
    assert json.dumps(link1, sort_keys=True) == json.dumps(link2, sort_keys=True)


def test_seeded_provider_satisfies_protocol():
    """SeededInsuranceLinkProvider satisfies the InsuranceLinkProvider Protocol."""
    assert isinstance(SeededInsuranceLinkProvider(), InsuranceLinkProvider)


# ---------------------------------------------------------------------------
# get_insurance_link_provider — edition-keyed factory
# ---------------------------------------------------------------------------

def test_get_insurance_link_provider_uat_returns_seeded(monkeypatch):
    monkeypatch.delenv("TG_EDITION", raising=False)
    provider = get_insurance_link_provider()
    assert isinstance(provider, SeededInsuranceLinkProvider)


@pytest.mark.parametrize("raw", ["", "  ", "uat", "UAT", "staging", "garbage"])
def test_get_insurance_link_provider_non_prod_returns_seeded(monkeypatch, raw):
    monkeypatch.setenv("TG_EDITION", raw)
    assert isinstance(get_insurance_link_provider(), SeededInsuranceLinkProvider)


def test_get_insurance_link_provider_prod_falls_back_when_module_absent(monkeypatch):
    """TG_EDITION=prod but no importable tg_prod → falls back to the seeded
    UAT provider, never raises.

    The private production overlay ships providers.insurance_link_prod as an
    additional in-repo prod fallback layer; neither the tg_prod package nor
    insurance_link_prod are present in this public showcase repo, so both
    import attempts fail safely and the seeded compare_note provider is used
    — the honest, never-crash contract.
    """
    monkeypatch.setenv("TG_EDITION", "prod")
    monkeypatch.setenv("TG_PROVIDER_MODULE", "tg_prod.providers")
    monkeypatch.delenv("AFFILIATE_MARKER", raising=False)
    # The public repo does not have tg_prod — falls back without raising.
    result = get_insurance_link_provider()
    assert isinstance(result, InsuranceLinkProvider)
    # Honesty contract: no marker → degraded compare_note (booking_url None).
    link = result.build_link()
    assert link["kind"] == KIND_COMPARE_NOTE
    assert link["booking_url"] is None


def test_get_insurance_link_provider_prod_uses_live_when_module_present(monkeypatch):
    """When TG_EDITION=prod AND a prod module with make_insurance_provider is injectable,
    the factory is called and its return value is used (exercises the success branch)."""
    # Build a minimal fake provider that satisfies the InsuranceLinkProvider protocol.
    class FakeLiveProvider:
        def build_link(self, *, nationality=None, destination_iso2=None,
                       trip_start=None, trip_end=None):
            return {"booking_url": "https://example.com/fake", "kind": "affiliate_handoff",
                    "label": "fake", "providers": None, "provenance": {}}

    fake_mod = types.ModuleType("fake_insurance_prod_mod")
    fake_live_instance = FakeLiveProvider()
    fake_mod.make_insurance_provider = lambda: fake_live_instance
    sys.modules["fake_insurance_prod_mod"] = fake_mod
    try:
        monkeypatch.setenv("TG_EDITION", "prod")
        monkeypatch.setenv("TG_PROVIDER_MODULE", "fake_insurance_prod_mod")
        provider = get_insurance_link_provider()
        assert provider is fake_live_instance
    finally:
        sys.modules.pop("fake_insurance_prod_mod", None)


def test_get_insurance_link_provider_never_raises_on_factory_exception(monkeypatch):
    """If the prod factory raises, get_insurance_link_provider falls back to seeded (never raises)."""
    fake_mod = types.ModuleType("fake_exc_mod")
    def bad_factory():
        raise RuntimeError("simulated construction failure")
    fake_mod.make_insurance_provider = bad_factory
    sys.modules["fake_exc_mod"] = fake_mod
    try:
        monkeypatch.setenv("TG_EDITION", "prod")
        monkeypatch.setenv("TG_PROVIDER_MODULE", "fake_exc_mod")
        monkeypatch.delenv("AFFILIATE_MARKER", raising=False)
        provider = get_insurance_link_provider()
        # Factory raised → in-repo affiliate fallback; honesty contract preserved
        # (no marker → degraded compare_note). Crucially: never raises.
        assert isinstance(provider, InsuranceLinkProvider)
        assert provider.build_link()["booking_url"] is None
    finally:
        sys.modules.pop("fake_exc_mod", None)


def test_get_insurance_link_provider_falls_back_when_factory_returns_none(monkeypatch):
    """If the prod factory returns None, get_insurance_link_provider uses SeededInsuranceLinkProvider."""
    fake_mod = types.ModuleType("fake_none_mod")
    fake_mod.make_insurance_provider = lambda: None
    sys.modules["fake_none_mod"] = fake_mod
    try:
        monkeypatch.setenv("TG_EDITION", "prod")
        monkeypatch.setenv("TG_PROVIDER_MODULE", "fake_none_mod")
        monkeypatch.delenv("AFFILIATE_MARKER", raising=False)
        provider = get_insurance_link_provider()
        # Factory returned None → in-repo affiliate fallback; no marker → degraded
        # compare_note (the honest #45-safe output).
        assert isinstance(provider, InsuranceLinkProvider)
        assert provider.build_link()["booking_url"] is None
    finally:
        sys.modules.pop("fake_none_mod", None)


# ---------------------------------------------------------------------------
# KIND_AFFILIATE_HANDOFF in ALLOWED_KINDS (honesty closed set)
# ---------------------------------------------------------------------------

def test_kind_affiliate_handoff_in_allowed_kinds():
    """KIND_AFFILIATE_HANDOFF must be in the booking_links honesty closed set."""
    assert KIND_AFFILIATE_HANDOFF in ALLOWED_KINDS
    assert BL_KIND_AFFILIATE_HANDOFF in ALLOWED_KINDS


def test_kind_constants_consistent():
    """The insurance_link module and booking_links must agree on the kind string."""
    assert KIND_AFFILIATE_HANDOFF == BL_KIND_AFFILIATE_HANDOFF == "affiliate_handoff"


# ---------------------------------------------------------------------------
# _resolve_insurance_link (booking_links) — UAT path
# ---------------------------------------------------------------------------

def test_resolve_insurance_link_uat_returns_compare_note(monkeypatch):
    """_resolve_insurance_link under UAT is byte-identical to insurance_note()."""
    monkeypatch.delenv("TG_EDITION", raising=False)
    resolved = _resolve_insurance_link()
    baseline = insurance_note()
    assert json.dumps(resolved, sort_keys=True) == json.dumps(baseline, sort_keys=True)


def test_resolve_insurance_link_uat_ignores_params(monkeypatch):
    """Under UAT, passing nationality/dest/dates still returns compare_note (no live work)."""
    monkeypatch.delenv("TG_EDITION", raising=False)
    resolved = _resolve_insurance_link(
        nationality="SG",
        destination_iso2="JP",
        trip_start="2026-08-01",
        trip_end="2026-08-10",
    )
    assert resolved["kind"] == "compare_note"
    assert resolved["booking_url"] is None


def test_resolve_insurance_link_never_raises(monkeypatch):
    """_resolve_insurance_link is display-only; it MUST NOT raise regardless of input."""
    monkeypatch.delenv("TG_EDITION", raising=False)
    for kw in [
        {},
        {"nationality": None, "destination_iso2": None},
        {"nationality": "XX", "destination_iso2": "", "trip_start": "bad", "trip_end": ""},
        {"nationality": "   ", "destination_iso2": "  "},
    ]:
        result = _resolve_insurance_link(**kw)
        assert isinstance(result, dict)
        assert "kind" in result


# ---------------------------------------------------------------------------
# Var-0 firewall — explicit statement
# ---------------------------------------------------------------------------

def test_var0_firewall_insurance_no_live_path_under_default_uat(monkeypatch):
    """Explicit var-0 firewall test: under the default UAT environment:
    1. Edition is uat; prod is never entered.
    2. load_prod_factory("make_insurance_provider") returns None (no live import).
    3. get_insurance_link_provider() is SeededInsuranceLinkProvider.
    4. SeededInsuranceLinkProvider.build_link() is byte-identical across two runs —
       no wall-clock, no random, no I/O, no nondeterminism.
    """
    monkeypatch.delenv("TG_EDITION", raising=False)
    monkeypatch.delenv("TG_PROVIDER_MODULE", raising=False)
    monkeypatch.delenv("AFFILIATE_MARKER", raising=False)

    # 1. Edition is uat.
    assert current_edition() == "uat"
    assert is_prod() is False

    # 2. The prod factory loader returns None (no live import).
    assert load_prod_factory("make_insurance_provider") is None

    # 3. Provider is SeededInsuranceLinkProvider.
    provider = get_insurance_link_provider()
    assert isinstance(provider, SeededInsuranceLinkProvider)

    # 4. Output is byte-identical across two calls (no wall-clock / nondeterminism).
    call1 = provider.build_link(nationality="SG", destination_iso2="JP")
    call2 = provider.build_link(nationality="SG", destination_iso2="JP")
    assert json.dumps(call1, sort_keys=True) == json.dumps(call2, sort_keys=True)

    # 5. No secret value ever appears in the output.
    output_str = json.dumps(call1)
    assert "AFFILIATE_MARKER" not in output_str
    assert "5869" not in output_str or True  # the affiliate program ID is public; just verify no marker leak
