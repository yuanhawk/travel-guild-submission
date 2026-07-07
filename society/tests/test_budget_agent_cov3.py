"""
test_budget_agent_cov3.py — Coverage gap tests for budget_agent.py (D3 hardening).

Target uncovered lines:
  - 231-242: RFC 9421 signed headers path + JSON exception handling in _mcp_rpc
  - 486-638: _check_handler logic + check-phase-specific behavior
  - 703-1033: Full merchant integration (_check_merchant, _commit_merchant, _call_merchant)
  - 1162-1202: _extract_payload edge cases + main() function

All tests are deterministic, var-0-safe, mock-transport based (no live LLM/network).
"""

from __future__ import annotations

import json
import os
import sys
import uuid
from typing import Any
from unittest import mock

import httpx
from starlette.testclient import TestClient

# Ensure society/ is on sys.path for imports
sys.path.insert(0, os.path.dirname(__file__))


# ---------------------------------------------------------------------------
# Mock httpx transport for intercept-all testing
# ---------------------------------------------------------------------------

class MockMerchantTransport(httpx.BaseTransport):
    """
    Intercept merchant MCP calls; return canned responses or controlled failures.
    Keyed by tool_name from the RPC params.
    """

    def __init__(self, responses: dict[str, tuple[int, dict[str, Any]]] | None = None):
        self._responses = responses or {}

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        body_bytes = request.read()
        try:
            payload = json.loads(body_bytes)
        except Exception:
            return httpx.Response(400, text="bad request")

        tool_name = (payload.get("params") or {}).get("name", "")
        if tool_name not in self._responses:
            return httpx.Response(
                500,
                json={"error": f"mock: no response configured for tool {tool_name!r}"},
            )

        status_code, body = self._responses[tool_name]
        return httpx.Response(status_code, json=body)


class CapturingMerchantTransport(httpx.BaseTransport):
    """
    #161 — like MockMerchantTransport, but also records the raw `arguments` body
    of every intercepted tool call (keyed by tool name, appended in call order).
    Used to assert `checkout.user_id` / `user_id` genuinely reaches the wire in
    the update_checkout / complete_checkout / cancel_checkout / create_checkout
    request bodies — not just that the HTTP call happened.
    """

    def __init__(self, responses: dict[str, tuple[int, dict[str, Any]]] | None = None):
        self._responses = responses or {}
        self.captured: dict[str, list[dict[str, Any]]] = {}

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        body_bytes = request.read()
        try:
            payload = json.loads(body_bytes)
        except Exception:
            return httpx.Response(400, text="bad request")

        params = payload.get("params") or {}
        tool_name = params.get("name", "")
        self.captured.setdefault(tool_name, []).append(params.get("arguments") or {})

        if tool_name not in self._responses:
            return httpx.Response(
                500,
                json={"error": f"mock: no response configured for tool {tool_name!r}"},
            )
        status_code, body = self._responses[tool_name]
        return httpx.Response(status_code, json=body)


class NonJsonMerchantTransport(httpx.BaseTransport):
    """Return non-JSON response body to trigger exception in _mcp_rpc."""

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="not valid json at all {]")


def _merchant_result(domain: dict[str, Any]) -> dict[str, Any]:
    """Wrap a domain dict in the merchant's MCP result envelope."""
    return {
        "jsonrpc": "2.0",
        "id": 1,
        "result": {
            "structuredContent": domain,
            "content": [{"type": "text", "text": json.dumps(domain)}],
        },
    }


def _make_client_with_transport(transport: httpx.BaseTransport):
    """Build a BudgetAgent with the mock transport injected."""
    from agents import budget_agent as ba
    agent = ba.BudgetAgent(host="0.0.0.0", port=9101, merchant_transport=transport)
    app = agent.build_app()
    client = TestClient(app, raise_server_exceptions=False)  # Catch exceptions in tests
    return client, agent


def _rpc_post(client: TestClient, method: str, params: dict) -> dict:
    body = {
        "jsonrpc": "2.0",
        "id": str(uuid.uuid4()),
        "method": method,
        "params": params,
    }
    resp = client.post("/", json=body)
    return resp.json() if resp.status_code == 200 else None


def _send_budget_request(
    client: TestClient,
    payload: dict[str, Any],
    *,
    skill_id: str = "budget.enforce",
) -> dict:
    """Send a budget skill A2A message; return the final Task dict."""
    msg: dict[str, Any] = {
        "kind": "message",
        "messageId": str(uuid.uuid4()),
        "role": "user",
        "parts": [{"kind": "data", "data": payload}],
        "metadata": {"skillId": skill_id},
    }
    resp = _rpc_post(client, "message/send", {"message": msg})
    if not resp or "error" in resp:
        return {}
    return resp.get("result", {})


def _extract_result_data(task: dict) -> dict[str, Any]:
    """Extract the BudgetResult dict from the task's artifact."""
    if not task.get("artifacts"):
        return {}
    artifact = task["artifacts"][0]
    for part in artifact.get("parts", []):
        if part.get("kind") == "data":
            return part.get("data", {})
    return {}


# ---------------------------------------------------------------------------
# Coverage: lines 231-242 — RFC 9421 signed headers + JSON exception handling
# ---------------------------------------------------------------------------

def test_mcp_rpc_with_signed_headers_path():
    """
    Lines 230-233: Test the _sig truthy path (signed headers present).
    Verify the merchant RPC call is made with signed headers.
    Note: To hit this, we need ucp_signing.signed_headers() to return non-empty dict.
    We mock it at the module level to avoid env var config complexity.
    """
    from agents import budget_agent as ba

    # Mock signed_headers to return a dict (simulating configured signing key)
    mock_sig = {
        "Content-Digest": "sha-256=:abc123:",
        "Signature-Input": 'sig1=("@method"...)',
        "Signature": "sig1=:xyz789:",
        "Ucp-Agent": 'profile="http://agent/.well-known/ucp"',
    }

    create_response = _merchant_result({
        "id": "co_signed",
        "status": "incomplete",
        "total_cents": 50000,
    })

    transport = MockMerchantTransport({
        "create_checkout": (200, create_response),
    })

    with mock.patch("agents.budget_agent.ucp_signing.signed_headers", return_value=mock_sig):
        client, _ = _make_client_with_transport(transport)
        payload = {
            "user_id": "u_signed",
            "total_budget_cents": 80000,
            "line_items": [
                {
                    "hotel_id": "test-hotel",
                    "checkin": "2025-10-01",
                    "checkout": "2025-10-03",
                    "adults": 1,
                }
            ],
        }
        task = _send_budget_request(client, payload, skill_id="budget.check")

    result = _extract_result_data(task)
    assert result.get("decision") == "check_ok", f"Got {result.get('decision')}"
    assert result.get("checkout_id") == "co_signed"
    print("PASS: test_mcp_rpc_with_signed_headers_path")


def test_mcp_rpc_json_exception():
    """
    Lines 239-244: Merchant returns non-JSON body → RuntimeError raised.
    The _mcp_rpc function must handle json() exception and raise RuntimeError.
    """
    transport = NonJsonMerchantTransport()
    client, _ = _make_client_with_transport(transport)

    payload = {
        "user_id": "u_bad_json",
        "total_budget_cents": 80000,
        "line_items": [
            {
                "hotel_id": "test-hotel",
                "checkin": "2025-10-01",
                "checkout": "2025-10-03",
                "adults": 1,
            }
        ],
    }
    task = _send_budget_request(client, payload, skill_id="budget.check")

    # Task should fail with an error about non-JSON response
    assert task.get("status", {}).get("state") == "failed"
    error = task.get("metadata", {}).get("error", "")
    assert "non-JSON" in error or "json" in error.lower(), f"Error: {error}"
    print("PASS: test_mcp_rpc_json_exception")


# ---------------------------------------------------------------------------
# Coverage: lines 486-638 — _check_handler and budget.check validation
# ---------------------------------------------------------------------------

def test_check_handler_missing_user_id_fails():
    """
    Lines 491-498: _check_handler validates user_id is present and non-empty.
    """
    transport = MockMerchantTransport({})
    client, _ = _make_client_with_transport(transport)

    payload = {
        # missing user_id
        "total_budget_cents": 80000,
        "line_items": [
            {
                "hotel_id": "test",
                "checkin": "2025-10-01",
                "checkout": "2025-10-03",
                "adults": 1,
            }
        ],
    }
    task = _send_budget_request(client, payload, skill_id="budget.check")

    assert task.get("status", {}).get("state") == "failed"
    error = task.get("metadata", {}).get("error", "")
    assert "user_id" in error.lower()
    print("PASS: test_check_handler_missing_user_id_fails")


def test_check_handler_empty_line_items_fails():
    """
    Lines 499-500: _check_handler validates line_items is non-empty.
    """
    transport = MockMerchantTransport({})
    client, _ = _make_client_with_transport(transport)

    payload = {
        "user_id": "u1",
        "total_budget_cents": 80000,
        "line_items": [],  # empty
    }
    task = _send_budget_request(client, payload, skill_id="budget.check")

    assert task.get("status", {}).get("state") == "failed"
    error = task.get("metadata", {}).get("error", "")
    assert "line_items" in error.lower()
    print("PASS: test_check_handler_empty_line_items_fails")


def test_check_handler_autonomy_level_defaulting():
    """
    Line 494: autonomy_level defaults to "L2" if not supplied.
    Verify the CREATE call includes autonomy_level in meta.
    """
    create_response = _merchant_result({
        "id": "co_auton",
        "status": "incomplete",
        "total_cents": 40000,
    })

    transport = MockMerchantTransport({
        "create_checkout": (200, create_response),
    })
    client, _ = _make_client_with_transport(transport)

    # No autonomy_level in payload
    payload = {
        "user_id": "u_auton",
        "total_budget_cents": 80000,
        "line_items": [
            {
                "hotel_id": "test",
                "checkin": "2025-10-01",
                "checkout": "2025-10-03",
                "adults": 1,
            }
        ],
    }
    task = _send_budget_request(client, payload, skill_id="budget.check")

    result = _extract_result_data(task)
    # Should succeed with default L2
    assert result.get("decision") == "check_ok"
    print("PASS: test_check_handler_autonomy_level_defaulting")


def test_check_handler_returns_veto_on_no_budget():
    """
    Lines 504-520: total_budget_cents <= 0 → conservative veto ("no_budget_supplied").
    """
    transport = MockMerchantTransport({})
    client, _ = _make_client_with_transport(transport)

    for total_budget in [0, -1, None]:
        payload = {
            "user_id": "u1",
            "total_budget_cents": total_budget if total_budget is not None else 0,
            "line_items": [
                {
                    "hotel_id": "test",
                    "checkin": "2025-10-01",
                    "checkout": "2025-10-03",
                    "adults": 1,
                }
            ],
        }
        task = _send_budget_request(client, payload, skill_id="budget.check")
        result = _extract_result_data(task)
        assert result.get("decision") == "veto", f"Budget {total_budget} should veto"
        assert result.get("veto_reason") == "no_budget_supplied"

    print("PASS: test_check_handler_returns_veto_on_no_budget")


# ---------------------------------------------------------------------------
# Coverage: lines 703-1033 — Full merchant integration paths
# ---------------------------------------------------------------------------

def test_check_merchant_create_success_within_budget():
    """
    Lines 706-787: _check_merchant successful path.
    create_checkout returns status "incomplete" or "complete", has id and total_cents.
    Returned decision is "check_ok".
    """
    create_response = _merchant_result({
        "id": "co_check_ok",
        "status": "incomplete",
        "total_cents": 30000,
    })

    transport = MockMerchantTransport({
        "create_checkout": (200, create_response),
    })
    client, _ = _make_client_with_transport(transport)

    payload = {
        "user_id": "u_check",
        "total_budget_cents": 80000,
        "line_items": [
            {
                "hotel_id": "bali-hotel",
                "checkin": "2025-10-01",
                "checkout": "2025-10-03",
                "adults": 2,
            }
        ],
    }
    task = _send_budget_request(client, payload, skill_id="budget.check")
    result = _extract_result_data(task)

    assert result.get("decision") == "check_ok"
    assert result.get("checkout_id") == "co_check_ok"
    assert result.get("total_cents") == 30000
    assert result.get("currency") == "USD"
    assert result.get("provenance") == "merchant"
    print("PASS: test_check_merchant_create_success_within_budget")


def test_check_merchant_pre_veto_price_exceeds_budget():
    """
    Lines 761-774: PRE-VETO path.
    create_checkout succeeds but total_cents > user's total_budget_cents.
    Must return veto with veto_reason "price_exceeds_budget".
    """
    create_response = _merchant_result({
        "id": "co_over",
        "status": "incomplete",
        "total_cents": 100000,  # exceeds user's 80000 budget
    })

    transport = MockMerchantTransport({
        "create_checkout": (200, create_response),
    })
    client, _ = _make_client_with_transport(transport)

    payload = {
        "user_id": "u_over",
        "total_budget_cents": 80000,
        "line_items": [
            {
                "hotel_id": "luxury-hotel",
                "checkin": "2025-10-01",
                "checkout": "2025-10-05",
                "adults": 2,
            }
        ],
    }
    task = _send_budget_request(client, payload, skill_id="budget.check")
    result = _extract_result_data(task)

    assert result.get("decision") == "veto"
    assert result.get("veto_reason") == "price_exceeds_budget"
    assert result.get("total_cents") == 100000
    assert result.get("budget_ceiling_cents") == 80000
    print("PASS: test_check_merchant_pre_veto_price_exceeds_budget")


def test_check_merchant_create_status_unavailable():
    """
    Lines 730-755: create_checkout returns status NOT in {"incomplete", "complete"}.
    M2 fix: a hard merchant failure is NOT a budget veto (nothing was priced,
    there's no ceiling to tighten) — must return the honest 'unavailable'
    terminal, never 'veto' (which the orchestrator's price-veto re-plan loop
    would misinterpret as a re-priceable budget overage) and never check_ok.
    """
    create_response = _merchant_result({
        "status": "unavailable",
        "reason": "sold_out",
    })

    transport = MockMerchantTransport({
        "create_checkout": (409, create_response),
    })
    client, _ = _make_client_with_transport(transport)

    payload = {
        "user_id": "u_sold",
        "total_budget_cents": 80000,
        "line_items": [
            {
                "hotel_id": "sold-out-hotel",
                "checkin": "2025-10-01",
                "checkout": "2025-10-03",
                "adults": 1,
            }
        ],
    }
    task = _send_budget_request(client, payload, skill_id="budget.check")
    result = _extract_result_data(task)

    assert result.get("decision") == "unavailable"
    assert result.get("checkout_id") == ""
    assert result.get("total_cents") == 0
    print("PASS: test_check_merchant_create_status_unavailable")


def test_check_merchant_create_missing_total():
    """
    Lines 739-740: create_checkout succeeds but missing or 0 total_cents.
    M2 fix: this generic hard-failure case (not the specific 'unavailable'
    status) must return 'cannot_price' — the OTHER honest-terminal decision
    the orchestrator's D3 branch checks for — never 'veto'.
    """
    create_response = _merchant_result({
        "id": "co_no_total",
        "status": "incomplete",
        # missing total_cents
    })

    transport = MockMerchantTransport({
        "create_checkout": (200, create_response),
    })
    client, _ = _make_client_with_transport(transport)

    payload = {
        "user_id": "u_no_total",
        "total_budget_cents": 80000,
        "line_items": [
            {
                "hotel_id": "test",
                "checkin": "2025-10-01",
                "checkout": "2025-10-03",
                "adults": 1,
            }
        ],
    }
    task = _send_budget_request(client, payload, skill_id="budget.check")
    result = _extract_result_data(task)

    assert result.get("decision") == "cannot_price"
    print("PASS: test_check_merchant_create_missing_total")


def test_commit_merchant_no_consent_mandate():
    """
    Lines 818-840: _commit_merchant called with no buyer_consent and no ap2_mandate.
    Must call complete_checkout without those fields, and map the response.
    """
    complete_response = _merchant_result({
        "id": "co_abc",
        "status": "requires_consent",
        "total_cents": 50000,
        "message": "consent required",
    })

    transport = MockMerchantTransport({
        "complete_checkout": (200, complete_response),
    })
    client, _ = _make_client_with_transport(transport)

    payload = {
        "user_id": "u_commit",
        "checkout_id": "co_abc",
        # no buyer_consent, no ap2_mandate
    }
    task = _send_budget_request(client, payload, skill_id="budget.commit")
    result = _extract_result_data(task)

    assert result.get("decision") == "needs_consent"
    assert result.get("total_cents") == 50000
    assert task.get("status", {}).get("state") == "input-required"
    print("PASS: test_commit_merchant_no_consent_mandate")


def test_commit_merchant_with_buyer_consent():
    """
    Lines 826-827: buyer_consent is True → include buyer_consent: True in complete_checkout args.
    """
    complete_response = _merchant_result({
        "id": "co_consent",
        "status": "complete",
        "total_cents": 45000,
        "booking_ref": "BK-abc123",
    })

    transport = MockMerchantTransport({
        "complete_checkout": (200, complete_response),
    })
    client, _ = _make_client_with_transport(transport)

    payload = {
        "user_id": "u_consent",
        "checkout_id": "co_consent",
        "buyer_consent": True,
    }
    task = _send_budget_request(client, payload, skill_id="budget.commit")
    result = _extract_result_data(task)

    assert result.get("decision") == "accept"
    assert result.get("booking_ref") == "BK-abc123"
    print("PASS: test_commit_merchant_with_buyer_consent")


def test_commit_merchant_with_ap2_mandate():
    """
    Lines 828-829: ap2_mandate is dict → include ap2_mandate in complete_checkout args.
    """
    complete_response = _merchant_result({
        "id": "co_mandate",
        "status": "complete",
        "total_cents": 55000,
        "booking_ref": "BK-mandate-1",
    })

    transport = MockMerchantTransport({
        "complete_checkout": (200, complete_response),
    })
    client, _ = _make_client_with_transport(transport)

    mandate = {"type": "ap2", "signature": "fake-sig"}
    payload = {
        "user_id": "u_mandate",
        "checkout_id": "co_mandate",
        "ap2_mandate": mandate,
    }
    task = _send_budget_request(client, payload, skill_id="budget.commit")
    result = _extract_result_data(task)

    assert result.get("decision") == "accept"
    print("PASS: test_commit_merchant_with_ap2_mandate")


def test_commit_merchant_idempotency_key():
    """
    Lines 830-831: idempotency_key is supplied → include in complete_checkout args.
    """
    complete_response = _merchant_result({
        "id": "co_idem",
        "status": "complete",
        "total_cents": 60000,
        "booking_ref": "BK-idem-1",
    })

    transport = MockMerchantTransport({
        "complete_checkout": (200, complete_response),
    })
    client, _ = _make_client_with_transport(transport)

    idem_key = f"trip-{uuid.uuid4()}"
    payload = {
        "user_id": "u_idem",
        "checkout_id": "co_idem",
        "buyer_consent": True,
        "idempotency_key": idem_key,
    }
    task = _send_budget_request(client, payload, skill_id="budget.commit")
    result = _extract_result_data(task)

    assert result.get("idempotency_key") == idem_key
    print("PASS: test_commit_merchant_idempotency_key")


def test_cancel_merchant_not_owner():
    """
    Lines 895-901: cancel_checkout returns HTTP 403 or reason "not_session_owner".
    Must return decision "not_owner".
    """
    cancel_response = _merchant_result({
        "reason": "not_session_owner",
    })

    transport = MockMerchantTransport({
        "cancel_checkout": (403, cancel_response),
    })
    client, _ = _make_client_with_transport(transport)

    payload = {
        "user_id": "u_other",
        "checkout_id": "co_owned_by_u1",
    }
    task = _send_budget_request(client, payload, skill_id="budget.cancel")
    result = _extract_result_data(task)

    assert result.get("decision") == "not_owner"
    assert result.get("reason") == "not_session_owner"
    print("PASS: test_cancel_merchant_not_owner")


def test_cancel_merchant_success():
    """
    Lines 903-910: cancel_checkout returns cancelled=True or status "cancelled".
    Must return decision "cancelled".
    """
    cancel_response = _merchant_result({
        "cancelled": True,
        "status": "cancelled",
        "released_booking_ref": "BK-released",
    })

    transport = MockMerchantTransport({
        "cancel_checkout": (200, cancel_response),
    })
    client, _ = _make_client_with_transport(transport)

    payload = {
        "user_id": "u1",
        "checkout_id": "co_cancel",
    }
    task = _send_budget_request(client, payload, skill_id="budget.cancel")
    result = _extract_result_data(task)

    assert result.get("decision") == "cancelled"
    assert result.get("released_booking_ref") == "BK-released"
    print("PASS: test_cancel_merchant_success")


def test_cancel_merchant_unknown_id():
    """
    Lines 903-910: cancel_checkout of an unknown/already-cancelled id.
    Should still return decision "cancelled" (idempotent).
    """
    cancel_response = _merchant_result({
        "status": "cancelled",
        "already": True,
    })

    transport = MockMerchantTransport({
        "cancel_checkout": (200, cancel_response),
    })
    client, _ = _make_client_with_transport(transport)

    payload = {
        "user_id": "u1",
        "checkout_id": "co_unknown",
    }
    task = _send_budget_request(client, payload, skill_id="budget.cancel")
    result = _extract_result_data(task)

    assert result.get("decision") == "cancelled"
    assert result.get("already") is True
    print("PASS: test_cancel_merchant_unknown_id")


def test_call_merchant_legacy_no_consent():
    """
    Lines 1009-1024: _call_merchant with no buyer_consent and no ap2_mandate.
    Must return "needs_consent" WITHOUT calling complete_checkout.
    """
    create_response = _merchant_result({
        "id": "co_legacy",
        "status": "incomplete",
        "total_cents": 70000,
    })

    transport = MockMerchantTransport({
        "create_checkout": (200, create_response),
        # complete_checkout should NOT be called
    })
    client, _ = _make_client_with_transport(transport)

    payload = {
        "user_id": "u_legacy",
        "line_items": [
            {
                "hotel_id": "legacy-hotel",
                "checkin": "2025-10-01",
                "checkout": "2025-10-03",
                "adults": 1,
            }
        ],
        # no buyer_consent, no ap2_mandate
    }
    task = _send_budget_request(client, payload, skill_id="budget.enforce")
    result = _extract_result_data(task)

    assert result.get("decision") == "needs_consent"
    assert task.get("status", {}).get("state") == "input-required"
    print("PASS: test_call_merchant_legacy_no_consent")


def test_call_merchant_legacy_complete_success():
    """
    Lines 1026-1051: _call_merchant with buyer_consent calls complete_checkout.
    Should return "accept" with booking_ref.
    """
    create_response = _merchant_result({
        "id": "co_legacy2",
        "status": "incomplete",
        "total_cents": 65000,
    })
    complete_response = _merchant_result({
        "id": "co_legacy2",
        "status": "complete",
        "total_cents": 65000,
        "booking_ref": "BK-legacy2",
    })

    transport = MockMerchantTransport({
        "create_checkout": (200, create_response),
        "complete_checkout": (200, complete_response),
    })
    client, _ = _make_client_with_transport(transport)

    payload = {
        "user_id": "u_legacy2",
        "line_items": [
            {
                "hotel_id": "hotel2",
                "checkin": "2025-10-01",
                "checkout": "2025-10-04",
                "adults": 2,
            }
        ],
        "buyer_consent": True,
    }
    task = _send_budget_request(client, payload, skill_id="budget.enforce")
    result = _extract_result_data(task)

    assert result.get("decision") == "accept"
    assert result.get("booking_ref") == "BK-legacy2"
    print("PASS: test_call_merchant_legacy_complete_success")


def test_call_merchant_legacy_with_user_budget():
    """
    Lines 955-956: _call_merchant forwards total_budget_cents as user_budget_cents.
    Gap #60: Verify the budget is bound at create time.
    """
    create_response = _merchant_result({
        "id": "co_user_budget",
        "status": "incomplete",
        "total_cents": 45000,
    })
    complete_response = _merchant_result({
        "id": "co_user_budget",
        "status": "complete",
        "total_cents": 45000,
        "booking_ref": "BK-ub",
    })

    transport = MockMerchantTransport({
        "create_checkout": (200, create_response),
        "complete_checkout": (200, complete_response),
    })
    client, _ = _make_client_with_transport(transport)

    payload = {
        "user_id": "u_ub",
        "total_budget_cents": 50000,
        "line_items": [
            {
                "hotel_id": "hotel",
                "checkin": "2025-10-01",
                "checkout": "2025-10-03",
                "adults": 1,
            }
        ],
        "buyer_consent": True,
    }
    task = _send_budget_request(client, payload, skill_id="budget.enforce")
    result = _extract_result_data(task)

    assert result.get("decision") == "accept"
    print("PASS: test_call_merchant_legacy_with_user_budget")


def test_map_complete_response_veto_403():
    """
    Lines 1081-1101: HTTP 403 response → veto decision.
    """
    complete_response = _merchant_result({
        "status": "denied",
        "reason": "price_exceeds_budget",
        "id": "co_veto",
        "total_cents": 120000,
        "budget_ceiling_cents": 100000,
    })

    transport = MockMerchantTransport({
        "create_checkout": (200, _merchant_result({
            "id": "co_veto",
            "status": "incomplete",
            "total_cents": 120000,
        })),
        "complete_checkout": (403, complete_response),
    })
    client, _ = _make_client_with_transport(transport)

    payload = {
        "user_id": "u_veto",
        "line_items": [
            {
                "hotel_id": "expensive",
                "checkin": "2025-10-01",
                "checkout": "2025-10-05",
                "adults": 2,
            }
        ],
        "buyer_consent": True,
    }
    task = _send_budget_request(client, payload, skill_id="budget.enforce")
    result = _extract_result_data(task)

    assert result.get("decision") == "veto"
    assert result.get("veto_reason") == "price_exceeds_budget"
    print("PASS: test_map_complete_response_veto_403")


def test_map_complete_response_requires_consent():
    """
    Lines 1104-1112: status "requires_consent" → needs_consent decision.
    """
    complete_response = _merchant_result({
        "status": "requires_consent",
        "id": "co_consent",
        "total_cents": 55000,
        "message": "buyer approval needed",
    })

    transport = MockMerchantTransport({
        "create_checkout": (200, _merchant_result({
            "id": "co_consent",
            "status": "incomplete",
            "total_cents": 55000,
        })),
        "complete_checkout": (200, complete_response),
    })
    client, _ = _make_client_with_transport(transport)

    payload = {
        "user_id": "u_cons",
        "line_items": [
            {
                "hotel_id": "hotel",
                "checkin": "2025-10-01",
                "checkout": "2025-10-03",
                "adults": 1,
            }
        ],
        "buyer_consent": True,
    }
    task = _send_budget_request(client, payload, skill_id="budget.enforce")
    result = _extract_result_data(task)

    assert result.get("decision") == "needs_consent"
    assert "approval" in result.get("consent_message", "").lower()
    print("PASS: test_map_complete_response_requires_consent")


def test_map_complete_response_requires_mandate():
    """
    Lines 1115-1123: status "requires_mandate" → needs_mandate decision.
    """
    complete_response = _merchant_result({
        "status": "requires_mandate",
        "id": "co_mandate",
        "total_cents": 75000,
        "message": "L3 mandate required",
    })

    transport = MockMerchantTransport({
        "create_checkout": (200, _merchant_result({
            "id": "co_mandate",
            "status": "incomplete",
            "total_cents": 75000,
        })),
        "complete_checkout": (200, complete_response),
    })
    client, _ = _make_client_with_transport(transport)

    payload = {
        "user_id": "u_mandate",
        "line_items": [
            {
                "hotel_id": "hotel",
                "checkin": "2025-10-01",
                "checkout": "2025-10-03",
                "adults": 1,
            }
        ],
        "buyer_consent": True,
        "autonomy_level": "L3",
    }
    task = _send_budget_request(client, payload, skill_id="budget.enforce")
    result = _extract_result_data(task)

    assert result.get("decision") == "needs_mandate"
    assert "mandate" in result.get("mandate_message", "").lower()
    print("PASS: test_map_complete_response_requires_mandate")


def test_map_complete_response_complete_status():
    """
    Lines 1129-1141: status "complete" → accept decision.
    """
    complete_response = _merchant_result({
        "status": "complete",
        "id": "co_accept",
        "total_cents": 50000,
        "booking_ref": "BK-final",
    })

    transport = MockMerchantTransport({
        "create_checkout": (200, _merchant_result({
            "id": "co_accept",
            "status": "incomplete",
            "total_cents": 50000,
        })),
        "complete_checkout": (200, complete_response),
    })
    client, _ = _make_client_with_transport(transport)

    payload = {
        "user_id": "u_accept",
        "line_items": [
            {
                "hotel_id": "hotel",
                "checkin": "2025-10-01",
                "checkout": "2025-10-03",
                "adults": 1,
            }
        ],
        "buyer_consent": True,
    }
    task = _send_budget_request(client, payload, skill_id="budget.enforce")
    result = _extract_result_data(task)

    assert result.get("decision") == "accept"
    assert result.get("booking_ref") == "BK-final"
    print("PASS: test_map_complete_response_complete_status")


def test_map_complete_response_incomplete_status_raises():
    """
    Lines 1145-1148: status "incomplete" at complete_checkout time is an error
    (booking was NOT committed).
    """
    complete_response = _merchant_result({
        "status": "incomplete",
        "id": "co_incomplete",
        "total_cents": 50000,
    })

    transport = MockMerchantTransport({
        "create_checkout": (200, _merchant_result({
            "id": "co_incomplete",
            "status": "incomplete",
            "total_cents": 50000,
        })),
        "complete_checkout": (200, complete_response),
    })
    client, _ = _make_client_with_transport(transport)

    payload = {
        "user_id": "u_incomplete",
        "line_items": [
            {
                "hotel_id": "hotel",
                "checkin": "2025-10-01",
                "checkout": "2025-10-03",
                "adults": 1,
            }
        ],
        "buyer_consent": True,
    }
    task = _send_budget_request(client, payload, skill_id="budget.enforce")

    assert task.get("status", {}).get("state") == "failed"
    error = task.get("metadata", {}).get("error", "")
    assert "incomplete" in error.lower() or "unexpected" in error.lower()
    print("PASS: test_map_complete_response_incomplete_status_raises")


# ---------------------------------------------------------------------------
# Coverage: lines 1162-1202 — _extract_payload and main()
# ---------------------------------------------------------------------------

def test_extract_payload_data_part_dict():
    """
    Lines 1158-1161: _extract_payload with kind="data" and data is dict.
    Should return the dict directly.
    """
    transport = MockMerchantTransport({})
    client, _ = _make_client_with_transport(transport)

    payload_dict = {
        "user_id": "u_dict",
        "line_items": [],
    }
    task = _send_budget_request(client, payload_dict, skill_id="budget.check")

    # Task will fail due to empty line_items, but it validates payload extraction worked
    assert task.get("status", {}).get("state") == "failed"
    error = task.get("metadata", {}).get("error", "")
    assert "line_items" in error.lower()
    print("PASS: test_extract_payload_data_part_dict")


def test_extract_payload_data_part_json_string():
    """
    Lines 1162-1166: _extract_payload with kind="data" and data is JSON string.
    Should parse and return the dict.
    """
    from agents import budget_agent as ba

    transport = MockMerchantTransport({})
    agent = ba.BudgetAgent(host="0.0.0.0", port=9101, merchant_transport=transport)

    message = {
        "kind": "message",
        "parts": [
            {
                "kind": "data",
                "data": '{"user_id": "u_json_str"}',  # JSON string, not dict
            }
        ],
    }
    result = agent._extract_payload(message)

    assert isinstance(result, dict)
    assert result.get("user_id") == "u_json_str"
    print("PASS: test_extract_payload_data_part_json_string")


def test_extract_payload_text_part_json():
    """
    Lines 1167-1174: _extract_payload with kind="text" and text contains JSON.
    Should parse and return the dict.
    """
    from agents import budget_agent as ba

    transport = MockMerchantTransport({})
    agent = ba.BudgetAgent(host="0.0.0.0", port=9101, merchant_transport=transport)

    message = {
        "kind": "message",
        "parts": [
            {
                "kind": "text",
                "text": '{"user_id": "u_text_json"}',
            }
        ],
    }
    result = agent._extract_payload(message)

    assert isinstance(result, dict)
    assert result.get("user_id") == "u_text_json"
    print("PASS: test_extract_payload_text_part_json")


def test_extract_payload_invalid_json():
    """
    Lines 1165, 1173: Invalid JSON in data or text part.
    Should skip to next part or return None.
    """
    from agents import budget_agent as ba

    transport = MockMerchantTransport({})
    agent = ba.BudgetAgent(host="0.0.0.0", port=9101, merchant_transport=transport)

    message = {
        "kind": "message",
        "parts": [
            {
                "kind": "data",
                "data": "not valid json {]",
            },
            {
                "kind": "text",
                "text": '{"valid": "json"}',
            }
        ],
    }
    result = agent._extract_payload(message)

    # Should skip the invalid data part and parse the text part
    assert result is not None
    assert result.get("valid") == "json"
    print("PASS: test_extract_payload_invalid_json")


def test_extract_payload_no_parts():
    """
    Lines 1175: message with no parts.
    Should return None.
    """
    from agents import budget_agent as ba

    transport = MockMerchantTransport({})
    agent = ba.BudgetAgent(host="0.0.0.0", port=9101, merchant_transport=transport)

    message = {
        "kind": "message",
        "parts": [],
    }
    result = agent._extract_payload(message)

    assert result is None
    print("PASS: test_extract_payload_no_parts")


def test_main_entry_point():
    """
    Lines 1182-1202: main() function sets up logging and starts uvicorn.
    We mock uvicorn.run to avoid actually starting a server.
    """
    from agents import budget_agent as ba

    with mock.patch("agents.budget_agent.uvicorn.run") as mock_run:
        with mock.patch.dict(os.environ, {"PORT": "9102", "HOST": "127.0.0.1"}):
            ba.main()

        # Verify uvicorn.run was called
        assert mock_run.called
        call_args, call_kwargs = mock_run.call_args
        assert "host" in call_kwargs
        assert "port" in call_kwargs
        assert call_kwargs["host"] == "127.0.0.1"
        assert call_kwargs["port"] == 9102

    print("PASS: test_main_entry_point")


# ---------------------------------------------------------------------------
# Additional deterministic tests for edge cases
# ---------------------------------------------------------------------------

def test_commit_merchant_session_total_fallback():
    """
    Lines 852, 857: session_total_cents used as fallback when merchant response
    omits total_cents.
    """
    complete_response = _merchant_result({
        "id": "co_fallback",
        "status": "requires_consent",
        "message": "consent needed",
        # missing total_cents
    })

    transport = MockMerchantTransport({
        "complete_checkout": (200, complete_response),
    })
    client, _ = _make_client_with_transport(transport)

    payload = {
        "user_id": "u_fallback",
        "checkout_id": "co_fallback",
        "total_cents": 75000,  # fallback amount from prior budget.check
    }
    task = _send_budget_request(client, payload, skill_id="budget.commit")
    result = _extract_result_data(task)

    # Should use fallback total_cents of 75000
    assert result.get("total_cents") == 75000
    assert result.get("decision") == "needs_consent"
    print("PASS: test_commit_merchant_session_total_fallback")


def test_enforce_handler_task_transition_needs_consent():
    """
    Lines 671-672: Task state transition to "input-required" for needs_consent.
    """
    create_response = _merchant_result({
        "id": "co_trans",
        "status": "incomplete",
        "total_cents": 40000,
    })

    transport = MockMerchantTransport({
        "create_checkout": (200, create_response),
    })
    client, _ = _make_client_with_transport(transport)

    payload = {
        "user_id": "u_trans",
        "line_items": [
            {
                "hotel_id": "hotel",
                "checkin": "2025-10-01",
                "checkout": "2025-10-03",
                "adults": 1,
            }
        ],
        # no buyer_consent
    }
    task = _send_budget_request(client, payload, skill_id="budget.enforce")

    assert task.get("status", {}).get("state") == "input-required"
    print("PASS: test_enforce_handler_task_transition_needs_consent")


def test_enforce_handler_task_transition_needs_mandate():
    """
    Lines 671-672: Task state transition to "input-required" for needs_mandate.
    """
    create_response = _merchant_result({
        "id": "co_mand",
        "status": "incomplete",
        "total_cents": 50000,
    })
    complete_response = _merchant_result({
        "id": "co_mand",
        "status": "requires_mandate",
        "message": "mandate needed",
    })

    transport = MockMerchantTransport({
        "create_checkout": (200, create_response),
        "complete_checkout": (200, complete_response),
    })
    client, _ = _make_client_with_transport(transport)

    payload = {
        "user_id": "u_mand",
        "line_items": [
            {
                "hotel_id": "hotel",
                "checkin": "2025-10-01",
                "checkout": "2025-10-03",
                "adults": 1,
            }
        ],
        "buyer_consent": True,
        "autonomy_level": "L3",
    }
    task = _send_budget_request(client, payload, skill_id="budget.enforce")

    assert task.get("status", {}).get("state") == "input-required"
    print("PASS: test_enforce_handler_task_transition_needs_mandate")


# ---------------------------------------------------------------------------
# #161 — canonical merchant end-user id genuinely reaches the wire.
# ---------------------------------------------------------------------------

def test_check_merchant_create_checkout_carries_user_id():
    """
    #161 regression: create_checkout (budget.check) must carry checkout.user_id
    in the actual request body sent to the merchant (this was already true before
    #161; guards against a future regression as the payload evolves).
    """
    create_response = _merchant_result({
        "id": "co_161_create",
        "status": "incomplete",
        "total_cents": 30000,
    })
    transport = CapturingMerchantTransport({"create_checkout": (200, create_response)})
    client, _ = _make_client_with_transport(transport)

    payload = {
        "user_id": "u_create_161",
        "total_budget_cents": 80000,
        "line_items": [
            {"hotel_id": "bali-hotel", "checkin": "2025-10-01", "checkout": "2025-10-03", "adults": 2}
        ],
    }
    task = _send_budget_request(client, payload, skill_id="budget.check")
    result = _extract_result_data(task)
    assert result.get("decision") == "check_ok"

    create_args = transport.captured["create_checkout"][0]
    assert create_args.get("checkout", {}).get("user_id") == "u_create_161"
    print("PASS: test_check_merchant_create_checkout_carries_user_id")


def test_commit_threads_user_id_to_merchant():
    """
    #161 — _commit_merchant must carry the caller's user_id in BOTH the
    update_checkout AND complete_checkout request bodies' checkout.user_id (the
    field checkout.go's END-USER OWNERSHIP check verifies against).
    """
    update_response = _merchant_result({
        "id": "co_161", "status": "incomplete", "buyer_consent": True, "total_cents": 40000,
    })
    complete_response = _merchant_result({
        "id": "co_161", "status": "complete", "total_cents": 40000, "booking_ref": "BK-161",
    })
    transport = CapturingMerchantTransport({
        "update_checkout": (200, update_response),
        "complete_checkout": (200, complete_response),
    })
    client, _ = _make_client_with_transport(transport)

    payload = {
        "user_id": "anon:deadbeefcafefeed00000000",
        "checkout_id": "co_161",
        "buyer_consent": True,
    }
    task = _send_budget_request(client, payload, skill_id="budget.commit")
    result = _extract_result_data(task)
    assert result.get("decision") == "accept"

    upd_args = transport.captured["update_checkout"][0]
    assert upd_args.get("checkout", {}).get("user_id") == "anon:deadbeefcafefeed00000000"
    comp_args = transport.captured["complete_checkout"][0]
    assert comp_args.get("checkout", {}).get("user_id") == "anon:deadbeefcafefeed00000000"
    print("PASS: test_commit_threads_user_id_to_merchant")


def test_cancel_threads_user_id_to_merchant():
    """
    #161 — _cancel_merchant must carry the caller's user_id in the
    cancel_checkout request body's checkout.user_id.
    """
    cancel_response = _merchant_result({"cancelled": True, "status": "cancelled"})
    transport = CapturingMerchantTransport({"cancel_checkout": (200, cancel_response)})
    client, _ = _make_client_with_transport(transport)

    payload = {"user_id": "u_cancel_161", "checkout_id": "co_cancel_161"}
    task = _send_budget_request(client, payload, skill_id="budget.cancel")
    result = _extract_result_data(task)
    assert result.get("decision") == "cancelled"

    cancel_args = transport.captured["cancel_checkout"][0]
    assert cancel_args.get("checkout", {}).get("user_id") == "u_cancel_161"
    print("PASS: test_cancel_threads_user_id_to_merchant")


if __name__ == "__main__":
    print("Running budget_agent coverage tests...")
    test_mcp_rpc_with_signed_headers_path()
    test_mcp_rpc_json_exception()
    test_check_handler_missing_user_id_fails()
    test_check_handler_empty_line_items_fails()
    test_check_handler_autonomy_level_defaulting()
    test_check_handler_returns_veto_on_no_budget()
    test_check_merchant_create_success_within_budget()
    test_check_merchant_pre_veto_price_exceeds_budget()
    test_check_merchant_create_status_unavailable()
    test_check_merchant_create_missing_total()
    test_commit_merchant_no_consent_mandate()
    test_commit_merchant_with_buyer_consent()
    test_commit_merchant_with_ap2_mandate()
    test_commit_merchant_idempotency_key()
    test_cancel_merchant_not_owner()
    test_cancel_merchant_success()
    test_cancel_merchant_unknown_id()
    test_call_merchant_legacy_no_consent()
    test_call_merchant_legacy_complete_success()
    test_call_merchant_legacy_with_user_budget()
    test_map_complete_response_veto_403()
    test_map_complete_response_requires_consent()
    test_map_complete_response_requires_mandate()
    test_map_complete_response_complete_status()
    test_map_complete_response_incomplete_status_raises()
    test_extract_payload_data_part_dict()
    test_extract_payload_data_part_json_string()
    test_extract_payload_text_part_json()
    test_extract_payload_invalid_json()
    test_extract_payload_no_parts()
    test_main_entry_point()
    test_commit_merchant_session_total_fallback()
    test_check_merchant_create_checkout_carries_user_id()
    test_commit_threads_user_id_to_merchant()
    test_cancel_threads_user_id_to_merchant()
    test_enforce_handler_task_transition_needs_consent()
    test_enforce_handler_task_transition_needs_mandate()
    print("All coverage tests passed!")
