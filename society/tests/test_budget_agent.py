"""
test_budget_agent.py — Unit tests for BudgetAgent (CI-safe, mock merchant).

All merchant HTTP calls are intercepted with a custom httpx transport —
no live network dependency.  Uses Starlette's in-process ASGI TestClient,
matching the M0 pattern in test_a2a.py.

Coverage:
  1. Agent Card — required fields, skill declared, protocol version
  2. budget.enforce: within-budget + buyer_consent  → decision "accept"
  3. budget.enforce: over-budget (mock 403 price_exceeds_budget)  → decision "veto"
     with veto_reason propagated from the mock merchant response
  4. budget.enforce: over-budget (mock 403 price_exceeds_hard_max) → decision "veto"
  5. budget.enforce: no consent/mandate supplied  → decision "needs_consent"
  6. budget.enforce: L2 requires_consent from complete_checkout  → "needs_consent"
  7. budget.enforce: L3 requires_mandate from complete_checkout  → "needs_mandate"
  8. budget.enforce: idempotency_key echoed in result
  9. budget.enforce: missing user_id  → task "failed"
 10. budget.enforce: empty line_items  → task "failed"
 11. tasks/get returns stored task
"""

from __future__ import annotations

import json
import sys
import os
import uuid
from typing import Any

import httpx

# Ensure we can import from society/
sys.path.insert(0, os.path.dirname(__file__))

from starlette.testclient import TestClient

# ---------------------------------------------------------------------------
# Mock httpx transport
# ---------------------------------------------------------------------------

class MockMerchantTransport(httpx.BaseTransport):
    """
    A custom httpx transport that intercepts merchant MCP calls and returns
    canned responses without touching any network.

    The responses are keyed on the MCP tool name extracted from the request body.
    Behaviour is configured per-test via the ``responses`` dict:
        {tool_name: (status_code, body_dict)}
    """

    def __init__(self, responses: dict[str, tuple[int, dict[str, Any]]]) -> None:
        self._responses = responses

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


# ---------------------------------------------------------------------------
# Helpers to make / send A2A requests
# ---------------------------------------------------------------------------

def _make_client_with_transport(transport: httpx.BaseTransport):
    """
    Build a BudgetAgent with the mock merchant transport injected.
    Returns (TestClient, BudgetAgent).
    """
    from agents import budget_agent as ba

    agent = ba.BudgetAgent(host="0.0.0.0", port=9101, merchant_transport=transport)
    app = agent.build_app()
    client = TestClient(app, raise_server_exceptions=True)
    return client, agent


def _rpc_post(client: TestClient, method: str, params: dict) -> dict:
    body = {
        "jsonrpc": "2.0",
        "id": str(uuid.uuid4()),
        "method": method,
        "params": params,
    }
    resp = client.post("/", json=body)
    assert resp.status_code == 200, f"HTTP {resp.status_code}: {resp.text}"
    return resp.json()


def _send_budget_request(
    client: TestClient,
    payload: dict[str, Any],
    *,
    context_id: str | None = None,
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
    if context_id:
        msg["contextId"] = context_id
    resp = _rpc_post(client, "message/send", {"message": msg})
    assert "error" not in resp, f"RPC error: {resp.get('error')}"
    return resp["result"]


def _extract_result_data(task: dict) -> dict[str, Any]:
    """Extract the BudgetResult dict from the task's artifact."""
    assert task["artifacts"], "No artifacts in task"
    artifact = task["artifacts"][0]
    for part in artifact["parts"]:
        if part.get("kind") == "data":
            return part["data"]
    raise AssertionError(f"No data part in artifact: {artifact}")


# ---------------------------------------------------------------------------
# Standard merchant mock responses
# ---------------------------------------------------------------------------

# create_checkout → success (within budget)
CREATE_OK = _merchant_result({
    "id": "co_abc123",
    "status": "incomplete",
    "user_id": "u1",
    "line_items": [{"hotel_id": "bali-alaya-ubud", "checkin": "2025-10-01",
                    "checkout": "2025-10-03", "adults": 1}],
    "total_cents": 33000,
    "currency": "USD",
    "buyer_consent": False,
    "booking_ref": "",
})

# create_checkout → over-budget item (still creates the session)
CREATE_OVER = _merchant_result({
    "id": "co_overbudget1",
    "status": "incomplete",
    "user_id": "u1",
    "line_items": [{"hotel_id": "bali-alila-seminyak", "checkin": "2025-10-01",
                    "checkout": "2025-10-04", "adults": 1}],
    "total_cents": 119400,
    "currency": "USD",
    "buyer_consent": False,
    "booking_ref": "",
})

# update_checkout → success (server-side consent; VULN-003 fix)
UPDATE_OK = _merchant_result({
    "id": "co_abc123",
    "status": "incomplete",
    "total_cents": 33000,
    "buyer_consent": True,
    "currency": "USD",
})

# complete_checkout → accepted (within budget)
COMPLETE_ACCEPT = _merchant_result({
    "id": "co_abc123",
    "status": "complete",
    "user_id": "u1",
    "line_items": [],
    "total_cents": 33000,
    "currency": "USD",
    "buyer_consent": True,
    "booking_ref": "BK-deadbeef",
})

# complete_checkout → denied: price_exceeds_budget (HTTP 403)
COMPLETE_VETO_BUDGET: tuple[int, dict] = (403, _merchant_result({
    "status": "denied",
    "reason": "price_exceeds_budget",
    "id": "co_overbudget1",
    "total_cents": 119400,
    "budget_ceiling_cents": 80000,
    "currency": "USD",
}))

# complete_checkout → denied: price_exceeds_hard_max (HTTP 403)
COMPLETE_VETO_HARDMAX: tuple[int, dict] = (403, _merchant_result({
    "status": "denied",
    "reason": "price_exceeds_hard_max",
    "id": "co_overbudget2",
    "total_cents": 220000,
    "hard_max_cents": 200000,
    "currency": "USD",
}))

# create for hard-max test
CREATE_HARDMAX = _merchant_result({
    "id": "co_overbudget2",
    "status": "incomplete",
    "user_id": "u1",
    "line_items": [],
    "total_cents": 220000,
    "currency": "USD",
    "buyer_consent": False,
    "booking_ref": "",
})

# complete_checkout → requires_consent (HITL gate — no buyer_consent given to server)
COMPLETE_REQUIRES_CONSENT = _merchant_result({
    "status": "requires_consent",
    "id": "co_abc123",
    "message": "buyer_consent required before complete_checkout (HITL gate)",
})

# complete_checkout → requires_mandate (L3)
COMPLETE_REQUIRES_MANDATE = _merchant_result({
    "status": "requires_mandate",
    "id": "co_mandate1",
    "message": "L3 autonomous checkout requires a signed ap2_mandate",
})

# create for mandate test
CREATE_MANDATE = _merchant_result({
    "id": "co_mandate1",
    "status": "incomplete",
    "user_id": "u1",
    "line_items": [],
    "total_cents": 50000,
    "currency": "USD",
    "buyer_consent": False,
    "booking_ref": "",
})


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_agent_card() -> None:
    """Agent Card contains required fields and declares budget.enforce skill."""
    transport = MockMerchantTransport({})
    client, _ = _make_client_with_transport(transport)

    resp = client.get("/.well-known/agent-card.json")
    assert resp.status_code == 200
    card = resp.json()

    print("\n--- Budget Agent Card ---")
    print(json.dumps(card, indent=2))
    print("--- end Agent Card ---\n")

    required_fields = {"name", "description", "url", "version", "capabilities", "skills"}
    missing = required_fields - card.keys()
    assert not missing, f"Missing fields: {missing}"

    assert card["name"] == "budget-agent"
    assert card.get("protocolVersion") == "0.3.0"
    assert card.get("preferredTransport") == "JSONRPC"

    skills = card["skills"]
    skill_ids = [s["id"] for s in skills]
    # v2.0.0: three skills — budget.check, budget.commit, budget.enforce (legacy)
    assert "budget.enforce" in skill_ids, f"budget.enforce missing from {skill_ids}"
    assert "budget.check" in skill_ids, f"budget.check missing from {skill_ids}"
    assert "budget.commit" in skill_ids, f"budget.commit missing from {skill_ids}"
    enforce_skill = next(s for s in skills if s["id"] == "budget.enforce")
    assert "name" in enforce_skill
    assert "description" in enforce_skill
    assert "inputSchema" in enforce_skill
    # veto keyword should appear in description (it's the keystone)
    assert "veto" in enforce_skill["description"].lower()

    print("PASS: test_agent_card")


def test_within_budget_accept() -> None:
    """Within-budget itinerary + buyer_consent → decision 'accept' with booking_ref."""
    transport = MockMerchantTransport({
        "create_checkout": (200, CREATE_OK),
        # VULN-003: update_checkout sets server-side consent before complete.
        "update_checkout": (200, UPDATE_OK),
        "complete_checkout": (200, COMPLETE_ACCEPT),
    })
    client, _ = _make_client_with_transport(transport)

    payload = {
        "user_id": "u1",
        "line_items": [
            {"hotel_id": "bali-alaya-ubud", "checkin": "2025-10-01",
             "checkout": "2025-10-03", "adults": 1}
        ],
        "buyer_consent": True,
    }
    task = _send_budget_request(client, payload)

    print("\n--- within-budget accept task ---")
    print(json.dumps(task, indent=2))
    print("--- end ---\n")

    assert task["status"]["state"] == "completed", f"Expected completed, got {task['status']['state']!r}"

    result = _extract_result_data(task)
    assert result["decision"] == "accept", f"Expected accept, got {result['decision']!r}"
    assert result["checkout_id"] == "co_abc123"
    assert result["total_cents"] == 33000
    assert result["booking_ref"] == "BK-deadbeef"
    assert result["status"] == "complete"
    assert result["provenance"] == "merchant"

    print("PASS: test_within_budget_accept")


def test_over_budget_veto() -> None:
    """
    Over-budget itinerary + buyer_consent → decision 'veto'.
    The veto_reason must be the merchant's EXACT reason string ('price_exceeds_budget').
    This is the keystone test — the veto propagates the real 403 reason.
    """
    UPDATE_OVER_OK = _merchant_result({
        "id": "co_overbudget1", "status": "incomplete",
        "total_cents": 119400, "buyer_consent": True, "currency": "USD",
    })
    transport = MockMerchantTransport({
        "create_checkout": (200, CREATE_OVER),
        "update_checkout": (200, UPDATE_OVER_OK),
        "complete_checkout": COMPLETE_VETO_BUDGET,
    })
    client, _ = _make_client_with_transport(transport)

    payload = {
        "user_id": "u1",
        "line_items": [
            {"hotel_id": "bali-alila-seminyak", "checkin": "2025-10-01",
             "checkout": "2025-10-04", "adults": 1}
        ],
        "buyer_consent": True,
    }
    task = _send_budget_request(client, payload)

    print("\n--- over-budget veto task ---")
    print(json.dumps(task, indent=2))
    print("--- end ---\n")

    assert task["status"]["state"] == "completed"

    result = _extract_result_data(task)
    assert result["decision"] == "veto", f"Expected veto, got {result['decision']!r}"
    assert result["veto_reason"] == "price_exceeds_budget", (
        f"Exact merchant reason expected, got {result['veto_reason']!r}"
    )
    assert result["total_cents"] == 119400
    assert result["budget_ceiling_cents"] == 80000
    assert result["provenance"] == "merchant"
    assert result["checkout_id"] == "co_overbudget1"

    print("PASS: test_over_budget_veto")


def test_hard_max_veto() -> None:
    """Exceeds hard max → 'veto' with veto_reason 'price_exceeds_hard_max'."""
    UPDATE_HARDMAX_OK = _merchant_result({
        "id": "co_overbudget2", "status": "incomplete",
        "total_cents": 220000, "buyer_consent": True, "currency": "USD",
    })
    transport = MockMerchantTransport({
        "create_checkout": (200, CREATE_HARDMAX),
        "update_checkout": (200, UPDATE_HARDMAX_OK),
        "complete_checkout": COMPLETE_VETO_HARDMAX,
    })
    client, _ = _make_client_with_transport(transport)

    payload = {
        "user_id": "u1",
        "line_items": [
            {"hotel_id": "some-expensive-hotel", "checkin": "2025-10-01",
             "checkout": "2025-10-21", "adults": 2}
        ],
        "buyer_consent": True,
    }
    task = _send_budget_request(client, payload)

    result = _extract_result_data(task)
    assert result["decision"] == "veto"
    assert result["veto_reason"] == "price_exceeds_hard_max"
    assert result["total_cents"] == 220000
    assert result["hard_max_cents"] == 200000
    assert result["provenance"] == "merchant"

    print("PASS: test_hard_max_veto")


def test_no_consent_needs_consent() -> None:
    """No buyer_consent and no ap2_mandate → 'needs_consent' (HITL gate)."""
    transport = MockMerchantTransport({
        "create_checkout": (200, CREATE_OK),
        # complete_checkout should NOT be called in this path
    })
    client, _ = _make_client_with_transport(transport)

    payload = {
        "user_id": "u1",
        "line_items": [
            {"hotel_id": "bali-alaya-ubud", "checkin": "2025-10-01",
             "checkout": "2025-10-03", "adults": 1}
        ],
        # no buyer_consent, no ap2_mandate
    }
    task = _send_budget_request(client, payload)

    print("\n--- needs_consent task ---")
    print(json.dumps(task, indent=2))
    print("--- end ---\n")

    # Task state should be input-required (A2A signal for HITL)
    assert task["status"]["state"] == "input-required", (
        f"Expected input-required, got {task['status']['state']!r}"
    )

    result = _extract_result_data(task)
    assert result["decision"] == "needs_consent"
    assert result["checkout_id"] == "co_abc123"
    assert result["total_cents"] == 33000
    assert "consent_message" in result
    assert result["provenance"] == "merchant"

    print("PASS: test_no_consent_needs_consent")


def test_requires_consent_from_merchant() -> None:
    """
    Merchant returns 'requires_consent' from complete_checkout (e.g. buyer_consent
    flag was sent False) → 'needs_consent'.
    """
    transport = MockMerchantTransport({
        "create_checkout": (200, CREATE_OK),
        "complete_checkout": (200, COMPLETE_REQUIRES_CONSENT),
    })
    client, _ = _make_client_with_transport(transport)

    payload = {
        "user_id": "u1",
        "line_items": [
            {"hotel_id": "bali-alaya-ubud", "checkin": "2025-10-01",
             "checkout": "2025-10-03", "adults": 1}
        ],
        "buyer_consent": False,
        # Pass buyer_consent=False so complete_checkout IS called but returns requires_consent
        # We set it as False so the agent calls complete_checkout but gets requires_consent back.
        # Actually in the current design, False => no consent => early return.
        # To exercise the merchant's requires_consent path we need buyer_consent to be truthy
        # so complete_checkout is called. We'll simulate this by passing buyer_consent: True
        # but the mock returns requires_consent from the merchant side.
    }
    # Adjust: send with buyer_consent=True so update_checkout + complete_checkout are called.
    payload["buyer_consent"] = True
    UPDATE_CONSENT_OK = _merchant_result({
        "id": "co_abc123", "status": "incomplete",
        "total_cents": 33000, "buyer_consent": True, "currency": "USD",
    })
    transport2 = MockMerchantTransport({
        "create_checkout": (200, CREATE_OK),
        "update_checkout": (200, UPDATE_CONSENT_OK),
        # The mock complete_checkout returns requires_consent (200, not 403).
        "complete_checkout": (200, COMPLETE_REQUIRES_CONSENT),
    })
    client2, _ = _make_client_with_transport(transport2)
    task = _send_budget_request(client2, payload)

    result = _extract_result_data(task)
    assert result["decision"] == "needs_consent"
    assert result["provenance"] == "merchant"

    print("PASS: test_requires_consent_from_merchant")


def test_requires_mandate() -> None:
    """Merchant returns 'requires_mandate' (L3 path) → 'needs_mandate'."""
    UPDATE_MANDATE_OK = _merchant_result({
        "id": "co_mandate1", "status": "incomplete",
        "total_cents": 50000, "buyer_consent": True, "currency": "USD",
    })
    transport = MockMerchantTransport({
        "create_checkout": (200, CREATE_MANDATE),
        "update_checkout": (200, UPDATE_MANDATE_OK),
        "complete_checkout": (200, COMPLETE_REQUIRES_MANDATE),
    })
    client, _ = _make_client_with_transport(transport)

    payload = {
        "user_id": "u1",
        "line_items": [
            {"hotel_id": "bali-alaya-ubud", "checkin": "2025-10-01",
             "checkout": "2025-10-03", "adults": 1}
        ],
        "buyer_consent": True,
        "autonomy_level": "L3",
        # No ap2_mandate supplied → merchant returns requires_mandate
    }
    task = _send_budget_request(client, payload)

    print("\n--- needs_mandate task ---")
    print(json.dumps(task, indent=2))
    print("--- end ---\n")

    assert task["status"]["state"] == "input-required"
    result = _extract_result_data(task)
    assert result["decision"] == "needs_mandate"
    assert "mandate_message" in result
    assert result["provenance"] == "merchant"

    print("PASS: test_requires_mandate")


def test_idempotency_key_echo() -> None:
    """Idempotency key supplied by caller is echoed in the result."""
    transport = MockMerchantTransport({
        "create_checkout": (200, CREATE_OK),
        "update_checkout": (200, UPDATE_OK),
        "complete_checkout": (200, COMPLETE_ACCEPT),
    })
    client, _ = _make_client_with_transport(transport)

    idem_key = "trip-" + str(uuid.uuid4())
    payload = {
        "user_id": "u1",
        "line_items": [
            {"hotel_id": "bali-alaya-ubud", "checkin": "2025-10-01",
             "checkout": "2025-10-03", "adults": 1}
        ],
        "buyer_consent": True,
        "idempotency_key": idem_key,
    }
    task = _send_budget_request(client, payload)

    result = _extract_result_data(task)
    assert result["idempotency_key"] == idem_key, (
        f"Expected {idem_key!r}, got {result.get('idempotency_key')!r}"
    )

    print("PASS: test_idempotency_key_echo")


def test_missing_user_id_fails() -> None:
    """Missing user_id in payload causes task to fail."""
    transport = MockMerchantTransport({})
    client, _ = _make_client_with_transport(transport)

    payload = {
        # no user_id
        "line_items": [{"hotel_id": "bali-alaya-ubud", "checkin": "2025-10-01",
                        "checkout": "2025-10-03", "adults": 1}],
    }
    task = _send_budget_request(client, payload)

    assert task["status"]["state"] == "failed", (
        f"Expected failed, got {task['status']['state']!r}"
    )
    error = task.get("metadata", {}).get("error", "")
    assert "user_id" in error.lower(), f"Expected user_id mention in error: {error!r}"

    print("PASS: test_missing_user_id_fails")


def test_empty_line_items_fails() -> None:
    """Empty line_items causes task to fail."""
    transport = MockMerchantTransport({})
    client, _ = _make_client_with_transport(transport)

    payload = {
        "user_id": "u1",
        "line_items": [],
    }
    task = _send_budget_request(client, payload)

    assert task["status"]["state"] == "failed"
    error = task.get("metadata", {}).get("error", "")
    assert "line_items" in error.lower(), f"Expected line_items mention in error: {error!r}"

    print("PASS: test_empty_line_items_fails")


def test_tasks_get() -> None:
    """tasks/get returns the stored task after budget.enforce call."""
    transport = MockMerchantTransport({
        "create_checkout": (200, CREATE_OK),
        "complete_checkout": (200, COMPLETE_ACCEPT),
    })
    client, _ = _make_client_with_transport(transport)

    payload = {
        "user_id": "u1",
        "line_items": [
            {"hotel_id": "bali-alaya-ubud", "checkin": "2025-10-01",
             "checkout": "2025-10-03", "adults": 1}
        ],
        "buyer_consent": True,
    }
    task = _send_budget_request(client, payload)
    task_id = task["id"]

    get_resp = _rpc_post(client, "tasks/get", {"id": task_id})
    assert "error" not in get_resp
    fetched = get_resp["result"]
    assert fetched["id"] == task_id
    assert fetched["status"]["state"] == "completed"
    assert len(fetched["artifacts"]) == 1

    print("PASS: test_tasks_get")


# ---------------------------------------------------------------------------
# D3 hardening tests (gaps #10, #11, #35, #59, #60)
# ---------------------------------------------------------------------------

# Sold-out create_checkout (status "unavailable", HTTP 409 — no id, no total)
CREATE_UNAVAILABLE = _merchant_result({
    "status": "unavailable",
    "reason": "hotel_sold_out",
    "hotel_id": "bali-alaya-ubud",
})


def test_check_sold_out_returns_veto() -> None:
    """
    D3 gap #10 / M2 fix: CHECK phase — create_checkout returns status
    'unavailable' (sold-out). Must return a HONEST merchant-unavailability
    terminal (decision "unavailable"), NEVER check_ok with empty id / total 0,
    and NEVER the generic "veto" (which the orchestrator's price-veto re-plan
    loop would misinterpret as a re-priceable budget overage — M2).
    """
    transport = MockMerchantTransport({
        "create_checkout": (409, CREATE_UNAVAILABLE),
        # complete_checkout must NOT be called on this path
    })
    client, _ = _make_client_with_transport(transport)

    payload = {
        "user_id": "u1",
        "total_budget_cents": 80000,
        "line_items": [
            {"hotel_id": "bali-alaya-ubud", "checkin": "2025-10-01",
             "checkout": "2025-10-03", "adults": 1}
        ],
    }
    task = _send_budget_request(client, payload, skill_id="budget.check")

    assert task["status"]["state"] == "completed", (
        f"Expected completed, got {task['status']['state']!r}"
    )
    result = _extract_result_data(task)
    # M2: must be the honest merchant-unavailable terminal, NOT a price veto
    # (a price veto is re-plannable; a hard merchant failure is not).
    assert result["decision"] == "unavailable", (
        f"Sold-out must return decision='unavailable' (M2 fix), got {result['decision']!r}"
    )
    # checkout_id must NOT be a real id (empty — merchant gave none)
    assert result.get("checkout_id", "") == "", (
        f"checkout_id should be empty for unavailable, got {result.get('checkout_id')!r}"
    )
    # total_cents must not be a positive number (nothing was priced)
    assert result.get("total_cents", 0) == 0, (
        f"total_cents should be 0 for unavailable, got {result.get('total_cents')!r}"
    )
    assert result.get("veto_reason") in ("hotel_sold_out", "unavailable", "create_failed"), (
        f"Unexpected veto_reason: {result.get('veto_reason')!r}"
    )
    print("PASS: test_check_sold_out_returns_veto")


def test_check_create_failed_returns_cannot_price() -> None:
    """
    M2: CHECK phase — create_checkout returns a generic hard failure status
    (not the specific 'unavailable' status, e.g. 'error') with no id/total.
    Must return decision "cannot_price" (the OTHER honest-terminal decision
    the orchestrator's D3 branch checks for), not "veto" and not "unavailable".
    """
    CREATE_ERROR = _merchant_result({
        "status": "error",
        "reason": "merchant_internal_error",
    })
    transport = MockMerchantTransport({
        "create_checkout": (500, CREATE_ERROR),
    })
    client, _ = _make_client_with_transport(transport)

    payload = {
        "user_id": "u1",
        "total_budget_cents": 80000,
        "line_items": [
            {"hotel_id": "bali-alaya-ubud", "checkin": "2025-10-01",
             "checkout": "2025-10-03", "adults": 1}
        ],
    }
    task = _send_budget_request(client, payload, skill_id="budget.check")

    assert task["status"]["state"] == "completed"
    result = _extract_result_data(task)
    assert result["decision"] == "cannot_price", (
        f"Generic create_checkout hard failure must return 'cannot_price', "
        f"got {result['decision']!r}"
    )
    assert result.get("total_cents", 0) == 0
    print("PASS: test_check_create_failed_returns_cannot_price")


def test_check_no_budget_supplied_returns_veto() -> None:
    """
    D3 gap #11: CHECK phase — total_budget_cents absent/zero.
    Budget gate cannot run → conservative veto (never check_ok).
    """
    transport = MockMerchantTransport({
        # create_checkout should NOT be called when budget is absent
    })
    client, _ = _make_client_with_transport(transport)

    for missing_budget_payload in [
        # Case 1: total_budget_cents not supplied
        {
            "user_id": "u1",
            "line_items": [
                {"hotel_id": "bali-alaya-ubud", "checkin": "2025-10-01",
                 "checkout": "2025-10-03", "adults": 1}
            ],
        },
        # Case 2: total_budget_cents explicitly 0
        {
            "user_id": "u1",
            "total_budget_cents": 0,
            "line_items": [
                {"hotel_id": "bali-alaya-ubud", "checkin": "2025-10-01",
                 "checkout": "2025-10-03", "adults": 1}
            ],
        },
    ]:
        task = _send_budget_request(client, missing_budget_payload, skill_id="budget.check")
        result = _extract_result_data(task)
        assert result["decision"] == "veto", (
            f"Absent/zero budget must veto conservatively, got {result['decision']!r} "
            f"for payload {missing_budget_payload}"
        )
        assert result.get("veto_reason") == "no_budget_supplied", (
            f"Expected no_budget_supplied, got {result.get('veto_reason')!r}"
        )

    print("PASS: test_check_no_budget_supplied_returns_veto")


def test_legacy_enforce_sold_out_returns_veto() -> None:
    """
    D3 gap #35: Legacy budget.enforce — create_checkout returns 'unavailable'.
    Must return veto (availability failure), NOT needs_consent with empty id / total 0.
    """
    transport = MockMerchantTransport({
        "create_checkout": (409, CREATE_UNAVAILABLE),
        # complete_checkout must NOT be called
    })
    client, _ = _make_client_with_transport(transport)

    payload = {
        "user_id": "u1",
        "line_items": [
            {"hotel_id": "bali-alaya-ubud", "checkin": "2025-10-01",
             "checkout": "2025-10-03", "adults": 1}
        ],
        "buyer_consent": True,
    }
    task = _send_budget_request(client, payload, skill_id="budget.enforce")

    assert task["status"]["state"] == "completed"
    result = _extract_result_data(task)
    # Must be a veto, NOT needs_consent or accept
    assert result["decision"] == "veto", (
        f"Sold-out (legacy) must return veto, got {result['decision']!r}"
    )
    assert result.get("checkout_id", "") == "", (
        f"checkout_id should be empty for unavailable, got {result.get('checkout_id')!r}"
    )
    assert result.get("total_cents", 0) == 0
    print("PASS: test_legacy_enforce_sold_out_returns_veto")


def test_commit_needs_consent_real_total() -> None:
    """
    D3 gap #59: budget.commit — when no consent/mandate, must NOT return
    needs_consent with total_cents=0. The merchant returns requires_consent
    with the real session total, which must appear in the result.
    """
    # Merchant returns requires_consent with real total
    COMPLETE_REQUIRES_CONSENT_REAL = _merchant_result({
        "status": "requires_consent",
        "id": "co_abc123",
        "total_cents": 33000,
        "message": "buyer_consent required before complete_checkout (HITL gate)",
    })

    transport = MockMerchantTransport({
        "complete_checkout": (200, COMPLETE_REQUIRES_CONSENT_REAL),
    })
    client, _ = _make_client_with_transport(transport)

    payload = {
        "user_id": "u1",
        "checkout_id": "co_abc123",
        # no buyer_consent — should trigger requires_consent from merchant
    }
    task = _send_budget_request(client, payload, skill_id="budget.commit")

    # Task state should be input-required (A2A signal for HITL)
    assert task["status"]["state"] == "input-required", (
        f"Expected input-required, got {task['status']['state']!r}"
    )
    result = _extract_result_data(task)
    assert result["decision"] == "needs_consent", (
        f"Expected needs_consent, got {result['decision']!r}"
    )
    # total_cents must be the real total from the merchant, NOT 0
    assert result["total_cents"] == 33000, (
        f"Expected real total 33000, got {result['total_cents']!r} "
        "(must not emit misleading 0 as placeholder)"
    )
    assert result["provenance"] == "merchant"
    print("PASS: test_commit_needs_consent_real_total")


def test_legacy_enforce_forwards_user_budget_cents() -> None:
    """
    D3 gap #60: Legacy budget.enforce — total_budget_cents must be forwarded
    as user_budget_cents to create_checkout (mirrors SEV-1b fix from _check_merchant).
    Verify end-to-end that a legacy call with total_budget_cents produces accept.
    """
    transport = MockMerchantTransport({
        "create_checkout": (200, CREATE_OK),
        "complete_checkout": (200, COMPLETE_ACCEPT),
    })
    client, _ = _make_client_with_transport(transport)

    payload = {
        "user_id": "u1",
        "total_budget_cents": 80000,  # user budget must be forwarded
        "line_items": [
            {"hotel_id": "bali-alaya-ubud", "checkin": "2025-10-01",
             "checkout": "2025-10-03", "adults": 1}
        ],
        "buyer_consent": True,
    }
    task = _send_budget_request(client, payload, skill_id="budget.enforce")

    assert task["status"]["state"] == "completed"
    result = _extract_result_data(task)
    assert result["decision"] == "accept", (
        f"Expected accept, got {result['decision']!r}"
    )
    assert result["total_cents"] == 33000
    assert result["booking_ref"] == "BK-deadbeef"
    print("PASS: test_legacy_enforce_forwards_user_budget_cents")


# ---------------------------------------------------------------------------
# MED fix tests: budget.commit consent path surfaces real total
# ---------------------------------------------------------------------------

def test_commit_needs_consent_total_from_session_fallback() -> None:
    """
    MED fix: budget.commit — when requires_consent response omits total_cents,
    the orchestrator-supplied total_cents (from prior budget.check) must be used
    as the fallback, NOT 0.

    This tests the new 'total_cents' field in the budget.commit payload:
    the orchestrator threads the session total from budget.check so the
    consent surface always shows the real amount.
    """
    # Merchant returns requires_consent WITHOUT total_cents in the response
    COMPLETE_REQUIRES_CONSENT_NO_TOTAL = _merchant_result({
        "status": "requires_consent",
        "id": "co_abc123",
        # total_cents deliberately absent — simulates merchant omitting it
        "message": "buyer_consent required before complete_checkout (HITL gate)",
    })

    transport = MockMerchantTransport({
        "complete_checkout": (200, COMPLETE_REQUIRES_CONSENT_NO_TOTAL),
    })
    client, _ = _make_client_with_transport(transport)

    payload = {
        "user_id": "u1",
        "checkout_id": "co_abc123",
        "total_cents": 33000,  # orchestrator threads in the session total from budget.check
        # no buyer_consent — triggers requires_consent from merchant
    }
    task = _send_budget_request(client, payload, skill_id="budget.commit")

    assert task["status"]["state"] == "input-required", (
        f"Expected input-required, got {task['status']['state']!r}"
    )
    result = _extract_result_data(task)
    assert result["decision"] == "needs_consent", (
        f"Expected needs_consent, got {result['decision']!r}"
    )
    # total_cents must be the session total threaded by the orchestrator, NOT 0
    assert result["total_cents"] == 33000, (
        f"Expected session total 33000 from orchestrator fallback, got {result['total_cents']!r} "
        "(MED fix: consent surface must show real amount even when commit response omits total)"
    )
    assert result["provenance"] == "merchant"
    print("PASS: test_commit_needs_consent_total_from_session_fallback")


def test_commit_needs_consent_zero_without_session_total() -> None:
    """
    MED fix (negative case): budget.commit — when requires_consent response omits
    total_cents AND the orchestrator does NOT supply total_cents in the payload,
    the result total_cents will be 0 (no fallback available).  This is expected
    behaviour — we cannot fabricate a total we do not have.
    """
    COMPLETE_REQUIRES_CONSENT_NO_TOTAL = _merchant_result({
        "status": "requires_consent",
        "id": "co_abc123",
        "message": "buyer_consent required before complete_checkout (HITL gate)",
    })

    transport = MockMerchantTransport({
        "complete_checkout": (200, COMPLETE_REQUIRES_CONSENT_NO_TOTAL),
    })
    client, _ = _make_client_with_transport(transport)

    payload = {
        "user_id": "u1",
        "checkout_id": "co_abc123",
        # no total_cents supplied — orchestrator did not provide session total
    }
    task = _send_budget_request(client, payload, skill_id="budget.commit")

    assert task["status"]["state"] == "input-required"
    result = _extract_result_data(task)
    assert result["decision"] == "needs_consent"
    # Without session total from orchestrator, total_cents will be 0
    assert result["total_cents"] == 0, (
        f"Without orchestrator-supplied total, expected 0, got {result['total_cents']!r}"
    )
    print("PASS: test_commit_needs_consent_zero_without_session_total")


# ---------------------------------------------------------------------------
# LOW fix tests: accept branch tightened to "complete" only
# ---------------------------------------------------------------------------

def test_commit_incomplete_status_raises_error() -> None:
    """
    LOW fix: budget.commit — complete_checkout returning 'incomplete' must NOT
    be treated as accept (decision 'accept').  It should raise an error (task
    fails), because complete_checkout only returns 'complete' on a committed booking.
    An 'incomplete' at this stage means the booking was NOT committed.
    """
    COMPLETE_RETURNS_INCOMPLETE = _merchant_result({
        "id": "co_abc123",
        "status": "incomplete",  # unexpected from complete_checkout
        "total_cents": 33000,
        "currency": "USD",
        "buyer_consent": False,
        "booking_ref": "",
    })

    transport = MockMerchantTransport({
        "complete_checkout": (200, COMPLETE_RETURNS_INCOMPLETE),
    })
    client, _ = _make_client_with_transport(transport)

    payload = {
        "user_id": "u1",
        "checkout_id": "co_abc123",
        "buyer_consent": True,
    }
    task = _send_budget_request(client, payload, skill_id="budget.commit")

    # Must NOT accept — task should fail because "incomplete" from complete_checkout
    # is an unexpected/error status; the booking was NOT committed.
    assert task["status"]["state"] == "failed", (
        f"Expected failed for incomplete complete_checkout, got {task['status']['state']!r} "
        "(LOW fix: incomplete must not be treated as accept)"
    )
    print("PASS: test_commit_incomplete_status_raises_error")


def test_enforce_incomplete_status_raises_error() -> None:
    """
    LOW fix (legacy path): budget.enforce — complete_checkout returning 'incomplete'
    must NOT be treated as accept.  Same tightening as budget.commit.
    """
    COMPLETE_RETURNS_INCOMPLETE = _merchant_result({
        "id": "co_abc123",
        "status": "incomplete",
        "total_cents": 33000,
        "currency": "USD",
        "buyer_consent": False,
        "booking_ref": "",
    })

    transport = MockMerchantTransport({
        "create_checkout": (200, CREATE_OK),
        "complete_checkout": (200, COMPLETE_RETURNS_INCOMPLETE),
    })
    client, _ = _make_client_with_transport(transport)

    payload = {
        "user_id": "u1",
        "line_items": [
            {"hotel_id": "bali-alaya-ubud", "checkin": "2025-10-01",
             "checkout": "2025-10-03", "adults": 1}
        ],
        "buyer_consent": True,
    }
    task = _send_budget_request(client, payload, skill_id="budget.enforce")

    # Must NOT accept
    assert task["status"]["state"] == "failed", (
        f"Expected failed for incomplete complete_checkout (legacy), "
        f"got {task['status']['state']!r} "
        "(LOW fix: incomplete must not be treated as accept)"
    )
    print("PASS: test_enforce_incomplete_status_raises_error")


# ---------------------------------------------------------------------------
# Test runner
# ---------------------------------------------------------------------------

TESTS = [
    test_agent_card,
    test_within_budget_accept,
    test_over_budget_veto,
    test_hard_max_veto,
    test_no_consent_needs_consent,
    test_requires_consent_from_merchant,
    test_requires_mandate,
    test_idempotency_key_echo,
    test_missing_user_id_fails,
    test_empty_line_items_fails,
    test_tasks_get,
    # D3 hardening tests
    test_check_sold_out_returns_veto,
    test_check_create_failed_returns_cannot_price,
    test_check_no_budget_supplied_returns_veto,
    test_legacy_enforce_sold_out_returns_veto,
    test_commit_needs_consent_real_total,
    test_legacy_enforce_forwards_user_budget_cents,
    # MED fix tests: budget.commit consent path surfaces real total
    test_commit_needs_consent_total_from_session_fallback,
    test_commit_needs_consent_zero_without_session_total,
    # LOW fix tests: accept branch tightened to "complete" only
    test_commit_incomplete_status_raises_error,
    test_enforce_incomplete_status_raises_error,
]


def main() -> None:
    passed = 0
    failed = 0
    errors: list[tuple[str, Exception]] = []

    print(f"\n{'='*60}")
    print("Travel Guild — Budget Agent Unit Test Suite")
    print(f"{'='*60}\n")

    for test_fn in TESTS:
        name = test_fn.__name__
        try:
            test_fn()
            passed += 1
        except Exception as exc:
            failed += 1
            errors.append((name, exc))
            print(f"FAIL: {name} — {exc}")
            import traceback
            traceback.print_exc()

    print(f"\n{'='*60}")
    print(f"Results: {passed} passed, {failed} failed out of {len(TESTS)} tests")
    print(f"{'='*60}\n")

    if errors:
        sys.exit(1)


if __name__ == "__main__":
    main()
