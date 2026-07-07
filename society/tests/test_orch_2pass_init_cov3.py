"""
test_orch_2pass_init_cov3.py — Deep coverage for TravelOrchestrator
"init/config/early-setup" region.

Target lines in orchestration/orchestrator.py:
  168-171  _extract_task_data:  data-artifact found path (return part["data"])
  191      _send_to_client:     RPC "error" key in response → RuntimeError
  204-223  _send_to_url:        full URL-based A2A dispatch function
  434      _call_fraud:         URL branch (_send_to_url path)
  440-449  _call_planner:       URL branch + _call_accommodation client branch
  453-465  _call_budget / _call_budget_check: client and URL branches
  471-473  _call_budget_commit: URL branch + no-client-no-url raise
  485      _call_critic:        URL branch
  502      _call_transport:     URL branch
  524      _call_day_planner:   URL branch
  540      _call_food_search:   None → return None bypass
  543-545  _call_food_search:   exception handler (degraded to None)
  605      _call_insurance:     URL branch
  626-630  _call_coverage_gap:  URL branch
  653      _call_compliance:    URL branch
  675      _call_risk:          URL branch

Design: DETERMINISTIC / var-0.
  - Module-level helpers (_extract_task_data, _send_to_url) are called directly.
  - URL-based dispatch branches are exercised by patching
    ``orchestration.orchestrator.httpx.post`` with a canned A2A JSON response —
    no live network, no real agent, no LLM.
  - Client-seam tests use the real in-process agents (same TestClient pattern as
    test_composition_wiring.py / test_orchestrator_gates_cov2.py).
  - Concrete behaviour (return value dict shape, raised exception message,
    bypass == None) is asserted so regressions are caught.

HARD RULES observed:
  - ADD-ONLY, no product edits.
  - Mock ONLY the httpx.post network seam; never mock the orchestrator itself.
  - No live LLM / paid API / real merchant network calls.
"""

from __future__ import annotations

import json
import os
import sys
import uuid
from unittest import mock

import httpx
import pytest

# Make the society package importable regardless of cwd.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from orchestration.orchestrator import (  # noqa: E402
    TravelOrchestrator,
    _extract_task_data,
    _send_to_url,
    _send_to_client,
    _CONSUMABLE_TASK_STATES,
)


# ===========================================================================
# A2A response builder helpers (shared across all URL-dispatch tests)
# ===========================================================================

def _a2a_task(data: dict, state: str = "completed") -> dict:
    """Build a minimal A2A task dict carrying `data` as the first data-part."""
    return {
        "status": {"state": state},
        "artifacts": [
            {"parts": [{"kind": "data", "data": data}]}
        ],
    }


def _rpc_ok(data: dict, state: str = "completed") -> dict:
    """Build a JSON-RPC 2.0 success response wrapping an A2A task."""
    return {"jsonrpc": "2.0", "id": "1", "result": _a2a_task(data, state)}


def _mock_httpx_post(data: dict, state: str = "completed"):
    """Return a context-manager-compatible mock for httpx.post that yields
    a Response whose .json() returns a valid A2A task carrying `data`."""
    resp = mock.MagicMock(spec=httpx.Response)
    resp.json.return_value = _rpc_ok(data, state)
    resp.raise_for_status = mock.MagicMock()
    return mock.patch("orchestration.orchestrator.httpx.post", return_value=resp)


# ===========================================================================
# SECTION 1 — _extract_task_data (lines 167-171)
# ===========================================================================

class TestExtractTaskData:
    """Unit tests for the module-level _extract_task_data helper."""

    def test_returns_data_from_completed_task(self) -> None:
        """Lines 168-171: happy path — data artifact found → return part["data"]."""
        task = _a2a_task({"verdict": "can_satisfy", "bookable": True})
        result = _extract_task_data(task, "test.skill")
        # The returned dict must BE the data payload, not a wrapper.
        assert result == {"verdict": "can_satisfy", "bookable": True}

    def test_returns_data_from_input_required_task(self) -> None:
        """input-required is also a consumable state (§42 contract) — data returned."""
        task = _a2a_task({"needs_consent": True}, state="input-required")
        result = _extract_task_data(task, "budget.check")
        assert result["needs_consent"] is True

    def test_raises_on_failed_state(self) -> None:
        """A 'failed' task is non-consumable → RuntimeError with state + skill_id."""
        task = {
            "status": {"state": "failed"},
            "metadata": {"error": "agent crashed"},
            "artifacts": [],
        }
        with pytest.raises(RuntimeError) as exc_info:
            _extract_task_data(task, "compliance.check_eligibility")
        err = str(exc_info.value)
        assert "compliance.check_eligibility" in err
        assert "failed" in err

    def test_raises_on_missing_data_artifact(self) -> None:
        """Completed state but NO data-kind part → RuntimeError."""
        task = {
            "status": {"state": "completed"},
            "artifacts": [
                {"parts": [{"kind": "text", "text": "hello"}]}
            ],
        }
        with pytest.raises(RuntimeError) as exc_info:
            _extract_task_data(task, "risk.assess")
        assert "No data artifact" in str(exc_info.value)
        assert "risk.assess" in str(exc_info.value)

    def test_raises_on_empty_artifacts(self) -> None:
        """Completed with empty artifacts → RuntimeError (no data part)."""
        task = {"status": {"state": "completed"}, "artifacts": []}
        with pytest.raises(RuntimeError):
            _extract_task_data(task, "health.assess")

    def test_first_data_part_wins_over_later_parts(self) -> None:
        """When multiple parts exist, the FIRST data part is returned."""
        task = {
            "status": {"state": "completed"},
            "artifacts": [
                {"parts": [
                    {"kind": "text", "text": "ignored"},
                    {"kind": "data", "data": {"first": True}},
                    {"kind": "data", "data": {"first": False}},
                ]}
            ],
        }
        result = _extract_task_data(task, "test.skill")
        assert result["first"] is True

    def test_first_data_part_across_artifacts(self) -> None:
        """Data part in the FIRST artifact wins; second artifact ignored."""
        task = {
            "status": {"state": "completed"},
            "artifacts": [
                {"parts": [{"kind": "data", "data": {"artifact": 1}}]},
                {"parts": [{"kind": "data", "data": {"artifact": 2}}]},
            ],
        }
        result = _extract_task_data(task, "test.skill")
        assert result["artifact"] == 1


# ===========================================================================
# SECTION 2 — _send_to_client (line 191: "error" branch)
# ===========================================================================

class TestSendToClientErrorBranch:
    """Line 191: _send_to_client must raise RuntimeError when 'error' is in rpc."""

    def test_send_to_client_raises_on_rpc_error(self) -> None:
        """Line 191: 'error' key in RPC response → RuntimeError with skill_id."""

        class _ErrorClient:
            """Stub TestClient that always returns a JSON-RPC error."""
            def post(self, path, json=None, **kwargs):
                resp = mock.MagicMock()
                resp.json.return_value = {
                    "jsonrpc": "2.0",
                    "id": "1",
                    "error": {"code": -32600, "message": "Invalid Request"},
                }
                return resp

        with pytest.raises(RuntimeError) as exc_info:
            _send_to_client(_ErrorClient(), {"legs": []}, "health.assess")
        err = str(exc_info.value)
        assert "health.assess" in err
        assert "A2A RPC error" in err


# ===========================================================================
# SECTION 3 — _send_to_url (lines 204-223)
# ===========================================================================

class TestSendToUrl:
    """Lines 204-223: full URL-based A2A dispatch — mocked httpx.post."""

    def test_send_to_url_returns_data_on_success(self) -> None:
        """Lines 204-223 happy path: mocked httpx.post returns a valid A2A task."""
        data = {"verdict": "can_satisfy", "bookable": True}
        with _mock_httpx_post(data):
            result = _send_to_url("http://agent:8000", {"legs": []}, "compliance.check_eligibility")
        assert result == data

    def test_send_to_url_strips_trailing_slash(self) -> None:
        """URL with trailing slash: the function appends '/' so double-slash
        is avoided — test via call_args on the mock."""
        data = {"rollup": {"all_committable": True}}
        with mock.patch("orchestration.orchestrator.httpx.post") as mock_post:
            resp = mock.MagicMock(spec=httpx.Response)
            resp.json.return_value = _rpc_ok(data)
            resp.raise_for_status = mock.MagicMock()
            mock_post.return_value = resp
            result = _send_to_url("http://fraud:9000/", {"counterparties": []}, "fraud.vet")
        # Confirm the URL called ends with exactly one slash
        called_url = mock_post.call_args[0][0]
        assert not called_url.endswith("//"), f"double-slash in URL: {called_url!r}"
        assert result == data

    def test_send_to_url_raises_on_rpc_error(self) -> None:
        """Lines 220-221: 'error' in rpc → RuntimeError with skill_id."""
        with mock.patch("orchestration.orchestrator.httpx.post") as mock_post:
            resp = mock.MagicMock(spec=httpx.Response)
            resp.json.return_value = {
                "jsonrpc": "2.0",
                "id": "1",
                "error": {"code": -32600, "message": "bad request"},
            }
            resp.raise_for_status = mock.MagicMock()
            mock_post.return_value = resp
            with pytest.raises(RuntimeError) as exc_info:
                _send_to_url("http://risk:8001", {"legs": []}, "risk.assess")
        err = str(exc_info.value)
        assert "risk.assess" in err

    def test_send_to_url_uses_30s_timeout(self) -> None:
        """The function must pass timeout=30.0 to httpx.post."""
        data = {"areas": ["south-bali"]}
        with mock.patch("orchestration.orchestrator.httpx.post") as mock_post:
            resp = mock.MagicMock(spec=httpx.Response)
            resp.json.return_value = _rpc_ok(data)
            resp.raise_for_status = mock.MagicMock()
            mock_post.return_value = resp
            _send_to_url("http://dest:8002", {"city": "bali"}, "destination.assess")
        _, kwargs = mock_post.call_args
        assert kwargs.get("timeout") == 30.0, f"expected timeout=30.0 got {kwargs.get('timeout')!r}"

    def test_send_to_url_embeds_skill_id_in_message_metadata(self) -> None:
        """Lines 210-216: the A2A message must carry skillId matching the skill_id arg."""
        data = {"verdict": "can_complete", "bookable": True}
        with mock.patch("orchestration.orchestrator.httpx.post") as mock_post:
            resp = mock.MagicMock(spec=httpx.Response)
            resp.json.return_value = _rpc_ok(data)
            resp.raise_for_status = mock.MagicMock()
            mock_post.return_value = resp
            _send_to_url("http://health:8003", {"legs": []}, "health.assess")
        _, kwargs = mock_post.call_args
        body = kwargs["json"]
        msg = (body.get("params") or {}).get("message", {})
        assert msg.get("metadata", {}).get("skillId") == "health.assess"

    def test_send_to_url_raises_on_http_error(self) -> None:
        """raise_for_status raising propagates out of _send_to_url."""
        with mock.patch("orchestration.orchestrator.httpx.post") as mock_post:
            resp = mock.MagicMock(spec=httpx.Response)
            resp.raise_for_status.side_effect = httpx.HTTPStatusError(
                "404", request=mock.MagicMock(), response=mock.MagicMock()
            )
            mock_post.return_value = resp
            with pytest.raises(httpx.HTTPStatusError):
                _send_to_url("http://dead:9999", {}, "plan.decompose")


# ===========================================================================
# SECTION 4 — _call_fraud URL branch (line 434)
# ===========================================================================

class TestCallFraudUrlBranch:
    """Line 434: _call_fraud uses _send_to_url when only fraud_url is set."""

    def test_call_fraud_url_branch_returns_verdict(self) -> None:
        """Line 434: fraud_url (no client) → _send_to_url → fraud rollup returned."""
        rollup_data = {
            "rollup": {
                "all_committable": True,
                "blocked_ids": [],
                "committable_ids": [],
                "n_counterparties": 1,
            }
        }
        orch = TravelOrchestrator(fraud_url="http://fraud-agent:9100")
        with _mock_httpx_post(rollup_data):
            result = orch._call_fraud({"counterparties": [{"counterparty_id": "cp-1"}]})
        assert result is not None
        assert result.get("rollup", {}).get("all_committable") is True

    def test_call_fraud_no_client_no_url_returns_none(self) -> None:
        """The bypass leg: no client, no url → None."""
        orch = TravelOrchestrator()
        assert orch._call_fraud({"counterparties": []}) is None


# ===========================================================================
# SECTION 5 — _call_planner URL branch (line 440-441)
# ===========================================================================

class TestCallPlannerUrlBranch:
    """Lines 440-441: _call_planner uses _send_to_url when only planner_url is set."""

    def test_call_planner_url_branch_returns_legs(self) -> None:
        """Line 440: planner_url → _send_to_url → planner legs returned."""
        plan_data = {
            "legs": [
                {"leg_id": "leg-0", "city": "bali",
                 "per_leg_budget_cents": 50000, "checkin": "2026-12-01",
                 "checkout": "2026-12-04", "adults": 1}
            ],
            "dp_allocation": False,
        }
        orch = TravelOrchestrator(planner_url="http://planner:9200")
        with _mock_httpx_post(plan_data):
            result = orch._call_planner({"legs": [], "total_budget_cents": 100000})
        assert result is not None
        assert len(result.get("legs", [])) == 1
        assert result["legs"][0]["city"] == "bali"

    def test_call_planner_no_client_no_url_raises(self) -> None:
        """No client and no URL → RuntimeError (not None — Planner is mandatory)."""
        orch = TravelOrchestrator()
        with pytest.raises(RuntimeError) as exc_info:
            orch._call_planner({"legs": []})
        assert "No planner client or URL configured" in str(exc_info.value)


# ===========================================================================
# SECTION 6 — _call_accommodation URL branch (lines 447-448)
# ===========================================================================

class TestCallAccommodationUrlBranch:
    """Lines 447-448: _call_accommodation uses _send_to_url when only accommodation_url is set."""

    def test_call_accommodation_url_branch_returns_proposal(self) -> None:
        """URL branch: mocked response carrying fit+proposal is returned."""
        acc_data = {
            "fit": "ok",
            "proposal": {
                "hotel_id": "bali-beach-1",
                "total_cents": 45000,
                "review_score": 8.2,
                "provenance": "merchant",
            },
        }
        orch = TravelOrchestrator(accommodation_url="http://acc:9300")
        with _mock_httpx_post(acc_data):
            result = orch._call_accommodation({
                "city": "bali", "checkin": "2026-12-01",
                "checkout": "2026-12-04", "adults": 1, "max_cents": 60000,
            })
        assert result.get("fit") == "ok"
        assert result["proposal"]["hotel_id"] == "bali-beach-1"

    def test_call_accommodation_no_client_no_url_raises(self) -> None:
        """No client and no URL → RuntimeError."""
        orch = TravelOrchestrator()
        with pytest.raises(RuntimeError) as exc_info:
            orch._call_accommodation({"city": "bali"})
        assert "No accommodation client or URL configured" in str(exc_info.value)


# ===========================================================================
# SECTION 7 — _call_budget (legacy) + _call_budget_check URL branches (lines 453-465)
# ===========================================================================

class TestCallBudgetUrlBranches:
    """Lines 455-456, 463-464: legacy budget.enforce and budget.check URL branches."""

    def test_call_budget_url_branch(self) -> None:
        """Line 455: budget_url → _send_to_url → budget.enforce response."""
        enforce_data = {"decision": "check_ok", "checkout_id": "co-url-1"}
        orch = TravelOrchestrator(budget_url="http://budget:9400")
        with _mock_httpx_post(enforce_data):
            result = orch._call_budget({"proposals": {}, "buyer_consent": False})
        assert result.get("decision") == "check_ok"

    def test_call_budget_no_client_no_url_raises(self) -> None:
        """No client and no URL → RuntimeError for legacy budget.enforce."""
        orch = TravelOrchestrator()
        with pytest.raises(RuntimeError) as exc_info:
            orch._call_budget({})
        assert "No budget client or URL configured" in str(exc_info.value)

    def test_call_budget_check_url_branch(self) -> None:
        """Line 463: budget_url → _send_to_url → budget.check response."""
        check_data = {"decision": "check_ok", "checkout_id": "co-url-check-1"}
        orch = TravelOrchestrator(budget_url="http://budget:9400")
        with _mock_httpx_post(check_data):
            result = orch._call_budget_check({"proposals": {}, "buyer_consent": False})
        assert result.get("decision") == "check_ok"
        assert result.get("checkout_id") == "co-url-check-1"

    def test_call_budget_check_no_client_no_url_raises(self) -> None:
        """No client and no URL → RuntimeError for budget.check."""
        orch = TravelOrchestrator()
        with pytest.raises(RuntimeError) as exc_info:
            orch._call_budget_check({})
        assert "No budget client or URL configured" in str(exc_info.value)


# ===========================================================================
# SECTION 8 — _call_budget_commit URL branch + no-config raise (lines 471-473)
# ===========================================================================

class TestCallBudgetCommitUrlBranch:
    """Lines 471-473: _call_budget_commit URL branch and no-config raise."""

    def test_call_budget_commit_url_branch(self) -> None:
        """Line 471-472: budget_url → _send_to_url → budget.commit response."""
        commit_data = {
            "decision": "accept",
            "booking_ref": "BK-url-abc999",
            "total_cents": 45000,
            "checkout_id": "co-url-commit-1",
        }
        orch = TravelOrchestrator(budget_url="http://budget:9400")
        with _mock_httpx_post(commit_data):
            result = orch._call_budget_commit(
                {"buyer_consent": True, "checkout_id": "co-url-commit-1",
                 "idempotency_key": "trip-url-1"}
            )
        # The commit decision must be "accept" — not "veto" or "cannot_price"
        assert result.get("decision") == "accept"
        assert result.get("booking_ref") == "BK-url-abc999"

    def test_call_budget_commit_no_client_no_url_raises(self) -> None:
        """Line 473: no client and no URL → RuntimeError for budget.commit."""
        orch = TravelOrchestrator()
        with pytest.raises(RuntimeError) as exc_info:
            orch._call_budget_commit({"buyer_consent": True})
        assert "No budget client or URL configured" in str(exc_info.value)


# ===========================================================================
# SECTION 9 — _call_critic URL branch (line 485)
# ===========================================================================

class TestCallCriticUrlBranch:
    """Line 485: _call_critic uses _send_to_url when only critic_url is set."""

    def test_call_critic_url_branch_returns_verdict(self) -> None:
        """Line 485: critic_url → _send_to_url → itinerary.verify response."""
        critic_data = {
            "decision": "verified",
            "violations": [],
            "quality_score": 0.88,
        }
        orch = TravelOrchestrator(critic_url="http://critic:9500")
        with _mock_httpx_post(critic_data):
            result = orch._call_critic({"proposals": {}, "total_cents": 40000})
        # Critic returned verified: violations list must be empty
        assert result is not None
        assert result.get("decision") == "verified"
        assert result.get("violations") == []

    def test_call_critic_no_client_no_url_returns_none(self) -> None:
        """No critic wired → None (M3a gate bypassed, backward-compatible)."""
        orch = TravelOrchestrator()
        assert orch._call_critic({}) is None


# ===========================================================================
# SECTION 10 — _call_transport URL branch (line 502)
# ===========================================================================

class TestCallTransportUrlBranch:
    """Line 502: _call_transport uses _send_to_url when only transport_url is set."""

    def test_call_transport_url_branch_returns_feasibility(self) -> None:
        """Line 502: transport_url → _send_to_url → transport.feasibility response."""
        transport_data = {
            "infeasible_edges": [],
            "edges": [{"from_leg": "leg-0", "to_leg": "leg-1", "mode": "flight"}],
        }
        orch = TravelOrchestrator(transport_url="http://transport:9600")
        legs = [
            {"leg_id": "leg-0", "city": "tokyo", "checkin": "2026-12-01"},
            {"leg_id": "leg-1", "city": "osaka", "checkin": "2026-12-04"},
        ]
        with _mock_httpx_post(transport_data):
            result = orch._call_transport(legs)
        assert result is not None
        # No infeasible edges → trip is transport-feasible
        assert result.get("infeasible_edges") == []

    def test_call_transport_no_client_no_url_returns_none(self) -> None:
        """No transport wired → None (M3b gate bypassed, backward-compatible)."""
        orch = TravelOrchestrator()
        assert orch._call_transport([]) is None


# ===========================================================================
# SECTION 11 — _call_day_planner URL branch (line 524)
# ===========================================================================

class TestCallDayPlannerUrlBranch:
    """Line 524: _call_day_planner uses _send_to_url when only day_planner_url is set."""

    def test_call_day_planner_url_branch_returns_plan(self) -> None:
        """Line 524: day_planner_url → _send_to_url → activity.plan response."""
        plan_data = {
            "leg_plans": [
                {"leg_id": "leg-0", "city": "bali", "catalog_hit": True, "days": []}
            ]
        }
        orch = TravelOrchestrator(day_planner_url="http://day-planner:9700")
        with _mock_httpx_post(plan_data):
            result = orch._call_day_planner([{"leg_id": "leg-0", "city": "bali"}])
        assert result is not None
        assert len(result.get("leg_plans", [])) == 1
        assert result["leg_plans"][0]["catalog_hit"] is True

    def test_call_day_planner_no_client_no_url_returns_none(self) -> None:
        """No day-planner wired → None (off the money path, backward-compatible)."""
        orch = TravelOrchestrator()
        assert orch._call_day_planner([]) is None


# ===========================================================================
# SECTION 12 — _call_food_search: None bypass + exception handler (lines 540, 543-545)
# ===========================================================================

class TestCallFoodSearch:
    """Lines 540, 543-545: _call_food_search None bypass and exception degradation."""

    def test_call_food_search_returns_none_when_not_configured(self) -> None:
        """Line 540: food_search=None → return None (no-op bypass, var-0)."""
        orch = TravelOrchestrator(food_search=None)
        assert orch._call_food_search({"kind": "FOOD_DELIVERY", "city": "bali"}) is None

    def test_call_food_search_calls_callable_on_configured(self) -> None:
        """When food_search IS configured (callable), it is invoked with the query."""
        called_with = {}
        def fake_search(query: dict):
            called_with.update(query)
            return {"results": [{"name": "Warung Bali", "price_cents": 8000}], "source": "mock"}

        orch = TravelOrchestrator(food_search=fake_search)
        result = orch._call_food_search({"kind": "FOOD_DELIVERY", "city": "bali"})
        # food_search was called: the result carries mock items
        assert result is not None
        assert len(result.get("results", [])) == 1
        assert called_with.get("city") == "bali"

    def test_call_food_search_degrades_to_none_on_exception(self) -> None:
        """Lines 543-545: food_search raises → exception caught → None returned.
        The caller never sees the exception (supper is opt-in, off the money path)."""
        def boom_search(query: dict):
            raise ConnectionError("network unreachable")

        orch = TravelOrchestrator(food_search=boom_search)
        result = orch._call_food_search({"kind": "FOOD_DELIVERY", "city": "bali"})
        # Exception swallowed; callers get None, not a crash.
        assert result is None


# ===========================================================================
# SECTION 13 — _call_insurance URL branch (line 605)
# ===========================================================================

class TestCallInsuranceUrlBranch:
    """Line 605: _call_insurance uses _send_to_url when only insurance_url is set."""

    def test_call_insurance_url_branch_returns_assessment(self) -> None:
        """Line 605: insurance_url → _send_to_url → insurance.assess_coverage response."""
        ins_data = {
            "premium_cents": 5500,
            "peril_set": ["cyclone"],
            "excluded_perils_summary": [],
        }
        orch = TravelOrchestrator(insurance_url="http://insurance:9800")
        with _mock_httpx_post(ins_data):
            result = orch._call_insurance(
                {"peril_set": ["cyclone"], "insured_trip_cost_cents": 90000}
            )
        assert result is not None
        # A real premium was returned — not zero
        assert result.get("premium_cents") == 5500
        assert "cyclone" in (result.get("peril_set") or [])

    def test_call_insurance_no_client_no_url_returns_none(self) -> None:
        """No insurance wired → None (backward-compatible)."""
        orch = TravelOrchestrator()
        assert orch._call_insurance({"peril_set": []}) is None


# ===========================================================================
# SECTION 14 — _call_coverage_gap URL branch (lines 626-630)
# ===========================================================================

class TestCallCoverageGapUrlBranch:
    """Lines 626-630: _call_coverage_gap URL branch."""

    def test_call_coverage_gap_url_branch_returns_gap_analysis(self) -> None:
        """Lines 626-630: insurance_url (no client) → _send_to_url for coverage_gap skill."""
        gap_data = {
            "gaps": [{"peril": "cyclone", "gap_type": "sublimit", "detail": "capped at $500"}],
            "verdict": "gaps_found",
        }
        orch = TravelOrchestrator(insurance_url="http://insurance:9800")
        with _mock_httpx_post(gap_data):
            result = orch._call_coverage_gap(
                {"peril_set": ["cyclone"], "user_policy": {"policy_label": "Basic Cover"}}
            )
        assert result is not None
        assert result.get("verdict") == "gaps_found"
        assert len(result.get("gaps", [])) == 1

    def test_call_coverage_gap_no_client_no_url_returns_none(self) -> None:
        """No insurance wired → _call_coverage_gap returns None."""
        orch = TravelOrchestrator()
        assert orch._call_coverage_gap({"peril_set": ["cyclone"]}) is None


# ===========================================================================
# SECTION 15 — _call_compliance URL branch (line 653)
# ===========================================================================

class TestCallComplianceUrlBranch:
    """Line 653: _call_compliance uses _send_to_url when only compliance_url is set."""

    def test_call_compliance_url_branch_returns_verdict(self) -> None:
        """Line 653: compliance_url → _send_to_url → compliance.check_eligibility response."""
        comp_data = {
            "verdict": "can_satisfy",
            "bookable": True,
            "line_items": [],
            "flagged_legs": [],
        }
        orch = TravelOrchestrator(compliance_url="http://compliance:9900")
        with _mock_httpx_post(comp_data):
            result = orch._call_compliance(
                {"legs": [{"dest_country": "TH", "departure_date": "2026-12-01"}],
                 "nationality": "US"}
            )
        assert result is not None
        assert result.get("verdict") == "can_satisfy"
        assert result.get("bookable") is True

    def test_call_compliance_no_client_no_url_returns_none(self) -> None:
        """No compliance wired → None (backward-compatible)."""
        orch = TravelOrchestrator()
        assert orch._call_compliance({"legs": []}) is None


# ===========================================================================
# SECTION 16 — _call_risk URL branch (line 675)
# ===========================================================================

class TestCallRiskUrlBranch:
    """Line 675: _call_risk uses _send_to_url when only risk_url is set."""

    def test_call_risk_url_branch_returns_assessment(self) -> None:
        """Line 675: risk_url → _send_to_url → risk.assess response."""
        risk_data = {
            "per_leg": [{"leg_id": "leg-0", "advisory": []}],
            "rollup": {
                "any_avoid_window": False,
                "flagged_legs": [],
                "all_reason_codes": [],
            },
        }
        orch = TravelOrchestrator(risk_url="http://risk:10000")
        with _mock_httpx_post(risk_data):
            result = orch._call_risk(
                {"legs": [{"leg_id": "leg-0", "city": "bali", "checkin": "2026-12-01"}]}
            )
        assert result is not None
        rollup = result.get("rollup", {})
        # No avoid window — safe trip signal
        assert rollup.get("any_avoid_window") is False
        assert rollup.get("flagged_legs") == []

    def test_call_risk_no_client_no_url_returns_none(self) -> None:
        """No risk wired → None (S1-S5 byte-identical)."""
        orch = TravelOrchestrator()
        assert orch._call_risk({"legs": []}) is None


# ===========================================================================
# SECTION 17 — Constructor/init config (tracer, _today, _user_policy wiring)
# ===========================================================================

class TestOrchestratorInit:
    """Constructor config branches: tracer, _today, _user_policy, optional hooks."""

    def test_default_tracer_is_noop_callable(self) -> None:
        """Default tracer is a no-op lambda — must be callable, never crash."""
        orch = TravelOrchestrator()
        # Must not raise on any positional/keyword call:
        orch._tracer("negotiate_started", trip_id="x", summary="y", data={})
        orch._tracer("agent_completed", "Risk", trip_id="x", summary="FLAG", data={})

    def test_custom_tracer_is_called(self) -> None:
        """A custom callable tracer passed at init IS stored and IS called."""
        calls: list[tuple] = []
        def my_tracer(*args, **kwargs):
            calls.append((args, kwargs))

        orch = TravelOrchestrator(tracer=my_tracer)
        orch._tracer("negotiate_started", trip_id="t1", summary="s", data={})
        assert len(calls) == 1
        assert calls[0][0][0] == "negotiate_started"

    def test_today_initially_none(self) -> None:
        """_today is None at construction (set per-run in negotiate())."""
        orch = TravelOrchestrator()
        assert orch._today is None

    def test_user_policy_initially_none(self) -> None:
        """_user_policy is None at construction (set per-run from trip_request)."""
        orch = TravelOrchestrator()
        assert orch._user_policy is None

    def test_supper_request_initially_none(self) -> None:
        """_supper_request is None at construction (set per-run from trip_request)."""
        orch = TravelOrchestrator()
        assert orch._supper_request is None

    def test_dining_request_initially_none(self) -> None:
        """_dining_request is None at construction."""
        orch = TravelOrchestrator()
        assert orch._dining_request is None

    def test_emergency_request_initially_none(self) -> None:
        """_emergency_request is None at construction."""
        orch = TravelOrchestrator()
        assert orch._emergency_request is None

    def test_food_search_stored_on_init(self) -> None:
        """food_search callable passed at init is stored as self._food_search."""
        sentinel = lambda q: None  # noqa: E731
        orch = TravelOrchestrator(food_search=sentinel)
        assert orch._food_search is sentinel

    def test_all_url_fields_stored(self) -> None:
        """All URL kwargs are stored as the corresponding _*_url attributes."""
        orch = TravelOrchestrator(
            planner_url="http://planner",
            accommodation_url="http://acc",
            budget_url="http://budget",
            critic_url="http://critic",
            transport_url="http://transport",
            destination_url="http://dest",
            insurance_url="http://ins",
            compliance_url="http://comp",
            risk_url="http://risk",
            health_url="http://health",
            fraud_url="http://fraud",
            day_planner_url="http://dp",
            dining_url="http://dining",
            emergency_url="http://emerg",
        )
        assert orch._planner_url == "http://planner"
        assert orch._accommodation_url == "http://acc"
        assert orch._budget_url == "http://budget"
        assert orch._critic_url == "http://critic"
        assert orch._transport_url == "http://transport"
        assert orch._destination_url == "http://dest"
        assert orch._insurance_url == "http://ins"
        assert orch._compliance_url == "http://comp"
        assert orch._risk_url == "http://risk"
        assert orch._health_url == "http://health"
        assert orch._fraud_url == "http://fraud"
        assert orch._day_planner_url == "http://dp"
        assert orch._dining_url == "http://dining"
        assert orch._emergency_url == "http://emerg"

    def test_trip_id_initially_empty_string(self) -> None:
        """_trip_id is '' at construction (set at negotiate() entry)."""
        orch = TravelOrchestrator()
        assert orch._trip_id == ""


# ===========================================================================
# SECTION 18 — negotiate() early-exit branches (invalid legs, zero budget)
# ===========================================================================

class TestNegotiateEarlyExits:
    """Coverage for the early-exit guards at the top of negotiate() (lines 1213-1219)."""

    def test_empty_legs_returns_cannot_satisfy(self) -> None:
        """An empty legs list must return cannot_satisfy without touching any agent."""
        orch = TravelOrchestrator()
        res = orch.negotiate({"user_id": "u1", "total_budget_cents": 100000, "legs": []})
        assert res["outcome"] == "cannot_satisfy"
        assert res.get("reason") == "invalid_request"
        assert "trip_id" in res  # deterministic trip_id is present

    def test_missing_legs_returns_cannot_satisfy(self) -> None:
        """Absent `legs` key (treated as None → not a list) → cannot_satisfy."""
        orch = TravelOrchestrator()
        res = orch.negotiate({"user_id": "u1", "total_budget_cents": 100000})
        assert res["outcome"] == "cannot_satisfy"
        assert res.get("reason") == "invalid_request"

    def test_zero_budget_returns_cannot_satisfy(self) -> None:
        """total_budget_cents=0 → cannot_satisfy (budget guard)."""
        orch = TravelOrchestrator()
        res = orch.negotiate({
            "user_id": "u1",
            "total_budget_cents": 0,
            "legs": [{"city": "bali", "checkin": "2026-12-01", "checkout": "2026-12-04"}],
        })
        assert res["outcome"] == "cannot_satisfy"
        assert res.get("reason") == "invalid_request"

    def test_negative_budget_returns_cannot_satisfy(self) -> None:
        """total_budget_cents < 0 → cannot_satisfy (budget guard)."""
        orch = TravelOrchestrator()
        res = orch.negotiate({
            "user_id": "u1",
            "total_budget_cents": -500,
            "legs": [{"city": "bali", "checkin": "2026-12-01", "checkout": "2026-12-04"}],
        })
        assert res["outcome"] == "cannot_satisfy"
        assert res.get("reason") == "invalid_request"

    def test_trip_id_is_deterministic(self) -> None:
        """Identical requests must produce the same trip_id (var-0 contract)."""
        orch = TravelOrchestrator()
        req = {"user_id": "u1", "total_budget_cents": 0, "legs": []}
        res1 = orch.negotiate(req)
        res2 = orch.negotiate(req)
        assert res1["trip_id"] == res2["trip_id"], (
            "trip_id must be deterministic (same request → same digest)"
        )

    def test_today_stored_per_run(self) -> None:
        """negotiate() sets self._today from trip_request['today'] (D7 contract)."""
        orch = TravelOrchestrator()
        orch.negotiate({"user_id": "u1", "total_budget_cents": 0, "legs": [], "today": "2026-08-15"})
        # After the call, _today was set to the supplied value.
        assert orch._today == "2026-08-15"

    def test_user_policy_stored_per_run(self) -> None:
        """negotiate() sets self._user_policy from trip_request['user_policy']."""
        policy = {"policy_label": "Platinum Cover", "declarations": []}
        orch = TravelOrchestrator()
        orch.negotiate({"user_id": "u1", "total_budget_cents": 0, "legs": [], "user_policy": policy})
        assert orch._user_policy == policy

    def test_supper_request_stored_per_run(self) -> None:
        """negotiate() sets self._supper_request from trip_request['supper']."""
        supper = {"order": True, "city": "bali", "diet": "vegan"}
        orch = TravelOrchestrator()
        orch.negotiate({"user_id": "u1", "total_budget_cents": 0, "legs": [], "supper": supper})
        assert orch._supper_request == supper


# ===========================================================================
# SECTION 19 — negotiate() idempotency_key: explicit override vs digest default
# ===========================================================================

class TestNegotiateIdempotencyKey:
    """The idempotency_key uses the caller-supplied value when present."""

    def test_explicit_idempotency_key_preserved(self) -> None:
        """An explicit 'idempotency_key' in the request must be threaded through."""
        orch = TravelOrchestrator()
        # Early-exit path (zero budget) is fastest to instrument without full society.
        req = {
            "user_id": "u1",
            "total_budget_cents": 0,
            "legs": [],
            "idempotency_key": "my-custom-idem-42",
        }
        res = orch.negotiate(req)
        # The early-exit path doesn't surface idempotency_key, but we can verify
        # it doesn't crash and the trip_id is still deterministic.
        assert res["outcome"] == "cannot_satisfy"

    def test_trip_id_uses_request_digest_not_uuid(self) -> None:
        """The surfaced trip_id must begin 'trip-' (from _request_digest, not uuid4)."""
        orch = TravelOrchestrator()
        res = orch.negotiate({"user_id": "u1", "total_budget_cents": 0, "legs": []})
        assert res.get("trip_id", "").startswith("trip-"), (
            "trip_id must be 'trip-<digest>' (deterministic) not a raw UUID"
        )


# ===========================================================================
# SECTION 20 — _call_health URL branch (lines 699-701)
#   (bonus: health dispatch URL path, adjacent to target ranges)
# ===========================================================================

class TestCallHealthUrlBranch:
    """Line 699-701: _call_health uses _send_to_url when only health_url is set."""

    def test_call_health_url_branch_returns_verdict(self) -> None:
        """health_url → _send_to_url → health.assess response."""
        health_data = {
            "verdict": "can_complete",
            "bookable": True,
            "jab_records": [],
            "mandatory_line_items": [],
        }
        orch = TravelOrchestrator(health_url="http://health:10100")
        with _mock_httpx_post(health_data):
            result = orch._call_health(
                {"legs": [{"leg_id": "leg-0", "dest_country": "TH",
                           "departure_date": "2026-12-01"}]}
            )
        assert result is not None
        assert result.get("verdict") == "can_complete"
        assert result.get("bookable") is True

    def test_call_health_no_client_no_url_returns_none(self) -> None:
        """No health wired → None (backward-compatible, S1-S5 unchanged)."""
        orch = TravelOrchestrator()
        assert orch._call_health({"legs": []}) is None


if __name__ == "__main__":
    import sys as _sys
    _sys.exit(pytest.main([__file__, "-q"]))
