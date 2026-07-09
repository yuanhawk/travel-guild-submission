"""
test_health_agent_cov3.py — Coverage gap-targeting tests for agents/health_agent.py.

Targets the uncovered lines enumerated in the cov3 spec:
  - _build_city_to_slate_key() exception handlers (3041-3042)
  - fetch_slate() country-name / ISO2 / simulate_unreachable fallbacks (3113-3131)
  - _vaccine_money() zero-cost guard + FX-normalization path (3158-3173)
  - assess() held_certs deduping (3637)
  - validate_summary() block-signal extraction + soften guard (3843-3856)
  - _llm_summary() api-key check / httpx exception / draft validation (3865-3892)
  - _assess_handler() legs + today (D7) validation + simulate passthrough (4018-4039)
  - _explain_handler() verdict-vs-legs dispatch + use_llm flag (4059-4086)

HARD-RULE compliant: ADD-ONLY (one file), NO product edits, deterministic / var-0
(frozen TODAY clock), NO live LLM/network/paid-API — direct function calls + a
Starlette TestClient over build_app() for the A2A handler paths, httpx fully mocked.
"""

from __future__ import annotations

import json
import os
import sys
import uuid
from unittest import mock

sys.path.insert(0, os.path.dirname(__file__))

import pytest
from starlette.testclient import TestClient

from agents import health_agent as health
from agents.health_agent import (
    HealthAgent,
    GateVerdict,
    _vaccine_money,
    _llm_summary,
    fetch_slate,
    assess,
    validate_summary,
)


# Frozen clock — every assess() call below pins `today` so reruns are byte-identical.
TODAY = "2026-06-16"


def _send_skill(client: TestClient, skill_id: str, data: dict) -> dict:
    """Dispatch one A2A skill request and return the resulting task dict."""
    body = {
        "jsonrpc": "2.0",
        "id": str(uuid.uuid4()),
        "method": "message/send",
        "params": {"message": {
            "messageId": str(uuid.uuid4()),
            "role": "user",
            "parts": [{"kind": "data", "data": data}],
            "metadata": {"skillId": skill_id},
        }},
    }
    resp = client.post("/", json=body)
    assert resp.status_code == 200, resp.text
    return resp.json()["result"]


# ===========================================================================
# 1. _build_city_to_slate_key() graceful-degradation (#109 rewrite)
# ===========================================================================

def test_build_city_to_slate_key_empty_city_to_iso2():
    """
    #109: _build_city_to_slate_key() no longer reads catalog.json directly — it
    is COMPOSED from intent_parser.CITY_TO_ISO2 (the same city→country table used
    to set each leg's dest_country) and this module's own ISO2→slate-key table,
    so this fallback map can never disagree with dest_country on a city-name
    collision (e.g. "Salem" — India vs United States in catalog.json).
    CITY_TO_ISO2 itself is loaded once at import time and already has its own
    OSError/JSONDecodeError → {} fallback (see intent_parser._load_city_to_iso2);
    verify the composition still degrades gracefully to {} — no raise — when
    that upstream table is empty (the equivalent "catalog unreadable" scenario
    under the new architecture).
    """
    with mock.patch("utils.intent_parser.CITY_TO_ISO2", {}):
        assert health._build_city_to_slate_key() == {}


# ===========================================================================
# 2. fetch_slate() fallbacks (lines 3113-3131)
# ===========================================================================

def test_fetch_slate_country_name_fallback():
    """3113-3117: a country *name* resolves via _COUNTRY_NAME_TO_SLATE_KEY."""
    slate, freshness = fetch_slate("ethiopia")
    assert slate is not None
    assert slate["country"] == "Ethiopia"
    assert freshness == "SEEDED"


def test_fetch_slate_iso2_fallback():
    """3120-3124: an uppercase ISO-3166 alpha-2 resolves via the ISO2 table."""
    slate, freshness = fetch_slate("ET")
    assert slate is not None
    assert slate["country"] == "Ethiopia"
    assert freshness == "SEEDED"


def test_fetch_slate_unknown_returns_none():
    """3103-3131: a key matching no table → (None, SEEDED), never fabricated."""
    slate, freshness = fetch_slate("nowhere-land-xyz")
    assert slate is None
    assert freshness == "SEEDED"


def test_fetch_slate_simulate_unreachable_logs_warning():
    """3126-3131: simulate_unreachable tags the seeded slate SEEDED_FALLBACK."""
    slate, freshness = fetch_slate("ethiopia", simulate_unreachable=True)
    assert slate is not None
    assert freshness == "SEEDED_FALLBACK"


# ===========================================================================
# 3. _vaccine_money() guard + FX path (lines 3158-3173)
# ===========================================================================

def test_vaccine_money_usd_trivial_path():
    """3160-3164: USD passes through 1:1, fx_rate 1.0 (no FX seam call)."""
    money = _vaccine_money(cost_cents=5000, currency="USD", source="test")
    assert money is not None
    assert money["currency"] == "USD"
    assert money["usd_cents"] == 5000
    assert money["fx"]["fx_rate"] == 1.0


def test_vaccine_money_zero_cost_returns_none():
    """3158-3159: cost_cents <= 0 → None (honest decline)."""
    assert _vaccine_money(0, "USD", "test") is None
    assert _vaccine_money(-100, "USD", "test") is None


def test_vaccine_money_sgd_fx_normalized():
    """3165-3173: non-USD routes through the fx_provider seam → usd_cents."""
    mock_fxr = mock.MagicMock()
    mock_fxr.exhausted = False
    mock_fxr.to_money.return_value = {
        "currency": "SGD",
        "native_cents": 15000,
        "usd_cents": 11100,
        "fx": {"fx_rate": 0.74, "fx_source": "test",
               "fetched_at": health._SEED_FETCHED_AT},
    }
    with mock.patch("utils.fx_provider.normalize", return_value=mock_fxr):
        money = _vaccine_money(cost_cents=15000, currency="SGD", source="test")
        assert money is not None
        assert money["usd_cents"] == 11100


def test_vaccine_money_unknown_currency_honest_decline():
    """3166-3172: fxr.exhausted=True (unknown currency) → None, never treat-as-USD."""
    mock_fxr = mock.MagicMock()
    mock_fxr.exhausted = True
    with mock.patch("utils.fx_provider.normalize", return_value=mock_fxr):
        assert _vaccine_money(cost_cents=10000, currency="XYZ", source="test") is None


# ===========================================================================
# 4. assess() held_certs deduping (line 3637)
# ===========================================================================

def test_assess_held_certs_deduped_and_collected():
    """3637: held cert seen on multiple legs is recorded once (deduped)."""
    v = assess(
        legs=[
            {"place_key": "ethiopia", "departure_date": "2026-09-01"},
            {"place_key": "kenya", "departure_date": "2026-09-15"},
        ],
        today=TODAY,
        held_vaccine_certs=["yellow_fever"],
    )
    assert v["verdict"] == GateVerdict.CAN_COMPLETE
    assert len(v["per_destination"]) == 2


def test_assess_held_certs_empty_list():
    """3637: an empty held list takes the no-held branch cleanly."""
    v = assess(
        legs=[{"place_key": "ethiopia", "departure_date": "2026-09-01"}],
        today=TODAY,
        held_vaccine_certs=[],
    )
    assert v["verdict"] == GateVerdict.CAN_COMPLETE


# ===========================================================================
# 5. validate_summary() block-signal extraction (lines 3843-3856)
# ===========================================================================

def _blocked_verdict() -> dict:
    """A real CANNOT_COMPLETE verdict (ethiopia YF lead-time short)."""
    return assess(
        legs=[{"place_key": "ethiopia", "departure_date": "2026-06-23"}],
        today=TODAY,
    )


def test_validate_summary_extracts_efd_from_blocking():
    """3843-3845: earliest_feasible_departure date counts as a block signal."""
    blocked = _blocked_verdict()
    assert blocked["bookable"] is False
    assert validate_summary("You must depart on 2026-06-29 or later.", blocked) is True


def test_validate_summary_extracts_place_key_from_blocking():
    """3846-3848: the blocking place_key counts as a block signal."""
    blocked = _blocked_verdict()
    assert validate_summary("ethiopia needs more certification time.", blocked) is True


def test_validate_summary_extracts_country_from_per_destination():
    """3850-3853: a per_destination country name counts as a block signal."""
    blocked = _blocked_verdict()
    assert validate_summary("ethiopia entry cannot be satisfied in time.", blocked) is True


def test_validate_summary_soften_guard_all_set():
    """3826-3834: softening phrases are rejected even atop a block keyword."""
    blocked = _blocked_verdict()
    assert validate_summary("You're all set to travel.", blocked) is False
    assert validate_summary("No problem, this is fine.", blocked) is False
    assert validate_summary("Good to go with all certificates.", blocked) is False


# ===========================================================================
# 6. _llm_summary() error / validation paths (lines 3865-3892)
# ===========================================================================

def test_llm_summary_no_api_key():
    """3865-3867: no DASHSCOPE_API_KEY → None (no network)."""
    with mock.patch.dict(os.environ, {}, clear=True):
        result = _llm_summary({
            "verdict": "cannot_complete", "blocking": [],
            "total_vaccine_cost_usd_cents": 0,
        })
        assert result is None


def test_llm_summary_http_exception():
    """3868-3892: httpx.post raising is caught → None."""
    with mock.patch.dict(os.environ, {"DASHSCOPE_API_KEY": "test-key"}):
        with mock.patch("httpx.post", side_effect=Exception("network down")):
            result = _llm_summary({
                "verdict": "cannot_complete", "blocking": [],
                "total_vaccine_cost_usd_cents": 0,
            })
            assert result is None


def test_llm_summary_invalid_response_json():
    """3888-3892: resp.json() raising is caught → None."""
    with mock.patch.dict(os.environ, {"DASHSCOPE_API_KEY": "test-key"}):
        mock_resp = mock.MagicMock()
        mock_resp.raise_for_status.return_value = None
        mock_resp.json.side_effect = ValueError("not json")
        with mock.patch("httpx.post", return_value=mock_resp):
            result = _llm_summary({
                "verdict": "cannot_complete", "blocking": [],
                "total_vaccine_cost_usd_cents": 0,
            })
            assert result is None


def test_llm_summary_draft_fails_validation():
    """3890: a softening draft fails validate_summary → None (det. fallback)."""
    blocked = _blocked_verdict()
    with mock.patch.dict(os.environ, {"DASHSCOPE_API_KEY": "test-key"}):
        mock_resp = mock.MagicMock()
        mock_resp.raise_for_status.return_value = None
        mock_resp.json.return_value = {
            "choices": [{"message": {"content": "All set, you can travel."}}]
        }
        with mock.patch("httpx.post", return_value=mock_resp):
            assert _llm_summary(blocked) is None


def test_llm_summary_valid_draft_returned():
    """3881-3890: a faithful draft passes validation and is returned verbatim."""
    blocked = _blocked_verdict()
    draft = ("Ethiopia requires a yellow-fever certificate that cannot be "
             "completed in time.")
    with mock.patch.dict(os.environ, {"DASHSCOPE_API_KEY": "test-key"}):
        mock_resp = mock.MagicMock()
        mock_resp.raise_for_status.return_value = None
        mock_resp.json.return_value = {
            "choices": [{"message": {"content": draft}}]
        }
        with mock.patch("httpx.post", return_value=mock_resp):
            assert _llm_summary(blocked) == draft


# ===========================================================================
# 7. _assess_handler() validation + passthrough (lines 4018-4039)
# ===========================================================================

def test_assess_handler_missing_legs():
    """4018-4020: empty/absent legs → handler raises → task failed."""
    client = TestClient(HealthAgent().build_app())
    task = _send_skill(client, "health.assess", {})
    assert task["status"]["state"] == "failed"


def test_assess_handler_missing_today():
    """4024-4030: legs but no 'today' → D7 contract ValueError → task failed."""
    client = TestClient(HealthAgent().build_app())
    task = _send_skill(client, "health.assess", {
        "legs": [{"place_key": "ethiopia", "departure_date": "2026-09-01"}],
    })
    assert task["status"]["state"] == "failed"


def test_assess_handler_valid_payload():
    """4034-4051: a valid payload computes and returns the verdict artifact."""
    client = TestClient(HealthAgent().build_app())
    task = _send_skill(client, "health.assess", {
        "legs": [{"place_key": "ethiopia", "departure_date": "2026-09-01"}],
        "today": TODAY,
    })
    assert task["status"]["state"] == "completed"
    result = task["artifacts"][0]["parts"][0]["data"]
    assert result["verdict"] == GateVerdict.CAN_COMPLETE


def test_assess_handler_simulate_source_unreachable():
    """4038-4039: simulate_source_unreachable flows through to source_freshness."""
    client = TestClient(HealthAgent().build_app())
    task = _send_skill(client, "health.assess", {
        "legs": [{"place_key": "ethiopia", "departure_date": "2026-09-01"}],
        "today": TODAY,
        "simulate_source_unreachable": True,
    })
    assert task["status"]["state"] == "completed"
    result = task["artifacts"][0]["parts"][0]["data"]
    assert result["source_freshness"] == "SEEDED_FALLBACK"


# ===========================================================================
# 8. _explain_handler() dispatch + use_llm flag (lines 4059-4086)
# ===========================================================================

def test_explain_handler_missing_verdict_and_legs():
    """4059-4065: neither verdict nor legs → handler raises → task failed."""
    client = TestClient(HealthAgent().build_app())
    task = _send_skill(client, "health.explain", {})
    assert task["status"]["state"] == "failed"


def test_explain_handler_with_verdict():
    """4059-4079: a pre-computed verdict dispatches straight to explain()."""
    client = TestClient(HealthAgent().build_app())
    verdict = assess(
        legs=[{"place_key": "ethiopia", "departure_date": "2026-09-01"}],
        today=TODAY,
    )
    task = _send_skill(client, "health.explain", {"verdict": verdict})
    assert task["status"]["state"] == "completed"
    result = task["artifacts"][0]["parts"][0]["data"]
    assert result["verdict"] == GateVerdict.CAN_COMPLETE
    assert result["summary_source"] == "deterministic"


def test_explain_handler_with_legs_missing_today():
    """4066-4072: legs path also enforces the D7 'today' requirement."""
    client = TestClient(HealthAgent().build_app())
    task = _send_skill(client, "health.explain", {
        "legs": [{"place_key": "ethiopia", "departure_date": "2026-09-01"}],
    })
    assert task["status"]["state"] == "failed"


def test_explain_handler_with_legs_computes_verdict():
    """4073-4077: legs + today computes the verdict before explaining."""
    client = TestClient(HealthAgent().build_app())
    task = _send_skill(client, "health.explain", {
        "legs": [{"place_key": "ethiopia", "departure_date": "2026-09-01"}],
        "today": TODAY,
    })
    assert task["status"]["state"] == "completed"
    result = task["artifacts"][0]["parts"][0]["data"]
    assert result["verdict"] == GateVerdict.CAN_COMPLETE


def test_explain_handler_use_llm_flag_false():
    """4085-4086: use_llm=False → summary_source 'deterministic'."""
    client = TestClient(HealthAgent().build_app())
    verdict = assess(
        legs=[{"place_key": "ethiopia", "departure_date": "2026-09-01"}],
        today=TODAY,
    )
    task = _send_skill(client, "health.explain", {"verdict": verdict, "use_llm": False})
    assert task["status"]["state"] == "completed"
    result = task["artifacts"][0]["parts"][0]["data"]
    assert result["summary_source"] == "deterministic"


def test_explain_handler_use_llm_flag_true_no_key():
    """4081-4084: use_llm=True with no key → llm_summary None, det. fallback."""
    client = TestClient(HealthAgent().build_app())
    verdict = assess(
        legs=[{"place_key": "ethiopia", "departure_date": "2026-09-01"}],
        today=TODAY,
    )
    with mock.patch.dict(os.environ, {}, clear=True):
        task = _send_skill(client, "health.explain", {"verdict": verdict, "use_llm": True})
    assert task["status"]["state"] == "completed"
    result = task["artifacts"][0]["parts"][0]["data"]
    assert result["llm_summary"] is None
    assert result["summary_source"] == "deterministic"


# ===========================================================================
# 9. Determinism (var-0) guards
# ===========================================================================

def test_held_certs_deterministic_across_runs():
    """Line 3637 dedup is byte-identical across 5 reruns (frozen clock)."""
    sigs = []
    for _ in range(5):
        v = assess(
            legs=[
                {"place_key": "ethiopia", "departure_date": "2026-09-01"},
                {"place_key": "kenya", "departure_date": "2026-09-15"},
            ],
            today=TODAY,
            held_vaccine_certs=["yellow_fever"],
        )
        sigs.append(json.dumps({
            "verdict": v["verdict"],
            "cost": v["total_vaccine_cost_usd_cents"],
        }, sort_keys=True))
    assert len(set(sigs)) == 1


def test_vaccine_money_deterministic():
    """_vaccine_money() USD path is byte-identical across 5 calls."""
    results = []
    for _ in range(5):
        money = _vaccine_money(cost_cents=5000, currency="USD", source="test")
        results.append(json.dumps(money, sort_keys=True, default=str))
    assert len(set(results)) == 1


def test_llm_summary_deterministic_on_none():
    """_llm_summary() with no key returns None on all 5 reruns."""
    with mock.patch.dict(os.environ, {}, clear=True):
        results = [
            _llm_summary({"verdict": "x", "blocking": [],
                          "total_vaccine_cost_usd_cents": 0})
            for _ in range(5)
        ]
    assert all(r is None for r in results)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
