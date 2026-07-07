"""
test_planner_agent_cov3.py — Planner A2A agent coverage extension (lines 239-240, 361-409, 569, 628-668).

Targets:
  - Lines 239-240: _build_card URL construction
  - Lines 361-409: _decompose_handler payload extraction, validation, risk_directives assembly
  - Line 569: risk_buffer_min > 0 advisory generation
  - Lines 628-668: _extract_payload helper (all branches), main() entry point

All tests are var-0: deterministic, pure input/output, no live network/LLM, mocked where needed.
"""

from __future__ import annotations

import asyncio
import json
import os
from unittest.mock import patch, MagicMock

import pytest

from agents import planner_agent
from agents.planner_agent import PlannerAgent


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_agent(host: str = "127.0.0.1", port: int = 9102) -> PlannerAgent:
    """Construct an agent without binding ASGI server."""
    return PlannerAgent(host=host, port=port)


def _msg(payload: dict | str) -> dict:
    """Build a message with data part."""
    return {"parts": [{"kind": "data", "data": payload}]}


async def _decompose(payload: dict) -> dict:
    """Invoke _decompose_handler and return skeleton dict."""
    agent = _make_agent()
    artifact = await agent._decompose_handler(_msg(payload), task={})
    return artifact["parts"][0]["data"]


# ---------------------------------------------------------------------------
# Lines 239-240: _build_card URL construction
# ---------------------------------------------------------------------------

def test_build_card_url_construction_line_239_240():
    """Line 239: url = f"http://{self._host}:{self._port}" is used in line 240 dict."""
    host = "192.168.1.100"
    port = 9999
    agent = _make_agent(host=host, port=port)
    card = agent._build_card()
    
    expected_url = f"http://{host}:{port}"
    assert card["url"] == expected_url
    assert card["name"] == "planner-agent"
    assert isinstance(card["skills"], list)
    assert len(card["skills"]) > 0


def test_build_card_url_with_different_ports():
    """Verify URL construction works with different ports (var-0)."""
    for port in [8000, 9102, 9999]:
        agent = _make_agent(host="0.0.0.0", port=port)
        card = agent._build_card()
        assert card["url"] == f"http://0.0.0.0:{port}"


# ---------------------------------------------------------------------------
# Lines 361-364: ValueError when no payload (empty parts)
# ---------------------------------------------------------------------------

def test_handler_missing_payload_raises_error():
    """Line 361-364: Raise ValueError when message has no data part."""
    agent = _make_agent()
    message = {"parts": []}  # No data/text parts
    
    with pytest.raises(ValueError, match="requires a data part"):
        asyncio.run(agent._decompose_handler(message, task={}))


def test_handler_missing_payload_text_part_invalid():
    """Line 628-641: Text part with invalid JSON also returns None."""
    agent = _make_agent()
    message = {"parts": [{"kind": "text", "text": "not json"}]}
    
    with pytest.raises(ValueError, match="requires a data part"):
        asyncio.run(agent._decompose_handler(message, task={}))


def test_handler_parts_missing_entirely():
    """Message without 'parts' key at all."""
    agent = _make_agent()
    message = {}  # No parts key
    
    with pytest.raises(ValueError, match="requires a data part"):
        asyncio.run(agent._decompose_handler(message, task={}))


# ---------------------------------------------------------------------------
# Lines 366-369: Payload field extraction
# ---------------------------------------------------------------------------

def test_handler_extracts_user_id_line_366():
    """Line 366: user_id = payload.get("user_id", "")"""
    res = asyncio.run(_decompose({
        "user_id": "user-xyz",
        "total_budget_cents": 50000,
        "legs": [{"city": "paris", "checkin": "2026-10-01", "checkout": "2026-10-03"}],
    }))
    assert res["user_id"] == "user-xyz"


def test_handler_extracts_total_budget_cents_line_367():
    """Line 367: total_budget_cents = int(payload.get("total_budget_cents", 0))"""
    res = asyncio.run(_decompose({
        "user_id": "u1",
        "total_budget_cents": 123456,
        "legs": [{"city": "paris", "checkin": "2026-10-01", "checkout": "2026-10-03"}],
    }))
    assert res["total_budget_cents"] == 123456


def test_handler_extracts_legs_line_368():
    """Line 368: legs_input = payload.get("legs", [])"""
    legs_data = [
        {"city": "paris", "checkin": "2026-10-01", "checkout": "2026-10-03"},
        {"city": "london", "checkin": "2026-10-03", "checkout": "2026-10-05"},
    ]
    res = asyncio.run(_decompose({
        "user_id": "u1",
        "total_budget_cents": 50000,
        "legs": legs_data,
    }))
    assert len(res["legs"]) == 2
    assert res["legs"][0]["city"] == "paris"
    assert res["legs"][1]["city"] == "london"


def test_handler_extracts_preferences_line_369():
    """Line 369: preferences = payload.get("preferences") or {}"""
    res = asyncio.run(_decompose({
        "user_id": "u1",
        "total_budget_cents": 50000,
        "legs": [{"city": "paris", "checkin": "2026-10-01", "checkout": "2026-10-03"}],
        "preferences": {"style": "luxury", "climate": "warm"},
    }))
    # preferences are not directly in skeleton, just need to ensure no error
    assert res["user_id"] == "u1"


def test_handler_preferences_defaults_to_empty():
    """Line 369: preferences defaults to {} when missing."""
    res = asyncio.run(_decompose({
        "user_id": "u1",
        "total_budget_cents": 50000,
        "legs": [{"city": "paris", "checkin": "2026-10-01", "checkout": "2026-10-03"}],
        # no preferences key
    }))
    # Should not raise, just use empty dict
    assert res["user_id"] == "u1"


def test_handler_extracts_per_leg_candidates_line_370():
    """Line 370: per_leg_candidates = payload.get("per_leg_candidates")"""
    # Test with no candidates to avoid DP allocator being triggered
    payload = {
        "user_id": "u1",
        "total_budget_cents": 50000,
        "legs": [{"city": "paris", "checkin": "2026-10-01", "checkout": "2026-10-03"}],
        # per_leg_candidates missing - should not trigger DP
    }
    # Should not raise with per_leg_candidates missing
    res = asyncio.run(_decompose(payload))
    assert res["user_id"] == "u1"


# ---------------------------------------------------------------------------
# Lines 378-401: Risk directives extraction and risk_flagged assembly
# ---------------------------------------------------------------------------

def test_handler_risk_avoid_window_line_379():
    """Line 379: risk_avoid_window = bool(risk_directives.get("avoid_window"))"""
    res = asyncio.run(_decompose({
        "user_id": "u1",
        "total_budget_cents": 50000,
        "legs": [{"city": "paris", "checkin": "2026-10-01", "checkout": "2026-10-03"}],
        "risk_directives": {"avoid_window": True},
    }))
    assert res["risk_flagged"] is True
    assert any("avoid-window" in adv.lower() for adv in res["risk_advisories"])


def test_handler_risk_buffer_connection_min_line_380():
    """Line 380: risk_buffer_min = int(risk_directives.get("buffer_connection_min", 0) or 0)"""
    res = asyncio.run(_decompose({
        "user_id": "u1",
        "total_budget_cents": 50000,
        "legs": [{"city": "paris", "checkin": "2026-10-01", "checkout": "2026-10-03"}],
        "risk_directives": {"buffer_connection_min": 60},
    }))
    assert res["risk_flagged"] is True
    assert res["legs"][0]["buffer_connection_min"] == 60


def test_handler_risk_flagged_legs_line_384_386():
    """Lines 384-386: risk_flagged_legs set construction from risk_directives."""
    res = asyncio.run(_decompose({
        "user_id": "u1",
        "total_budget_cents": 50000,
        "legs": [
            {"city": "paris", "checkin": "2026-10-01", "checkout": "2026-10-03"},
            {"city": "london", "checkin": "2026-10-03", "checkout": "2026-10-05"},
        ],
        "risk_directives": {"flagged_legs": ["leg-0", "leg-1"], "reason_codes": ["FLOOD"]},
    }))
    assert res["risk_flagged"] is True
    assert res["legs"][0]["risk_flags"] != []
    assert res["legs"][1]["risk_flags"] != []


def test_handler_risk_reason_codes_line_387_389():
    """Lines 387-389: risk_reason_codes extracted as list."""
    res = asyncio.run(_decompose({
        "user_id": "u1",
        "total_budget_cents": 50000,
        "legs": [{"city": "paris", "checkin": "2026-10-01", "checkout": "2026-10-03"}],
        "risk_directives": {"reason_codes": ["CYCLONE", "FLOOD"]},
    }))
    assert "CYCLONE" in res["risk_reason_codes"]
    assert "FLOOD" in res["risk_reason_codes"]


def test_handler_risk_prefer_flexible_line_390_392():
    """Lines 390-392: risk_prefer_flexible_cancellation extracted."""
    res = asyncio.run(_decompose({
        "user_id": "u1",
        "total_budget_cents": 50000,
        "legs": [{"city": "paris", "checkin": "2026-10-01", "checkout": "2026-10-03"}],
        "risk_directives": {"avoid_window": True, "prefer_flexible_cancellation": True},
    }))
    # Should produce an advisory about flexible cancellation
    assert any("flexible" in adv.lower() for adv in res["risk_advisories"])


def test_handler_risk_flagged_computed_line_396_401():
    """Lines 396-401: risk_flagged = bool(avoid_window OR buffer_min OR legs OR codes)"""
    # Test each condition that makes risk_flagged True
    
    # Condition 1: avoid_window
    res1 = asyncio.run(_decompose({
        "user_id": "u1",
        "total_budget_cents": 50000,
        "legs": [{"city": "paris", "checkin": "2026-10-01", "checkout": "2026-10-03"}],
        "risk_directives": {"avoid_window": True},
    }))
    assert res1["risk_flagged"] is True
    
    # Condition 2: buffer_min > 0
    res2 = asyncio.run(_decompose({
        "user_id": "u1",
        "total_budget_cents": 50000,
        "legs": [{"city": "paris", "checkin": "2026-10-01", "checkout": "2026-10-03"}],
        "risk_directives": {"buffer_connection_min": 30},
    }))
    assert res2["risk_flagged"] is True
    
    # Condition 3: flagged_legs present
    res3 = asyncio.run(_decompose({
        "user_id": "u1",
        "total_budget_cents": 50000,
        "legs": [{"city": "paris", "checkin": "2026-10-01", "checkout": "2026-10-03"}],
        "risk_directives": {"flagged_legs": ["leg-0"]},
    }))
    assert res3["risk_flagged"] is True
    
    # Condition 4: reason_codes present
    res4 = asyncio.run(_decompose({
        "user_id": "u1",
        "total_budget_cents": 50000,
        "legs": [{"city": "paris", "checkin": "2026-10-01", "checkout": "2026-10-03"}],
        "risk_directives": {"reason_codes": ["CYCLONE"]},
    }))
    assert res4["risk_flagged"] is True


# ---------------------------------------------------------------------------
# Lines 404-409: Validation checks
# ---------------------------------------------------------------------------

def test_handler_validates_user_id_required_line_404_405():
    """Lines 404-405: if not user_id: raise ValueError"""
    with pytest.raises(ValueError, match="user_id is required"):
        asyncio.run(_decompose({
            "user_id": "",
            "total_budget_cents": 50000,
            "legs": [{"city": "paris", "checkin": "2026-10-01", "checkout": "2026-10-03"}],
        }))


def test_handler_validates_total_budget_positive_line_406_407():
    """Lines 406-407: if total_budget_cents <= 0: raise ValueError"""
    with pytest.raises(ValueError, match="total_budget_cents must be > 0"):
        asyncio.run(_decompose({
            "user_id": "u1",
            "total_budget_cents": 0,
            "legs": [{"city": "paris", "checkin": "2026-10-01", "checkout": "2026-10-03"}],
        }))
    
    with pytest.raises(ValueError, match="total_budget_cents must be > 0"):
        asyncio.run(_decompose({
            "user_id": "u1",
            "total_budget_cents": -100,
            "legs": [{"city": "paris", "checkin": "2026-10-01", "checkout": "2026-10-03"}],
        }))


def test_handler_validates_legs_nonempty_line_408_409():
    """Lines 408-409: if not legs_input: raise ValueError"""
    with pytest.raises(ValueError, match="legs must be a non-empty list"):
        asyncio.run(_decompose({
            "user_id": "u1",
            "total_budget_cents": 50000,
            "legs": [],
        }))


# ---------------------------------------------------------------------------
# Line 569: risk_buffer_min > 0 advisory (multiple conditions)
# ---------------------------------------------------------------------------

def test_handler_risk_buffer_advisory_line_569_572():
    """Line 569: if risk_buffer_min > 0: advisory about connection buffer."""
    res = asyncio.run(_decompose({
        "user_id": "u1",
        "total_budget_cents": 50000,
        "legs": [{"city": "paris", "checkin": "2026-10-01", "checkout": "2026-10-03"}],
        "risk_directives": {"buffer_connection_min": 45},
    }))
    # Should have an advisory mentioning the buffer
    assert any("45" in adv for adv in res["risk_advisories"])
    assert any("buffer" in adv.lower() for adv in res["risk_advisories"])


def test_handler_risk_buffer_zero_no_advisory():
    """When buffer_min is 0 or missing, no buffer advisory."""
    res = asyncio.run(_decompose({
        "user_id": "u1",
        "total_budget_cents": 50000,
        "legs": [{"city": "paris", "checkin": "2026-10-01", "checkout": "2026-10-03"}],
        "risk_directives": {"buffer_connection_min": 0},
    }))
    # No buffer advisory
    assert not any("buffer" in adv.lower() for adv in res["risk_advisories"])


def test_handler_risk_buffer_large_value():
    """Line 569 with large buffer value."""
    res = asyncio.run(_decompose({
        "user_id": "u1",
        "total_budget_cents": 50000,
        "legs": [{"city": "paris", "checkin": "2026-10-01", "checkout": "2026-10-03"}],
        "risk_directives": {"buffer_connection_min": 120},
    }))
    assert any("120" in adv for adv in res["risk_advisories"])


# ---------------------------------------------------------------------------
# Lines 628-641: _extract_payload helper (text parts, data parts, JSON parsing)
# ---------------------------------------------------------------------------

def test_extract_payload_data_part_dict_line_624_627():
    """Lines 624-627: data part with dict data returns dict directly."""
    message = {"parts": [{"kind": "data", "data": {"key": "value"}}]}
    agent = _make_agent()
    result = agent._extract_payload(message)
    assert result == {"key": "value"}


def test_extract_payload_data_part_json_string_line_628_632():
    """Lines 628-632: data part with JSON string is parsed."""
    message = {"parts": [{"kind": "data", "data": '{"user_id": "u1"}'}]}
    agent = _make_agent()
    result = agent._extract_payload(message)
    assert result == {"user_id": "u1"}


def test_extract_payload_data_part_invalid_json_continues():
    """Lines 628-632: invalid JSON in data part is caught, continues."""
    message = {
        "parts": [
            {"kind": "data", "data": "not valid json"},
            {"kind": "data", "data": {"fallback": "dict"}},
        ]
    }
    agent = _make_agent()
    result = agent._extract_payload(message)
    # Should skip invalid JSON and use the fallback
    assert result == {"fallback": "dict"}


def test_extract_payload_text_part_json_dict_line_633_640():
    """Lines 633-640: text part with valid JSON dict returns dict."""
    message = {"parts": [{"kind": "text", "text": '{"user_id": "u2"}'}]}
    agent = _make_agent()
    result = agent._extract_payload(message)
    assert result == {"user_id": "u2"}


def test_extract_payload_text_part_json_array_skipped():
    """Lines 633-640: text part with JSON array (not dict) is skipped."""
    message = {
        "parts": [
            {"kind": "text", "text": "[1, 2, 3]"},
            {"kind": "text", "text": '{"valid": "dict"}'},
        ]
    }
    agent = _make_agent()
    result = agent._extract_payload(message)
    assert result == {"valid": "dict"}


def test_extract_payload_text_part_invalid_json_continues():
    """Lines 633-640: invalid JSON in text part continues to next part."""
    message = {
        "parts": [
            {"kind": "text", "text": "not json"},
            {"kind": "data", "data": {"result": "found"}},
        ]
    }
    agent = _make_agent()
    result = agent._extract_payload(message)
    assert result == {"result": "found"}


def test_extract_payload_no_valid_parts_returns_none():
    """Line 641: returns None when no valid data/text parts found."""
    message = {"parts": [{"kind": "unknown", "data": {}}]}
    agent = _make_agent()
    result = agent._extract_payload(message)
    assert result is None


def test_extract_payload_empty_parts_returns_none():
    """Line 641: empty parts list returns None."""
    message = {"parts": []}
    agent = _make_agent()
    result = agent._extract_payload(message)
    assert result is None


def test_extract_payload_text_part_empty_string():
    """Line 634: text part with empty string (no text key or empty)."""
    message = {"parts": [{"kind": "text"}]}  # no text key
    agent = _make_agent()
    result = agent._extract_payload(message)
    assert result is None


def test_extract_payload_first_valid_wins():
    """Multiple parts: first valid (dict or parsed JSON) is returned."""
    message = {
        "parts": [
            {"kind": "data", "data": {"first": True}},
            {"kind": "data", "data": {"second": True}},
        ]
    }
    agent = _make_agent()
    result = agent._extract_payload(message)
    assert result["first"] is True


# ---------------------------------------------------------------------------
# Lines 648-668: main() entry point
# ---------------------------------------------------------------------------

def test_main_entry_point_creates_agent_and_app_line_656_657():
    """Lines 656-657: main() creates agent and builds app."""
    with patch("agents.planner_agent.uvicorn.run") as mock_run:
        with patch.dict(os.environ, {"PORT": "9102", "HOST": "127.0.0.1"}):
            planner_agent.main()
            # Verify uvicorn.run was called
            assert mock_run.called


def test_main_entry_point_respects_port_env_line_653():
    """Line 653: port = int(os.environ.get("PORT", 9102))"""
    with patch("agents.planner_agent.uvicorn.run") as mock_run:
        with patch.dict(os.environ, {"PORT": "8888", "HOST": "0.0.0.0"}):
            planner_agent.main()
            call_args = mock_run.call_args
            assert call_args[1]["port"] == 8888


def test_main_entry_point_respects_host_env_line_654():
    """Security FIX 6: host = os.environ.get("AGENT_BIND_HOST", "127.0.0.1")."""
    with patch("agents.planner_agent.uvicorn.run") as mock_run:
        with patch.dict(os.environ, {"AGENT_BIND_HOST": "192.168.1.1", "PORT": "9102"}):
            planner_agent.main()
            call_args = mock_run.call_args
            assert call_args[1]["host"] == "192.168.1.1"


def test_main_entry_point_default_port():
    """Line 653: default port is 9102 when env var missing."""
    with patch("agents.planner_agent.uvicorn.run") as mock_run:
        with patch.dict(os.environ, {}, clear=False):
            # Ensure PORT is not set
            os.environ.pop("PORT", None)
            planner_agent.main()
            call_args = mock_run.call_args
            assert call_args[1]["port"] == 9102


def test_main_entry_point_logging_configured_line_649_652():
    """Lines 649-652: logging is configured before creating agent."""
    with patch("agents.planner_agent.logging.basicConfig") as mock_basic:
        with patch("agents.planner_agent.uvicorn.run"):
            planner_agent.main()
            assert mock_basic.called


def test_main_entry_point_logs_startup_messages_line_659_662():
    """Lines 659-662: logs startup info including DP allocator flag."""
    with patch("agents.planner_agent.logger") as mock_logger:
        with patch("agents.planner_agent.uvicorn.run"):
            with patch.dict(os.environ, {"PORT": "9102", "HOST": "0.0.0.0"}):
                planner_agent.main()
                # Should log startup, agent card URL, RPC endpoint, DP flag
                assert mock_logger.info.called


# ---------------------------------------------------------------------------
# Var-0: Deterministic re-runs
# ---------------------------------------------------------------------------

def test_handler_var0_risk_advisories_sorted():
    """Risk advisories are always sorted (var-0 determinism)."""
    payload = {
        "user_id": "u1",
        "total_budget_cents": 50000,
        "legs": [{"city": "paris", "checkin": "2026-10-01", "checkout": "2026-10-03"}],
        "risk_directives": {
            "avoid_window": True,
            "buffer_connection_min": 30,
            "reason_codes": ["Z_CODE", "A_CODE"],
        },
    }
    res1 = asyncio.run(_decompose(payload))
    res2 = asyncio.run(_decompose(payload))
    
    # Both runs must be identical
    assert res1 == res2
    # Advisories must be sorted
    assert res1["risk_advisories"] == sorted(res1["risk_advisories"])


def test_handler_var0_idempotent_runs():
    """Multiple decompose calls with same input always produce same output."""
    payload = {
        "user_id": "test-user",
        "total_budget_cents": 100000,
        "legs": [
            {"city": "paris", "checkin": "2026-10-01", "checkout": "2026-10-04"},
            {"city": "london", "checkin": "2026-10-04", "checkout": "2026-10-07"},
        ],
        "risk_directives": {"flagged_legs": ["leg-1"], "reason_codes": ["FLOOD"]},
    }
    results = [asyncio.run(_decompose(payload)) for _ in range(3)]
    assert results[0] == results[1] == results[2]


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
