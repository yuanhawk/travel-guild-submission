"""
test_coverage_gap_cov2.py — #45 coverage-gap analyzer, targeted uncovered-branch suite.

Deterministic / var-0-safe (no LLM, no network, no clock, no random). ADD-ONLY:
this file imports only the product module + the contracts/insurance vocab and
exercises the uncovered line ranges of society/utils/coverage_gap.py:

  171-172   _coerce_int_or_none exception handler (TypeError/ValueError → None)
  193       validate_user_policy non-dict raw → PolicyValidationError
  207-208   declarations non-list → warning + coerce to []
  210-212   declaration item non-dict → skip + warning
  214-219   invalid peril_class → drop + warning
  221-226   invalid stance → coerce to 'unknown' + warning
  228-232   sub_limited without valid limit → warning
  234       condition_text normalization (str() / None passthrough)
  245-246   global_exclusions non-list → warning + coerce to []
  248-252   invalid exclusion peril → drop + warning
  253       exclusion dedup first-seen
  362-364   COVERED dedup
  368-372   EXCLUDED absolute vs non-absolute note
  373-379   UNDETERMINED conservative path
  381-393   SUB_LIMITED (null limit / equal / below / above)
  399-401   CONDITIONAL with/without condition_text
  407-408   gap_perils first-seen order
  470-471   parse_user_policy_text empty/non-string early return
  488/491-503  parse_user_policy_text HTTP / non-dict / JSON / validation / success

The LLM path tests force a non-empty DASHSCOPE_API_KEY (monkeypatched) and stub
httpx.post via monkeypatch — NO real network is ever touched.
"""

from __future__ import annotations

import json

import pytest

from utils import coverage_gap
from utils.coverage_gap import (
    analyze_coverage_gap,
    validate_user_policy,
    parse_user_policy_text,
    PolicyValidationError,
    _coerce_int_or_none,
)
from agents.insurance_agent import CoverageStatus
from core.contracts import Peril


# Canonical peril values used throughout (all in Peril; see contracts).
MEDICAL = Peril.MEDICAL.value
WAR = Peril.WAR.value
TERRORISM = Peril.TERRORISM.value
CIVIL_UNREST = Peril.CIVIL_UNREST.value
BAGGAGE = Peril.BAGGAGE_LOSS.value
DELAY = Peril.TRAVEL_DELAY.value
CANCEL = Peril.TRIP_CANCELLATION.value


def _gap_for(report, peril):
    """First gap dict in a report for the given peril (or None)."""
    for g in report["gaps"]:
        if g["peril_class"] == peril:
            return g
    return None


# ===========================================================================
# Lines 171-172: _coerce_int_or_none exception handler
# ===========================================================================

def test_coerce_int_or_none_with_non_numeric_string():
    """ValueError path: a non-numeric string → None."""
    assert _coerce_int_or_none("not_a_number") is None


def test_coerce_int_or_none_with_object():
    """TypeError path: object without __int__ → None."""
    class NoInt:
        pass
    assert _coerce_int_or_none(NoInt()) is None


def test_coerce_int_or_none_with_list():
    """TypeError path: list → None."""
    assert _coerce_int_or_none([1, 2, 3]) is None


def test_coerce_int_or_none_passthrough_and_none():
    """Sanity: valid int/float coerces; None stays None (not the exc path)."""
    assert _coerce_int_or_none(500) == 500
    assert _coerce_int_or_none("42") == 42
    assert _coerce_int_or_none(None) is None


# ===========================================================================
# Line 193: validate_user_policy non-dict raw → PolicyValidationError
# ===========================================================================

def test_validate_user_policy_non_dict_raw_input():
    with pytest.raises(PolicyValidationError) as exc:
        validate_user_policy("not a dict")
    assert "JSON object" in str(exc.value)


def test_validate_user_policy_list_input():
    with pytest.raises(PolicyValidationError):
        validate_user_policy(["a", "b"])


def test_validate_user_policy_none_input():
    with pytest.raises(PolicyValidationError):
        validate_user_policy(None)


def test_validate_user_policy_non_usd_currency_rejected():
    """Adjacent invariant: non-USD currency rejects (sanity, deterministic)."""
    with pytest.raises(PolicyValidationError) as exc:
        validate_user_policy({"currency": "EUR"})
    assert "USD" in str(exc.value)


# ===========================================================================
# Lines 207-208: declarations non-list → warning + coerce to []
# ===========================================================================

def test_validate_user_policy_declarations_not_list():
    policy, warnings = validate_user_policy({"declarations": "covered"})
    assert policy["declarations"] == []
    assert any("declarations is not a list" in w for w in warnings)


def test_validate_user_policy_declarations_dict_instead_of_list():
    policy, warnings = validate_user_policy(
        {"declarations": {"peril_class": MEDICAL, "stance": "covered"}}
    )
    assert policy["declarations"] == []
    assert any("declarations is not a list" in w for w in warnings)


# ===========================================================================
# Lines 210-212: declaration item non-dict → skip + warning
# ===========================================================================

def test_validate_user_policy_declaration_item_not_dict():
    policy, warnings = validate_user_policy({
        "declarations": [
            "junk_string",
            {"peril_class": MEDICAL, "stance": "covered"},
        ]
    })
    perils = [d["peril_class"] for d in policy["declarations"]]
    assert perils == [MEDICAL]  # non-dict skipped, valid kept
    assert any("declaration is not an object" in w for w in warnings)


# ===========================================================================
# Lines 214-219: invalid peril_class → drop + warning
# ===========================================================================

def test_validate_user_policy_invalid_peril_class_dropped():
    policy, warnings = validate_user_policy({
        "declarations": [
            {"peril_class": "not_a_real_peril", "stance": "covered"},
            {"peril_class": MEDICAL, "stance": "covered"},
        ]
    })
    perils = [d["peril_class"] for d in policy["declarations"]]
    assert perils == [MEDICAL]
    assert any("not in canonical Peril set" in w for w in warnings)


# ===========================================================================
# Lines 221-226: invalid stance → coerce to 'unknown' + warning
# ===========================================================================

def test_validate_user_policy_invalid_stance_coerced_unknown():
    policy, warnings = validate_user_policy({
        "declarations": [{"peril_class": MEDICAL, "stance": "maybe_covered"}]
    })
    assert policy["declarations"][0]["stance"] == "unknown"
    assert any("closed stance set" in w for w in warnings)


# ===========================================================================
# Lines 228-232: sub_limited without valid limit → warning
# ===========================================================================

def test_validate_user_policy_sub_limited_without_limit_warning():
    """sub_limited with an UNCOERCIBLE limit → limit_cents None + warning."""
    policy, warnings = validate_user_policy({
        "declarations": [
            {"peril_class": MEDICAL, "stance": "sub_limited", "limit_cents": "bad"}
        ]
    })
    assert policy["declarations"][0]["limit_cents"] is None
    assert any("sub_limited but has no valid" in w for w in warnings)


def test_validate_user_policy_sub_limited_with_none_limit():
    """sub_limited with MISSING limit key → limit_cents None + warning."""
    policy, warnings = validate_user_policy({
        "declarations": [{"peril_class": MEDICAL, "stance": "sub_limited"}]
    })
    assert policy["declarations"][0]["limit_cents"] is None
    assert any("sub_limited but has no valid" in w for w in warnings)


def test_validate_user_policy_sub_limited_with_valid_limit_no_warning():
    """Sanity: a valid limit produces NO sub-limit warning (not line 228-232)."""
    policy, warnings = validate_user_policy({
        "declarations": [
            {"peril_class": MEDICAL, "stance": "sub_limited", "limit_cents": 5000}
        ]
    })
    assert policy["declarations"][0]["limit_cents"] == 5000
    assert not any("sub_limited but has no valid" in w for w in warnings)


# ===========================================================================
# Line 234: condition_text normalization
# ===========================================================================

def test_validate_user_policy_condition_text_normalization():
    policy, _ = validate_user_policy({
        "declarations": [
            {"peril_class": MEDICAL, "stance": "conditional", "condition_text": 12345}
        ]
    })
    ct = policy["declarations"][0]["condition_text"]
    assert ct == "12345" and isinstance(ct, str)


def test_validate_user_policy_condition_text_none_stays_none():
    policy, _ = validate_user_policy({
        "declarations": [
            {"peril_class": MEDICAL, "stance": "conditional"}
        ]
    })
    assert policy["declarations"][0]["condition_text"] is None


# ===========================================================================
# Lines 245-246: global_exclusions non-list → warning + coerce to []
# ===========================================================================

def test_validate_user_policy_global_exclusions_not_list():
    policy, warnings = validate_user_policy({"global_exclusions": WAR})
    assert policy["global_exclusions"] == []
    assert any("global_exclusions is not a list" in w for w in warnings)


def test_validate_user_policy_global_exclusions_int():
    policy, warnings = validate_user_policy({"global_exclusions": 7})
    assert policy["global_exclusions"] == []
    assert any("global_exclusions is not a list" in w for w in warnings)


# ===========================================================================
# Lines 248-252: invalid exclusion peril → drop + warning
# ===========================================================================

def test_validate_user_policy_global_exclusion_invalid_peril():
    policy, warnings = validate_user_policy({
        "global_exclusions": ["bogus_peril", WAR]
    })
    assert policy["global_exclusions"] == [WAR]
    assert any("not in canonical Peril set — dropped" in w for w in warnings)


# ===========================================================================
# Line 253: exclusion dedup first-seen
# ===========================================================================

def test_validate_user_policy_global_exclusion_dedup_first_seen():
    policy, _ = validate_user_policy({
        "global_exclusions": [WAR, TERRORISM, WAR, TERRORISM, CIVIL_UNREST]
    })
    assert policy["global_exclusions"] == [WAR, TERRORISM, CIVIL_UNREST]


# ===========================================================================
# Lines 368-372: EXCLUDED absolute vs non-absolute note
# ===========================================================================

def test_analyze_coverage_gap_excluded_non_absolute_peril():
    """EXCLUDED gap for a non-absolute peril (medical) has NO absolute note."""
    report = analyze_coverage_gap(
        peril_set=[MEDICAL],
        user_policy={
            "declarations": [{"peril_class": MEDICAL, "stance": "excluded"}]
        },
    )
    gap = _gap_for(report, MEDICAL)
    assert gap is not None and gap["status"] == CoverageStatus.EXCLUDED
    assert "note" not in gap


def test_analyze_coverage_gap_excluded_absolute_peril_has_note():
    """EXCLUDED gap for war attaches the absolute-exclusion note (368-372)."""
    report = analyze_coverage_gap(
        peril_set=[WAR],
        user_policy={"global_exclusions": [WAR]},
    )
    gap = _gap_for(report, WAR)
    assert gap is not None and gap["status"] == CoverageStatus.EXCLUDED
    assert "note" in gap and "absolute" in gap["note"]


# ===========================================================================
# Lines 373-379: UNDETERMINED conservative path
# ===========================================================================

def test_analyze_coverage_gap_undetermined_conservative_path():
    """A peril the user never declared → UNDETERMINED gap, conservative copy."""
    report = analyze_coverage_gap(
        peril_set=[MEDICAL],
        user_policy={"declarations": []},
    )
    gap = _gap_for(report, MEDICAL)
    assert gap is not None and gap["status"] == CoverageStatus.UNDETERMINED
    assert "confirm with your insurer" in gap["why_not_covered"]
    assert "never assumed covered" in gap["risk_left"]


# ===========================================================================
# Lines 381-393: SUB_LIMITED branches
# ===========================================================================

def test_analyze_coverage_gap_sublimit_with_null_limit():
    """SUB_LIMITED with null limit → generic verify message, no shortfall."""
    report = analyze_coverage_gap(
        peril_set=[MEDICAL],
        user_policy={
            "declarations": [{"peril_class": MEDICAL, "stance": "sub_limited"}]
        },
        insured_trip_cost_cents=100000,
    )
    gap = _gap_for(report, MEDICAL)
    assert gap["status"] == CoverageStatus.SUB_LIMITED
    assert "shortfall_cents" not in gap
    assert "limit may be adequate" in gap["why_not_covered"]


def test_analyze_coverage_gap_sublimit_insured_equal_limit():
    """insured == limit → no shortfall (line 382 boundary)."""
    report = analyze_coverage_gap(
        peril_set=[MEDICAL],
        user_policy={
            "declarations": [
                {"peril_class": MEDICAL, "stance": "sub_limited", "limit_cents": 5000}
            ]
        },
        insured_trip_cost_cents=5000,
    )
    gap = _gap_for(report, MEDICAL)
    assert gap["status"] == CoverageStatus.SUB_LIMITED
    assert "shortfall_cents" not in gap


def test_analyze_coverage_gap_sublimit_insured_below_limit():
    """insured < limit → no shortfall."""
    report = analyze_coverage_gap(
        peril_set=[MEDICAL],
        user_policy={
            "declarations": [
                {"peril_class": MEDICAL, "stance": "sub_limited", "limit_cents": 9000}
            ]
        },
        insured_trip_cost_cents=4000,
    )
    gap = _gap_for(report, MEDICAL)
    assert "shortfall_cents" not in gap


def test_analyze_coverage_gap_sublimit_insured_above_limit_has_shortfall():
    """insured > limit → shortfall = insured - limit (382-388)."""
    report = analyze_coverage_gap(
        peril_set=[MEDICAL],
        user_policy={
            "declarations": [
                {"peril_class": MEDICAL, "stance": "sub_limited", "limit_cents": 5000}
            ]
        },
        insured_trip_cost_cents=8000,
    )
    gap = _gap_for(report, MEDICAL)
    assert gap["shortfall_cents"] == 3000


# ===========================================================================
# Lines 399-401: CONDITIONAL with / without condition_text
# ===========================================================================

def test_analyze_coverage_gap_conditional_with_condition_text():
    report = analyze_coverage_gap(
        peril_set=[MEDICAL],
        user_policy={
            "declarations": [
                {"peril_class": MEDICAL, "stance": "conditional",
                 "condition_text": "only if pre-authorized"}
            ]
        },
    )
    gap = _gap_for(report, MEDICAL)
    assert gap["status"] == CoverageStatus.CONDITIONAL
    assert gap["condition_text"] == "only if pre-authorized"


def test_analyze_coverage_gap_conditional_without_condition_text():
    report = analyze_coverage_gap(
        peril_set=[MEDICAL],
        user_policy={
            "declarations": [{"peril_class": MEDICAL, "stance": "conditional"}]
        },
    )
    gap = _gap_for(report, MEDICAL)
    assert gap["status"] == CoverageStatus.CONDITIONAL
    assert "condition_text" not in gap


# ===========================================================================
# Lines 362-364: COVERED dedup
# ===========================================================================

def test_analyze_coverage_gap_covered_dedup():
    """Duplicate covered perils in the trip → single entry in covered[]."""
    report = analyze_coverage_gap(
        peril_set=[MEDICAL, MEDICAL, MEDICAL],
        user_policy={
            "declarations": [{"peril_class": MEDICAL, "stance": "covered"}]
        },
    )
    assert report["covered"] == [MEDICAL]
    assert report["gaps"] == []


# ===========================================================================
# Lines 407-408: gap_perils first-seen order (drives recommendations)
# ===========================================================================

def test_analyze_coverage_gap_gap_perils_first_seen_order():
    """Gap perils recorded in first-seen order; duplicates collapse."""
    report = analyze_coverage_gap(
        peril_set=[BAGGAGE, DELAY, BAGGAGE, CANCEL, DELAY],
        user_policy={"declarations": []},  # all UNDETERMINED → all gaps
    )
    gap_perils = [g["peril_class"] for g in report["gaps"]]
    assert gap_perils == [BAGGAGE, DELAY, CANCEL]


# ===========================================================================
# Combined: all five statuses in one report (multi-status categorization)
# ===========================================================================

def test_analyze_coverage_gap_all_status_types():
    report = analyze_coverage_gap(
        peril_set=[MEDICAL, WAR, BAGGAGE, DELAY, CANCEL],
        user_policy={
            "declarations": [
                {"peril_class": MEDICAL, "stance": "covered"},
                {"peril_class": BAGGAGE, "stance": "sub_limited", "limit_cents": 100},
                {"peril_class": DELAY, "stance": "conditional",
                 "condition_text": "delay > 6h"},
                # CANCEL undeclared → UNDETERMINED
            ],
            "global_exclusions": [WAR],
        },
        insured_trip_cost_cents=500,
    )
    assert report["covered"] == [MEDICAL]
    statuses = {g["peril_class"]: g["status"] for g in report["gaps"]}
    assert statuses[WAR] == CoverageStatus.EXCLUDED
    assert statuses[BAGGAGE] == CoverageStatus.SUB_LIMITED
    assert statuses[DELAY] == CoverageStatus.CONDITIONAL
    assert statuses[CANCEL] == CoverageStatus.UNDETERMINED
    # WAR exclusion is absolute → has the note.
    assert "note" in _gap_for(report, WAR)
    # BAGGAGE sub-limit (100) below insured (500) → shortfall.
    assert _gap_for(report, BAGGAGE)["shortfall_cents"] == 400


# ===========================================================================
# Lines 470-471: parse_user_policy_text empty / non-string early return
# ===========================================================================
# These force a non-empty key so we get PAST line 468 (the key gate) and reach
# the empty/non-string guard at 470-471 deterministically.

def test_parse_user_policy_text_empty_text(monkeypatch):
    monkeypatch.setattr(coverage_gap, "DASHSCOPE_API_KEY", "fake-key")
    assert parse_user_policy_text("") is None
    assert parse_user_policy_text("   ") is None


def test_parse_user_policy_text_non_string(monkeypatch):
    monkeypatch.setattr(coverage_gap, "DASHSCOPE_API_KEY", "fake-key")
    assert parse_user_policy_text(12345) is None  # type: ignore[arg-type]
    assert parse_user_policy_text(None) is None  # type: ignore[arg-type]


def test_parse_user_policy_text_key_unset_returns_none(monkeypatch):
    """Line 468 gate: no key → None regardless of input."""
    monkeypatch.setattr(coverage_gap, "DASHSCOPE_API_KEY", "")
    assert parse_user_policy_text("I have medical cover") is None


# ===========================================================================
# parse_user_policy_text network branches — httpx.post stubbed via monkeypatch.
# NO real network. We build a fake response object exposing exactly the methods
# the product code calls: raise_for_status() and json().
# ===========================================================================

class _FakeResp:
    def __init__(self, *, content, raise_exc=None, json_exc=None):
        self._content = content
        self._raise_exc = raise_exc
        self._json_exc = json_exc

    def raise_for_status(self):
        if self._raise_exc is not None:
            raise self._raise_exc

    def json(self):
        if self._json_exc is not None:
            raise self._json_exc
        return {"choices": [{"message": {"content": self._content}}]}


def _install_fake_httpx(monkeypatch, *, post_returns=None, post_raises=None):
    """Monkeypatch a fake httpx module so the local `import httpx` resolves to it."""
    import sys
    import types

    fake = types.ModuleType("httpx")

    def _post(*args, **kwargs):
        if post_raises is not None:
            raise post_raises
        return post_returns

    fake.post = _post
    monkeypatch.setitem(sys.modules, "httpx", fake)
    monkeypatch.setattr(coverage_gap, "DASHSCOPE_API_KEY", "fake-key")


def test_parse_user_policy_text_valid_with_warnings(monkeypatch):
    """Lines 494-497: valid policy WITH warnings → returns validated policy."""
    # condition_text given as int → triggers a (silent here) normalization; use an
    # invalid stance to force a warning so the warnings branch (495-496) runs.
    content = json.dumps({
        "currency": "USD",
        "declarations": [{"peril_class": MEDICAL, "stance": "bogus_stance"}],
    })
    _install_fake_httpx(monkeypatch, post_returns=_FakeResp(content=content))
    policy = parse_user_policy_text("free text describing my policy")
    assert policy is not None
    assert policy["currency"] == "USD"
    # bogus stance coerced to unknown by the laundering validator.
    assert policy["declarations"][0]["stance"] == "unknown"


def test_parse_user_policy_text_valid_clean(monkeypatch):
    """Happy path with no warnings → returns validated policy (497)."""
    content = json.dumps({
        "currency": "USD",
        "declarations": [{"peril_class": MEDICAL, "stance": "covered"}],
    })
    _install_fake_httpx(monkeypatch, post_returns=_FakeResp(content=content))
    policy = parse_user_policy_text("my policy text")
    assert policy is not None
    assert policy["declarations"][0]["peril_class"] == MEDICAL


def test_parse_user_policy_text_validation_error(monkeypatch):
    """Lines 498-500: non-USD currency → PolicyValidationError catch → None."""
    content = json.dumps({"currency": "EUR", "declarations": []})
    _install_fake_httpx(monkeypatch, post_returns=_FakeResp(content=content))
    assert parse_user_policy_text("euro policy") is None


def test_parse_user_policy_text_response_not_dict(monkeypatch):
    """Lines 491-492: parsed JSON is a list (not dict) → None."""
    content = json.dumps(["not", "a", "dict"])
    _install_fake_httpx(monkeypatch, post_returns=_FakeResp(content=content))
    assert parse_user_policy_text("array policy") is None


def test_parse_user_policy_text_json_parse_error(monkeypatch):
    """Lines 501-503: invalid JSON in content → json.loads raises → None."""
    content = "{ this is not valid json"
    _install_fake_httpx(monkeypatch, post_returns=_FakeResp(content=content))
    assert parse_user_policy_text("bad json policy") is None


def test_parse_user_policy_text_http_error(monkeypatch):
    """Lines 501-503: httpx.post itself raises → generic exception catch → None."""
    _install_fake_httpx(monkeypatch, post_raises=RuntimeError("connection refused"))
    assert parse_user_policy_text("policy text") is None


def test_parse_user_policy_text_raise_for_status_error(monkeypatch):
    """Line 488 + 501-503: raise_for_status raises → caught → None."""
    resp = _FakeResp(content="{}", raise_exc=RuntimeError("HTTP 500"))
    _install_fake_httpx(monkeypatch, post_returns=resp)
    assert parse_user_policy_text("policy text") is None


def test_parse_user_policy_text_json_method_error(monkeypatch):
    """resp.json() raising → caught by generic handler → None."""
    resp = _FakeResp(content="{}", json_exc=ValueError("no json"))
    _install_fake_httpx(monkeypatch, post_returns=resp)
    assert parse_user_policy_text("policy text") is None


# ===========================================================================
# Combined malformed-policy validation: many warning paths in one call.
# ===========================================================================

def test_validate_user_policy_all_error_paths_combined():
    policy, warnings = validate_user_policy({
        "currency": "USD",
        "declarations": [
            "not_a_dict",
            {"peril_class": "fake_peril", "stance": "covered"},
            {"peril_class": MEDICAL, "stance": "weird"},
            {"peril_class": BAGGAGE, "stance": "sub_limited", "limit_cents": "NaN"},
            {"peril_class": CANCEL, "stance": "covered"},
        ],
        "global_exclusions": [WAR, "junk", WAR],
    })
    # Surviving declarations: MEDICAL(unknown), BAGGAGE(sub_limited,None), CANCEL.
    perils = [d["peril_class"] for d in policy["declarations"]]
    assert perils == [MEDICAL, BAGGAGE, CANCEL]
    assert policy["declarations"][0]["stance"] == "unknown"
    assert policy["declarations"][1]["limit_cents"] is None
    assert policy["global_exclusions"] == [WAR]
    # At least one warning per malformed shape.
    joined = " || ".join(warnings)
    assert "declaration is not an object" in joined
    assert "not in canonical Peril set" in joined
    assert "closed stance set" in joined
    assert "sub_limited but has no valid" in joined
    assert "dropped" in joined  # global_exclusion junk dropped
