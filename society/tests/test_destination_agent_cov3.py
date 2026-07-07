"""
test_destination_agent_cov3.py — coverage-3 sweep for society/agents/destination_agent.py.

Targets the uncovered lines flagged by the coverage planner:
  - _month_in_window (224-229): non-wrapping + wrapping windows.
  - seasonal_advisory (232-280): fire/no-fire, country-None guard, date parse
    failure, short dates, no parseable months, case-insensitive matching,
    provenance fields.
  - city_wide_areas (594-601): single-area, Bali, unknown.
  - broaden_areas (563-591): single-area passthrough, non-Bali current, Bali
    broadening + merge.
  - assess type-guarding (665-668): non-string city raises, non-string vibe
    coerced to None.
  - assess country derivation (678-682): explicit precedence + auto-derive.
  - assess LLM fallback (761): deterministic fallback path (no DASHSCOPE key).
  - _assess_handler (893-927): missing/empty/non-string city, vibe coercion.
  - _extract_payload (934-954): data dict / data JSON str / text JSON / malformed.

VAR-0 DISCIPLINE: every test imports the module and calls its pure functions
directly with crafted inputs.  NO network, NO live LLM, NO paid API.  The LLM
path is naturally short-circuited because DASHSCOPE_API_KEY is unset in the
test env (_llm_call raises RuntimeError → deterministic fallback), so assess()
is fully deterministic here.  This file is ADD-ONLY and never edits product code.
"""

from __future__ import annotations

import asyncio

import pytest

from agents import destination_agent as da
from agents.destination_agent import (
    DestinationAgent,
    BALI_DEFAULT_RANKED,
    BALI_VIBE_BROAD,
    SINGLE_AREA_CITIES,
    assess,
    broaden_areas,
    city_wide_areas,
    seasonal_advisory,
    _month_in_window,
)


# ---------------------------------------------------------------------------
# Guard: assert the LLM is genuinely off in this env so assess() is var-0.
# ---------------------------------------------------------------------------

def test_dashscope_key_unset_so_llm_path_is_deterministic():
    """Pre-condition for the assess() fallback tests: no live LLM key present."""
    assert not da.DASHSCOPE_API_KEY, (
        "DASHSCOPE_API_KEY must be unset for var-0 determinism in this suite"
    )


# ---------------------------------------------------------------------------
# _month_in_window (lines 224-229)
# ---------------------------------------------------------------------------

def test_month_in_window_non_wrapping():
    # Non-wrapping window [3, 8]: start <= end branch (line 226-227).
    assert _month_in_window(4, 3, 8) is True       # interior
    assert _month_in_window(3, 3, 8) is True        # start boundary
    assert _month_in_window(8, 3, 8) is True         # end boundary
    assert _month_in_window(2, 3, 8) is False         # below
    assert _month_in_window(9, 3, 8) is False          # above


def test_month_in_window_wrapping():
    # Wrapping window [12, 2]: start > end branch (line 228-229).
    assert _month_in_window(12, 12, 2) is True    # start boundary
    assert _month_in_window(1, 12, 2) is True      # wraps past year end
    assert _month_in_window(2, 12, 2) is True       # end boundary
    assert _month_in_window(3, 12, 2) is False       # just outside high side
    assert _month_in_window(11, 12, 2) is False       # just outside low side


def test_month_in_window_single_month_window():
    # Degenerate non-wrapping window where start == end.
    assert _month_in_window(6, 6, 6) is True
    assert _month_in_window(7, 6, 6) is False


# ---------------------------------------------------------------------------
# seasonal_advisory (lines 232-280)
# ---------------------------------------------------------------------------

def test_seasonal_advisory_fires_on_checkin():
    adv = seasonal_advisory("perth", "2024-12-15", "2024-12-20", country="australia")
    assert adv is not None
    assert adv["type"] == "seasonal_advisory"
    assert adv["city"] == "perth"
    assert adv["flag"] == "bushfire_season"
    assert adv["severity"] == "high"
    assert adv["provenance"] == "seeded:user-2022-wa-trip"
    assert adv["window"] == "month 12–2"
    assert "detail" in adv


def test_seasonal_advisory_fires_on_checkout_only():
    # checkin out of window (Sept), checkout in window (Jan) → still fires.
    adv = seasonal_advisory("albany", "2024-09-30", "2025-01-05", country="australia")
    assert adv is not None
    assert adv["flag"] == "bushfire_season"


def test_seasonal_advisory_country_none_returns_none():
    # Line 252-254 guard: no country → cannot attribute advisory.
    assert seasonal_advisory("perth", "2024-12-15", "2024-12-20", country=None) is None


def test_seasonal_advisory_empty_country_returns_none():
    # Whitespace-only country is falsy after strip → guarded out.
    assert seasonal_advisory("perth", "2024-12-15", "2024-12-20", country="   ") is None


def test_seasonal_advisory_case_insensitive_city_and_country():
    adv = seasonal_advisory("  PERTH ", "2024-12-15", None, country="Australia")
    assert adv is not None
    assert adv["city"] == "perth"


def test_seasonal_advisory_no_window_for_city():
    # Country present but no seeded advisory for this (city, country) pair (255-257).
    assert seasonal_advisory("bali", "2024-12-15", "2024-12-20", country="indonesia") is None


def test_seasonal_advisory_date_parse_failure_returns_none():
    # Line 261-265: 'invalid-date'[5:7] == 'id' → int() raises ValueError → continue,
    # leaving months empty → line 266-267 returns None.
    assert seasonal_advisory("perth", "invalid-date", None, country="australia") is None


def test_seasonal_advisory_short_date_skipped():
    # len('2024')<7 → skipped (line 261 guard); both short → no months → None.
    assert seasonal_advisory("perth", "2024", "12", country="australia") is None


def test_seasonal_advisory_out_of_window_returns_none():
    # Valid date but month (June=06) outside the Dec–Feb window → None (line 280).
    assert seasonal_advisory("perth", "2024-06-15", "2024-06-20", country="australia") is None


def test_seasonal_advisory_none_dates_returns_none():
    # Both dates None → not a str → months empty → None.
    assert seasonal_advisory("perth", None, None, country="australia") is None


# ---------------------------------------------------------------------------
# city_wide_areas (lines 594-601)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("city", ["bangkok", "singapore", "kuala lumpur", "perth", "esperance"])
def test_city_wide_areas_single_area(city):
    # Line 597-598: single-area cities return [themselves].
    assert city in SINGLE_AREA_CITIES
    assert city_wide_areas(city) == [SINGLE_AREA_CITIES[city]]


def test_city_wide_areas_single_area_normalizes_case():
    assert city_wide_areas("  Bangkok ") == ["bangkok"]


def test_city_wide_areas_bali():
    # Line 599-600: Bali returns a copy of BALI_DEFAULT_RANKED.
    out = city_wide_areas("bali")
    assert out == list(BALI_DEFAULT_RANKED)
    assert out is not BALI_DEFAULT_RANKED  # list() makes a fresh copy


def test_city_wide_areas_unknown():
    # Line 601: unknown city → empty list.
    assert city_wide_areas("atlantis") == []


# ---------------------------------------------------------------------------
# broaden_areas (lines 563-591)
# ---------------------------------------------------------------------------

def test_broaden_areas_single_area_passthrough():
    # Line 575-576: single-area city ignores current_areas, returns [city area].
    assert broaden_areas("bangkok", "city", ["whatever"]) == ["bangkok"]


def test_broaden_areas_non_bali_returns_current():
    # Line 577-578: unknown (non-Bali, non-single-area) → echoes current_areas.
    cur = ["somewhere", "elsewhere"]
    out = broaden_areas("atlantis", "beach", cur)
    assert out == cur
    assert out is not cur  # list(...) returns a copy


def test_broaden_areas_bali_known_vibe_merges():
    # Line 580-591: Bali beach vibe broadens via BALI_VIBE_BROAD['beach'],
    # current (valid) areas kept first, then the broad set appended dedup'd.
    out = broaden_areas("bali", "beach", ["seminyak"])
    assert out[0] == "seminyak"  # current area ranked first
    for a in BALI_VIBE_BROAD["beach"]:
        assert a in out
    # no duplicates
    assert len(out) == len(set(out))


def test_broaden_areas_bali_unknown_vibe_uses_default():
    # Line 582-583: vibe with no BALI_VIBE_BROAD entry → BALI_DEFAULT_RANKED.
    out = broaden_areas("bali", "zzz-unknown-vibe", [])
    for a in BALI_DEFAULT_RANKED:
        assert a in out


def test_broaden_areas_bali_drops_invalid_current_areas():
    # Line 586: current areas not in BALI_AREAS are filtered out of the cur prefix.
    out = broaden_areas("bali", "beach", ["not-an-area", "ubud"])
    assert "not-an-area" not in out
    assert "ubud" in out


def test_broaden_areas_bali_none_vibe():
    # vibe=None → (vibe or "") == "" → no BALI_VIBE_BROAD entry → default set.
    out = broaden_areas("bali", None, [])
    assert out  # non-empty
    for a in BALI_DEFAULT_RANKED:
        assert a in out


# ---------------------------------------------------------------------------
# assess type-guarding (lines 665-668)
# ---------------------------------------------------------------------------

def test_assess_non_string_city_raises():
    # Line 665-666: non-string city raises ValueError.
    with pytest.raises(ValueError) as ei:
        assess(123, "beach")
    assert "city must be a str" in str(ei.value)


def test_assess_non_string_vibe_coerced_to_none():
    # Line 667-668: non-string vibe is coerced to None (no raise). Bali + None vibe
    # → deterministic broad fallback (LLM off here).
    result = assess("bali", 456)
    assert result["type"] == "area_assessment"
    assert result["vibe"] is None
    assert result["areas"]  # non-empty
    assert result["source"] == "fallback"


# ---------------------------------------------------------------------------
# assess country derivation (lines 678-682)
# ---------------------------------------------------------------------------

def test_assess_explicit_country_fires_advisory():
    # Line 679: explicit country takes precedence; Perth + Dec → advisory fires.
    result = assess("perth", "city", checkin="2024-12-15", checkout="2024-12-20",
                    country="australia")
    assert result["advisory"] is not None
    assert result["advisory"]["flag"] == "bushfire_season"


def test_assess_auto_derives_country_for_wa_town():
    # Line 680-681: country omitted → derived from _CITY_COUNTRY_DEFAULTS → advisory fires.
    result = assess("perth", "city", checkin="2024-12-15", checkout="2024-12-20",
                    country=None)
    assert result["advisory"] is not None
    assert result["advisory"]["flag"] == "bushfire_season"


def test_assess_whitespace_country_falls_back_to_default():
    # Line 680: whitespace country is falsy after strip → uses _CITY_COUNTRY_DEFAULTS.
    result = assess("perth", "city", checkin="2024-12-15", checkout="2024-12-20",
                    country="   ")
    assert result["advisory"] is not None


def test_assess_unknown_city_no_advisory():
    # Non-seeded city → _CITY_COUNTRY_DEFAULTS.get returns None → advisory None.
    result = assess("bali", "beach", checkin="2024-12-15", checkout="2024-12-20")
    assert result["advisory"] is None


# ---------------------------------------------------------------------------
# assess LLM fallback (line 761) — strict_set None branch, deterministic.
# ---------------------------------------------------------------------------

def test_assess_bali_known_vibe_fallback():
    # strict_set exists (Bali beach). With LLM off, clamped stays empty and we
    # land on the deterministic fallback (line 786). Areas ⊆ strict vibe set.
    result = assess("bali", "beach")
    assert result["areas"]
    assert result["source"] == "fallback"
    # Invariant: returned areas are real catalog areas within the beach fallback.
    for a in result["areas"]:
        assert a in da.BALI_VIBE_FALLBACK["beach"]


def test_assess_bali_unknown_vibe_uses_full_closed_set_branch():
    # Unknown vibe → _vibe_strict_set returns None → assess takes the
    # `else: clamped = city_clamped` branch shape (line 761 region) and the
    # full-closed-set `allowed = sorted(closed)` path. LLM off → broad fallback.
    result = assess("bali", "totally-unknown-vibe")
    assert result["areas"]
    # broad default fallback (no bespoke vibe mapping)
    assert set(result["areas"]) == set(BALI_DEFAULT_RANKED)


def test_assess_single_area_city_short_circuit():
    result = assess("singapore", "city")
    assert result["source"] == "city_single"
    assert result["areas"] == ["singapore"]


def test_assess_unknown_city_empty_areas():
    result = assess("atlantis", "beach")
    assert result["areas"] == []
    assert result["source"] == "fallback"


# ---------------------------------------------------------------------------
# _assess_handler (lines 893-927) — async handler over a crafted A2A message.
# ---------------------------------------------------------------------------

def _agent() -> DestinationAgent:
    return DestinationAgent()


def _run(coro):
    return asyncio.run(coro)


def _data_message(payload) -> dict:
    return {"parts": [{"kind": "data", "data": payload}]}


def test_handler_missing_payload_raises():
    # No valid part → _extract_payload None → line 896-899 raises.
    agent = _agent()
    msg = {"parts": []}
    with pytest.raises(ValueError) as ei:
        _run(agent._assess_handler(msg, {}))
    assert "requires a data part" in str(ei.value)


def test_handler_missing_city_raises():
    agent = _agent()
    msg = _data_message({"vibe": "beach"})
    with pytest.raises(ValueError) as ei:
        _run(agent._assess_handler(msg, {}))
    assert "city is required" in str(ei.value)


def test_handler_empty_city_raises():
    agent = _agent()
    msg = _data_message({"city": "   ", "vibe": "beach"})
    with pytest.raises(ValueError) as ei:
        _run(agent._assess_handler(msg, {}))
    assert "city is required" in str(ei.value)


def test_handler_non_string_city_raises():
    agent = _agent()
    msg = _data_message({"city": 123, "vibe": "beach"})
    with pytest.raises(ValueError) as ei:
        _run(agent._assess_handler(msg, {}))
    assert "city is required" in str(ei.value)


def test_handler_non_string_vibe_coerced_and_succeeds():
    # Line 910-911: vibe=456 → coerced to None; handler succeeds (var-0 fallback).
    agent = _agent()
    msg = _data_message({"city": "bali", "vibe": 456})
    artifact = _run(agent._assess_handler(msg, {}))
    # _new_artifact returns a dict with parts; the data part carries the result.
    data_parts = [p for p in artifact.get("parts", []) if p.get("kind") == "data"]
    assert data_parts
    result = data_parts[0]["data"]
    assert result["vibe"] is None
    assert result["areas"]


def test_handler_empty_string_vibe_normalized_to_none():
    # Line 911: empty-string vibe → None.
    agent = _agent()
    msg = _data_message({"city": "bali", "vibe": ""})
    artifact = _run(agent._assess_handler(msg, {}))
    result = [p for p in artifact["parts"] if p.get("kind") == "data"][0]["data"]
    assert result["vibe"] is None


def test_handler_happy_path_single_area():
    agent = _agent()
    msg = _data_message({"city": "bangkok", "vibe": "city"})
    artifact = _run(agent._assess_handler(msg, {}))
    result = [p for p in artifact["parts"] if p.get("kind") == "data"][0]["data"]
    assert result["source"] == "city_single"
    assert result["areas"] == ["bangkok"]


# ---------------------------------------------------------------------------
# _extract_payload (lines 934-954)
# ---------------------------------------------------------------------------

def test_extract_payload_from_data_dict():
    # Line 939-940: data part is already a dict → returned as-is.
    msg = {"parts": [{"kind": "data", "data": {"city": "bali", "vibe": "beach"}}]}
    out = DestinationAgent._extract_payload(msg)
    assert out == {"city": "bali", "vibe": "beach"}


def test_extract_payload_from_data_json_string():
    # Line 941-945: data part is a JSON string → parsed.
    msg = {"parts": [{"kind": "data", "data": '{"city": "bali", "vibe": "surf"}'}]}
    out = DestinationAgent._extract_payload(msg)
    assert out == {"city": "bali", "vibe": "surf"}


def test_extract_payload_from_data_malformed_json_string_falls_through():
    # Line 944-945: malformed JSON in a data str → except pass → keep scanning;
    # no further valid part → None.
    msg = {"parts": [{"kind": "data", "data": "{not json"}]}
    assert DestinationAgent._extract_payload(msg) is None


def test_extract_payload_from_text_json():
    # Line 946-951: text part holds JSON dict → parsed.
    msg = {"parts": [{"kind": "text", "text": '{"city": "singapore"}'}]}
    out = DestinationAgent._extract_payload(msg)
    assert out == {"city": "singapore"}


def test_extract_payload_text_json_non_dict_ignored():
    # Line 950: parsed JSON is a list, not a dict → not returned → None.
    msg = {"parts": [{"kind": "text", "text": '["bali", "beach"]'}]}
    assert DestinationAgent._extract_payload(msg) is None


def test_extract_payload_malformed_text_json_returns_none():
    # Line 952-953: malformed JSON text → except pass → None.
    msg = {"parts": [{"kind": "text", "text": "not-json-at-all"}]}
    assert DestinationAgent._extract_payload(msg) is None


def test_extract_payload_prioritizes_data_over_text():
    # Line 936 iteration order: a valid data part before a text part wins.
    msg = {
        "parts": [
            {"kind": "data", "data": {"city": "from-data"}},
            {"kind": "text", "text": '{"city": "from-text"}'},
        ]
    }
    out = DestinationAgent._extract_payload(msg)
    assert out == {"city": "from-data"}


def test_extract_payload_skips_data_str_then_uses_text():
    # First data part is malformed (skipped), later text part is valid → text wins.
    msg = {
        "parts": [
            {"kind": "data", "data": "{broken"},
            {"kind": "text", "text": '{"city": "rescued"}'},
        ]
    }
    out = DestinationAgent._extract_payload(msg)
    assert out == {"city": "rescued"}


def test_extract_payload_no_parts_key():
    # message.get('parts', []) → [] → loop body never runs → None.
    assert DestinationAgent._extract_payload({}) is None


def test_extract_payload_empty_parts():
    assert DestinationAgent._extract_payload({"parts": []}) is None
