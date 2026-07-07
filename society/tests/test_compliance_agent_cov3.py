"""
test_compliance_agent_cov3.py — coverage-targeted tests for the Compliance agent.

Targets the uncovered regions of society/agents/compliance_agent.py:
  - 983-985    : _load_henley_index() exception handling (silent {} fallback)
  - 1258-1264  : fetch_entry_rule() simulate_unreachable + no nat, specific-only dest
  - 1266-1272  : fetch_entry_rule() simulate_unreachable + matching rule (SEEDED_FALLBACK)
  - 1505-1530  : visa_fee_line_item() — USD path + FX-exhaustion honest decline
  - 1588-1603  : gate_leg_best_passport() multi-passport selection + tie-break
  - 1823-1858  : explain_block() early-return + blocked headline formatting
  - 1878-1887  : validate_rationale() input + softening-phrase checks
  - 1896-1924  : _llm_rationale() no-key / error / validation-fail / success paths

Pure + deterministic / var-0-safe: imports the module and calls functions directly
with crafted inputs. No live LLM, no network, no paid API, no real merchant.
All seed-dependent assertions probed against the shipped seed (TH/US/ET/AO/UA).
"""

from __future__ import annotations

import os
import sys

import pytest

# society/conftest.py already puts society/ on sys.path; belt-and-suspenders.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agents import compliance_agent as ca  # noqa: E402
from agents.compliance_agent import (  # noqa: E402
    _load_henley_index,
    fetch_entry_rule,
    visa_fee_line_item,
    gate_leg_best_passport,
    check_eligibility,
    explain_block,
    validate_rationale,
    _llm_rationale,
)

TODAY = "2026-06-23"


# ---------------------------------------------------------------------------
# _load_henley_index() — exception handling (lines 983-985)
# ---------------------------------------------------------------------------

def test_load_henley_index_oserror_fallback():
    """open() raising OSError → silent {} fallback (annotation disabled, never wrong)."""
    def _boom(*a, **k):
        raise OSError("disk gone")

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr("builtins.open", _boom)
        assert _load_henley_index() == {}


def test_load_henley_index_json_decode_error_fallback():
    """json.load raising JSONDecodeError → silent {} fallback."""
    import json as _json

    def _bad_load(*a, **k):
        raise _json.JSONDecodeError("bad", "doc", 0)

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(ca.json, "load", _bad_load)
        assert _load_henley_index() == {}


def test_load_henley_index_value_error_fallback():
    """json.load raising ValueError → silent {} fallback."""
    def _bad_load(*a, **k):
        raise ValueError("nope")

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(ca.json, "load", _bad_load)
        assert _load_henley_index() == {}


# ---------------------------------------------------------------------------
# fetch_entry_rule() — simulate_unreachable logging (lines 1258-1272)
# ---------------------------------------------------------------------------

def test_fetch_entry_rule_simulate_unreachable_no_nat_specific_present(caplog):
    """Empty nationality against a specific-only dest (AO) + simulate_unreachable
    → logs the Tier-3 warning and returns (None, SEEDED_FALLBACK) (no silent allow)."""
    caplog.clear()
    with caplog.at_level("WARNING"):
        rule, freshness = fetch_entry_rule("AO", "", simulate_unreachable=True)
    assert rule is None
    assert freshness == "SEEDED_FALLBACK"
    assert any("unreachable" in r.message or "SEEDED_FALLBACK" in r.message
               for r in caplog.records)


def test_fetch_entry_rule_simulate_unreachable_with_rule(caplog):
    """A matching rule (TH/US wildcard visa-free) + simulate_unreachable
    → logs the warning and returns (rule, SEEDED_FALLBACK)."""
    caplog.clear()
    with caplog.at_level("WARNING"):
        rule, freshness = fetch_entry_rule("TH", "US", simulate_unreachable=True)
    assert rule is not None
    assert rule["kind"] == ca.EntryKind.VISA_FREE
    assert freshness == "SEEDED_FALLBACK"
    assert any("unreachable" in r.message for r in caplog.records)


def test_fetch_entry_rule_without_simulate_unreachable_is_seeded():
    """No simulate flag → live freshness tag is 'SEEDED' (not the fallback tag)."""
    rule, freshness = fetch_entry_rule("TH", "US")
    assert rule is not None
    assert freshness == "SEEDED"


# ---------------------------------------------------------------------------
# visa_fee_line_item() — USD path + FX exhaustion (lines 1505-1530)
# ---------------------------------------------------------------------------

def test_visa_fee_line_item_usd_no_fx():
    """USD fee → line item assembled with usd_cents == native fee (no FX seam needed)."""
    lv = {
        "fee_cents": 2100, "currency": "USD", "program": "ESTA",
        "dest_country": "US", "dest_name": "United States",
        "provenance": {"source": "seed:test"},
    }
    li = visa_fee_line_item(lv, "leg-0")
    assert li is not None
    assert li["usd_cents"] == 2100
    assert li["kind"] in (ca.LineItemKind.VISA.value, "visa")
    assert li["leg"] == "leg-0"


def test_visa_fee_line_item_unknown_currency_fx_exhausted():
    """An unknown currency → FX seam exhausted → honest decline (None), gate unaffected."""
    lv = {"fee_cents": 5000, "currency": "ZZ_FAKE", "program": "Mystery",
          "dest_country": "XX"}
    assert visa_fee_line_item(lv, "leg-fx") is None


def test_visa_fee_line_item_zero_fee_returns_none():
    """A zero/visa-free fee → None (no line item emitted)."""
    assert visa_fee_line_item({"fee_cents": 0, "currency": "USD"}, "leg-z") is None


# ---------------------------------------------------------------------------
# gate_leg_best_passport() — multi-passport selection (lines 1588-1603)
# ---------------------------------------------------------------------------

def test_gate_leg_best_passport_selects_best_outcome():
    """TH with NG/US/ZZ → US wins (visa-free, allowed), surfacing the considered set."""
    best = gate_leg_best_passport(
        dest_country="TH", nationalities=["NG", "US", "ZZ"],
        departure_date="2026-12-01", today=TODAY,
    )
    assert best["selected_passport"] == "US"
    assert best["allowed"] is True
    assert best["kind"] == ca.EntryKind.VISA_FREE
    assert best["passports_considered"] == ["NG", "US", "ZZ"]


def test_gate_leg_best_passport_empty_nationalities():
    """No nationalities → falls back to [''] (conservative unknown verdict)."""
    best = gate_leg_best_passport(
        dest_country="TH", nationalities=[],
        departure_date="2026-12-01", today=TODAY,
    )
    assert best["selected_passport"] == ""
    assert best["passports_considered"] == [""]


def test_gate_leg_best_passport_stable_sort_on_tie():
    """Two passports both visa-free for TH (US, UA) → alphabetic min 'UA' wins on tie."""
    best = gate_leg_best_passport(
        dest_country="TH", nationalities=["US", "UA"],
        departure_date="2026-12-01", today=TODAY,
    )
    # both visa-free → identical _passport_rank → stable tie-break on the passport string
    assert best["selected_passport"] == "UA"
    assert best["allowed"] is True


# ---------------------------------------------------------------------------
# explain_block() — early return + blocked headline (lines 1823-1858)
# ---------------------------------------------------------------------------

def test_explain_block_bookable_no_flags_early_return():
    """Bookable + no eligibility flags → early return with the all-clear headline."""
    legs = [{"dest_country": "TH", "dest_name": "Bangkok", "departure_date": "2026-12-01"}]
    v = check_eligibility(legs=legs, nationality="US", today=TODAY)
    assert v["bookable"] is True
    eb = explain_block(v)
    assert eb["blocking_legs"] == []
    assert "bookable in time" in eb["headline"]


def test_explain_block_formats_blocked_leg_reason():
    """A short-lead ET eVisa block → headline derives dest + lead-time facts from the verdict."""
    legs = [{"dest_country": "ET", "dest_name": "Addis Ababa", "departure_date": "2026-06-24"}]
    v = check_eligibility(legs=legs, nationality="US", today=TODAY)
    assert v["bookable"] is False
    eb = explain_block(v)
    head = eb["headline"]
    assert "UNBOOKABLE IN TIME" in head
    assert "Ethiopia eVisa" in head
    # lead-time facts come straight from the structured verdict
    assert "5 business days" in head
    assert "Earliest feasible departure" in head


def test_explain_block_multiple_blocking_legs_skips_allowed():
    """Mixed trip: the allowed TH leg is skipped; only the blocked ET leg shapes the headline."""
    legs = [
        {"dest_country": "ET", "dest_name": "Addis Ababa", "departure_date": "2026-06-24"},
        {"dest_country": "TH", "dest_name": "Bangkok", "departure_date": "2026-12-01"},
    ]
    v = check_eligibility(legs=legs, nationality="US", today=TODAY)
    eb = explain_block(v)
    assert "Ethiopia" in eb["headline"]
    assert "Bangkok" not in eb["headline"]
    assert any(b["dest_country"] == "ET" for b in eb["blocking_legs"])


# ---------------------------------------------------------------------------
# validate_rationale() — input + softening checks (lines 1878-1887)
# ---------------------------------------------------------------------------

def _blocked_verdict():
    legs = [{"dest_country": "ET", "dest_name": "Addis Ababa", "departure_date": "2026-06-24"}]
    return check_eligibility(legs=legs, nationality="US", today=TODAY)


def _bookable_verdict():
    legs = [{"dest_country": "TH", "dest_name": "Bangkok", "departure_date": "2026-12-01"}]
    return check_eligibility(legs=legs, nationality="US", today=TODAY)


def test_validate_rationale_empty_string_rejected():
    v = _blocked_verdict()
    assert validate_rationale("", v) is False
    assert validate_rationale("   ", v) is False


def test_validate_rationale_non_string_rejected():
    v = _blocked_verdict()
    assert validate_rationale(None, v) is False  # type: ignore[arg-type]
    assert validate_rationale(123, v) is False  # type: ignore[arg-type]


def test_validate_rationale_softening_phrases_rejected():
    """A blocked verdict must reject prose that softens the block."""
    v = _blocked_verdict()
    assert v["bookable"] is False
    for phrase in ["good to go", "no problem", "you're fine", "ready to book",
                   "can be booked now", "is bookable now"]:
        assert validate_rationale(f"The trip {phrase} for you.", v) is False
    # a faithful, factual restatement is accepted
    assert validate_rationale(
        "The Ethiopia eVisa cannot be processed in time for that departure.", v
    ) is True


def test_validate_rationale_bookable_verdict_allows_any_text():
    """A bookable verdict skips the softening check (no block to soften)."""
    v = _bookable_verdict()
    assert v["bookable"] is True
    assert validate_rationale("You're good to go — no problem at all.", v) is True


# ---------------------------------------------------------------------------
# _llm_rationale() — no key / error / validation / success (lines 1896-1924)
# ---------------------------------------------------------------------------

def test_llm_rationale_no_api_key_returns_none():
    """No DASHSCOPE_API_KEY → early return None (never calls the network)."""
    v = _blocked_verdict()
    with pytest.MonkeyPatch.context() as mp:
        mp.delenv("DASHSCOPE_API_KEY", raising=False)
        assert _llm_rationale(v) is None


def test_llm_rationale_http_error_returns_none():
    """httpx.post raising → caught → None (deterministic fallback, no crash)."""
    import httpx
    v = _blocked_verdict()

    def _boom(*a, **k):
        raise httpx.ConnectError("no network")

    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("DASHSCOPE_API_KEY", "test-key")
        mp.setattr(httpx, "post", _boom)
        assert _llm_rationale(v) is None


def test_llm_rationale_invalid_json_response_returns_none():
    """resp.json() raising → caught by the broad except → None."""
    import httpx
    v = _blocked_verdict()

    class _Resp:
        def raise_for_status(self):
            return None

        def json(self):
            raise ValueError("not json")

    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("DASHSCOPE_API_KEY", "test-key")
        mp.setattr(httpx, "post", lambda *a, **k: _Resp())
        assert _llm_rationale(v) is None


def test_llm_rationale_response_fails_validation():
    """A softening LLM draft for a BLOCKED verdict fails validate_rationale → None."""
    import httpx
    v = _blocked_verdict()
    assert v["bookable"] is False

    class _Resp:
        def raise_for_status(self):
            return None

        def json(self):
            return {"choices": [{"message": {"content": "You're good to go, no problem!"}}]}

    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("DASHSCOPE_API_KEY", "test-key")
        mp.setattr(httpx, "post", lambda *a, **k: _Resp())
        assert _llm_rationale(v) is None


def test_llm_rationale_success_valid_response():
    """A faithful LLM draft that passes validate_rationale is returned verbatim."""
    import httpx
    v = _blocked_verdict()
    factual = "The Ethiopia eVisa cannot be processed before that departure date."

    class _Resp:
        def raise_for_status(self):
            return None

        def json(self):
            return {"choices": [{"message": {"content": factual}}]}

    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("DASHSCOPE_API_KEY", "test-key")
        mp.setattr(httpx, "post", lambda *a, **k: _Resp())
        assert _llm_rationale(v) == factual


# ---------------------------------------------------------------------------
# check_eligibility flows that exercise the multi-passport / unknown-nat paths
# ---------------------------------------------------------------------------

def test_check_handler_with_empty_nationality_degrades_to_unknown():
    """Empty nationality against a specific-only dest (AO) → conservative unverified verdict
    (not a silent allow)."""
    legs = [{"dest_country": "AO", "dest_name": "Luanda", "departure_date": "2026-12-01"}]
    v = check_eligibility(legs=legs, nationality="", today=TODAY)
    pl = v["per_leg"][0]
    # unknown rule → conservative: not an affirmative allow
    assert pl["allowed"] is not True


def test_check_handler_multi_passport_flow():
    """Multi-passport check (nationalities list) routes TH through the best passport (US visa-free)."""
    legs = [{"dest_country": "TH", "dest_name": "Bangkok", "departure_date": "2026-12-01"}]
    v = check_eligibility(
        legs=legs, nationality="NG", nationalities=["NG", "US"], today=TODAY,
    )
    pl = v["per_leg"][0]
    assert pl["allowed"] is True
    assert pl["kind"] == ca.EntryKind.VISA_FREE
