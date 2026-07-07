"""
test_a2a.py — In-process A2A roundtrip tests (no real port/network).

Uses Starlette's TestClient (ASGI transport) so these run without binding
any port and complete in milliseconds.

Coverage:
  1. Agent Card served at /.well-known/agent-card.json + required fields
  2. Legacy card path /.well-known/agent.json (v0.2 compat alias)
  3. Client→echo roundtrip: Task reaches 'completed'; artifact contains echoed text
  4. tasks/get returns the stored task
  5. Unknown method → JSON-RPC METHOD_NOT_FOUND error object
  6. Unknown skill via metadata.skillId → 'rejected' task (deliberate refusal, not crash)
  7. Missing/malformed message.parts → JSON-RPC INVALID_PARAMS error
  8. message/send with contextId is preserved in Task
  9. Internal skill crash → 'failed' task (unexpected error, not rejection)
 10. outcome metadata: 'rejected' refusals carry outcome='rejected'; crashes carry outcome='error'
"""

from __future__ import annotations

import json
import sys
import os
import uuid

# Ensure we can import from the society/ directory
sys.path.insert(0, os.path.dirname(__file__))

from starlette.testclient import TestClient

from agents.echo_agent import EchoAgent
from agents.a2a_agent import A2AAgent, SkillRejected, _text_part, _new_artifact
from orchestration.a2a_client import (
    TERMINAL_STATES,
    _build_message,
    _extract_result_text,
    _build_rpc_request,
)


# ---------------------------------------------------------------------------
# Shared fixture
# ---------------------------------------------------------------------------

def make_client() -> tuple[TestClient, EchoAgent]:
    agent = EchoAgent(host="0.0.0.0", port=9100)
    app = agent.build_app()
    return TestClient(app, raise_server_exceptions=True), agent


def rpc_post(client: TestClient, method: str, params: dict) -> dict:
    """POST a JSON-RPC 2.0 request; return the full JSON response dict."""
    body = _build_rpc_request(method, params)
    resp = client.post("/", json=body)
    assert resp.status_code == 200, f"HTTP {resp.status_code}: {resp.text}"
    return resp.json()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_agent_card_primary_path() -> None:
    """Agent Card served at /.well-known/agent-card.json with required fields."""
    client, _ = make_client()
    resp = client.get("/.well-known/agent-card.json")
    assert resp.status_code == 200, f"Expected 200, got {resp.status_code}"
    card = resp.json()

    print("\n--- Agent Card JSON ---")
    print(json.dumps(card, indent=2))
    print("--- end Agent Card ---\n")

    # Required fields per A2A v0.3 spec (camelCase wire format)
    required_fields = {"name", "description", "url", "version", "capabilities", "skills"}
    missing = required_fields - card.keys()
    assert not missing, f"Agent Card missing required fields: {missing}"

    assert card["name"] == "echo-agent"
    assert "capabilities" in card
    assert isinstance(card["skills"], list)
    assert len(card["skills"]) >= 1

    skill = card["skills"][0]
    assert skill["id"] == "echo"
    assert "name" in skill
    assert "description" in skill
    assert "tags" in skill

    # Protocol metadata
    assert card.get("protocolVersion") == "0.3.0"
    assert card.get("preferredTransport") == "JSONRPC"

    print("PASS: test_agent_card_primary_path")


def test_agent_card_legacy_path() -> None:
    """Legacy card path /.well-known/agent.json (v0.2 backward compat)."""
    client, _ = make_client()
    resp = client.get("/.well-known/agent.json")
    assert resp.status_code == 200, f"Expected 200, got {resp.status_code}"
    card = resp.json()
    assert card["name"] == "echo-agent"
    print("PASS: test_agent_card_legacy_path")


def test_echo_roundtrip() -> None:
    """
    Full client→echo roundtrip:
      - message/send creates a Task
      - Task transitions submitted→working→completed
      - Artifact contains the echoed text
      - history has both the user message and agent reply
    """
    client, _ = make_client()
    input_text = "6 days Bali, ~$1500, solo, beaches + culture"
    context_id = str(uuid.uuid4())

    message = _build_message(input_text, context_id=context_id)
    resp = rpc_post(client, "message/send", {"message": message})

    print("\n--- Echo roundtrip RPC response ---")
    print(json.dumps(resp, indent=2))
    print("--- end ---\n")

    assert "error" not in resp, f"Unexpected RPC error: {resp.get('error')}"
    task = resp["result"]

    # Task structure
    assert task["kind"] == "task"
    assert "id" in task
    assert task["contextId"] == context_id

    # Terminal state
    state = task["status"]["state"]
    assert state == "completed", f"Expected 'completed', got {state!r}"
    assert "timestamp" in task["status"]

    # Artifact: echoed text
    assert len(task["artifacts"]) == 1, "Expected exactly 1 artifact"
    artifact = task["artifacts"][0]
    assert "artifactId" in artifact
    assert artifact["name"] == "echo"
    assert len(artifact["parts"]) == 1
    part = artifact["parts"][0]
    assert part["kind"] == "text"
    assert part["text"] == input_text, (
        f"Echo mismatch: expected {input_text!r}, got {part['text']!r}"
    )

    # History: user message + agent reply
    assert len(task["history"]) >= 2, "Expected ≥2 history entries"
    roles = [m.get("role") for m in task["history"]]
    assert "user" in roles
    assert "agent" in roles

    # Test _extract_result_text helper
    result_text = _extract_result_text(task)
    assert result_text == input_text

    print("PASS: test_echo_roundtrip")
    return task  # returned for tasks/get test


def test_tasks_get() -> None:
    """tasks/get returns the stored task by id."""
    client, _ = make_client()
    message = _build_message("hello tasks/get")
    send_resp = rpc_post(client, "message/send", {"message": message})
    task_id = send_resp["result"]["id"]

    get_resp = rpc_post(client, "tasks/get", {"id": task_id})

    print("\n--- tasks/get response ---")
    print(json.dumps(get_resp, indent=2))
    print("--- end ---\n")

    assert "error" not in get_resp, f"Unexpected error: {get_resp.get('error')}"
    fetched = get_resp["result"]
    assert fetched["id"] == task_id
    assert fetched["status"]["state"] == "completed"
    assert len(fetched["artifacts"]) == 1
    assert fetched["artifacts"][0]["parts"][0]["text"] == "hello tasks/get"

    print("PASS: test_tasks_get")


def test_tasks_get_missing() -> None:
    """tasks/get returns a JSON-RPC error for an unknown task id."""
    client, _ = make_client()
    get_resp = rpc_post(client, "tasks/get", {"id": "nonexistent-task-id"})

    assert "error" in get_resp, "Expected RPC error for unknown task"
    err = get_resp["error"]
    assert "code" in err
    assert "message" in err
    # INVALID_PARAMS (-32602) per our implementation
    assert err["code"] == -32602, f"Expected -32602, got {err['code']}"

    print("PASS: test_tasks_get_missing")


def test_unknown_method() -> None:
    """Unknown JSON-RPC method returns METHOD_NOT_FOUND error."""
    client, _ = make_client()
    body = {
        "jsonrpc": "2.0",
        "id": "test-unknown",
        "method": "bogus/method",
        "params": {},
    }
    resp = client.post("/", json=body)
    assert resp.status_code == 200
    result = resp.json()

    print("\n--- unknown method response ---")
    print(json.dumps(result, indent=2))
    print("--- end ---\n")

    assert "error" in result, "Expected RPC error for unknown method"
    err = result["error"]
    assert err["code"] == -32601, (
        f"Expected METHOD_NOT_FOUND (-32601), got {err['code']}"
    )
    assert "bogus/method" in err["message"] or "not found" in err["message"].lower()

    print("PASS: test_unknown_method")


def test_invalid_params_missing_parts() -> None:
    """message/send with missing parts returns INVALID_PARAMS error."""
    client, _ = make_client()
    resp = rpc_post(client, "message/send", {
        "message": {
            "kind": "message",
            "messageId": str(uuid.uuid4()),
            "role": "user",
            "parts": [],   # empty parts
        }
    })

    assert "error" in resp, "Expected RPC error for empty parts"
    err = resp["error"]
    assert err["code"] == -32602, f"Expected INVALID_PARAMS (-32602), got {err['code']}"

    print("PASS: test_invalid_params_missing_parts")


def test_invalid_params_missing_message() -> None:
    """message/send with no message key returns INVALID_PARAMS error."""
    client, _ = make_client()
    resp = rpc_post(client, "message/send", {})

    assert "error" in resp, "Expected RPC error for missing message"
    err = resp["error"]
    assert err["code"] == -32602

    print("PASS: test_invalid_params_missing_message")


def test_context_id_preserved() -> None:
    """contextId supplied in the message is preserved in the returned Task."""
    client, _ = make_client()
    context_id = "trip-" + str(uuid.uuid4())
    message = _build_message("context test", context_id=context_id)
    resp = rpc_post(client, "message/send", {"message": message})

    assert "error" not in resp
    task = resp["result"]
    assert task["contextId"] == context_id, (
        f"contextId mismatch: expected {context_id!r}, got {task.get('contextId')!r}"
    )

    print("PASS: test_context_id_preserved")


def test_jsonrpc_version_validation() -> None:
    """Requests with wrong jsonrpc version return INVALID_REQUEST error."""
    client, _ = make_client()
    body = {
        "jsonrpc": "1.0",
        "id": "ver-test",
        "method": "message/send",
        "params": {"message": _build_message("hi")},
    }
    resp = client.post("/", json=body)
    result = resp.json()
    assert "error" in result
    err = result["error"]
    assert err["code"] == -32600  # INVALID_REQUEST

    print("PASS: test_jsonrpc_version_validation")


def test_unknown_skill_id_single_skill_agent_fails_closed() -> None:
    """
    Unknown skillId on a single-skill agent must produce a 'rejected' task,
    NOT silently dispatch to the only registered skill, and NOT produce 'failed'.

    Documented contract (test_a2a.py module docstring item 6):
      'Unknown skill via metadata.skillId → rejected task (deliberate refusal, not crash)'

    The distinction matters for consumers: 'rejected' signals a deliberate
    fail-closed policy decision; 'failed' signals an unexpected internal crash.
    Consumers can use 'rejected' to show a user-friendly "not supported" message
    rather than a generic "something went wrong" error.

    Gap #22 (audit): _dispatch was checking skill_id in self._skills and, only
    on mismatch, falling through to the single-skill fallback.  A caller with a
    typo or stale skill name was silently answered by the wrong skill.  The fix
    raises SkillRejected immediately when skill_id is non-empty but not registered.
    """
    client, _ = make_client()
    message = _build_message("hello")
    # Inject a skillId that does NOT exist on the echo agent
    message["metadata"] = {"skillId": "nonexistent-skill-xyz"}

    resp = rpc_post(client, "message/send", {"message": message})

    print("\n--- unknown skillId (single-skill agent) response ---")
    print(json.dumps(resp, indent=2))
    print("--- end ---\n")

    # The RPC envelope itself is well-formed (no RPC-level error), but the
    # Task should have transitioned to 'rejected' (deliberate refusal) and
    # carry an error in metadata.
    assert "error" not in resp, (
        f"Expected a Task result (not RPC error), got: {resp.get('error')}"
    )
    task = resp["result"]
    state = task["status"]["state"]
    assert state == "rejected", (
        f"Expected task state 'rejected' for unknown skillId, got {state!r}. "
        "An unknown skillId is a deliberate dispatch refusal, not an internal crash. "
        "Use state 'rejected' so consumers can distinguish decline from crash."
    )
    # The error detail should mention the unknown skill name
    error_detail = (task.get("metadata") or {}).get("error", "")
    assert "nonexistent-skill-xyz" in error_detail, (
        f"Expected error to mention the unknown skill id, got: {error_detail!r}"
    )
    # outcome metadata must be 'rejected' for deliberate refusals
    outcome = (task.get("metadata") or {}).get("outcome", "")
    assert outcome == "rejected", (
        f"Expected metadata.outcome='rejected' for deliberate dispatch refusal, got {outcome!r}"
    )

    print("PASS: test_unknown_skill_id_single_skill_agent_fails_closed")


def test_valid_skill_id_on_single_skill_agent_dispatches() -> None:
    """
    A correct skillId on a single-skill agent must still succeed normally.
    Ensures the fail-closed guard does not break valid skill routing.
    """
    client, _ = make_client()
    input_text = "Bali 5 nights $1200"
    message = _build_message(input_text)
    message["metadata"] = {"skillId": "echo"}  # the registered skill

    resp = rpc_post(client, "message/send", {"message": message})

    assert "error" not in resp, f"Unexpected RPC error: {resp.get('error')}"
    task = resp["result"]
    assert task["status"]["state"] == "completed", (
        f"Expected completed, got {task['status']['state']!r}"
    )
    artifact = task["artifacts"][0]
    assert artifact["parts"][0]["text"] == input_text

    print("PASS: test_valid_skill_id_on_single_skill_agent_dispatches")


# ---------------------------------------------------------------------------
# Helpers for the crash / rejection distinction tests
# ---------------------------------------------------------------------------

class _CrashingAgent(A2AAgent):
    """A minimal agent whose single skill always raises an unexpected RuntimeError."""

    def _build_card(self) -> dict:
        return {
            "name": "crashing-agent",
            "description": "Test agent that always crashes internally.",
            "url": "http://0.0.0.0:9199",
            "version": "1.0.0",
            "protocolVersion": "0.3.0",
            "preferredTransport": "JSONRPC",
            "capabilities": {},
            "skills": [{"id": "crash", "name": "Crash", "description": "Always crashes",
                         "tags": []}],
        }

    def _register_skills(self) -> None:
        self.register_skill("crash", self._crash_handler)

    async def _crash_handler(self, message: dict, task: dict) -> dict:
        raise RuntimeError("Simulated unexpected internal failure")


class _RejectingAgent(A2AAgent):
    """A minimal agent whose single skill deliberately raises SkillRejected."""

    def _build_card(self) -> dict:
        return {
            "name": "rejecting-agent",
            "description": "Test agent that deliberately rejects requests.",
            "url": "http://0.0.0.0:9198",
            "version": "1.0.0",
            "protocolVersion": "0.3.0",
            "preferredTransport": "JSONRPC",
            "capabilities": {},
            "skills": [{"id": "reject", "name": "Reject", "description": "Always rejects",
                         "tags": []}],
        }

    def _register_skills(self) -> None:
        self.register_skill("reject", self._reject_handler)

    async def _reject_handler(self, message: dict, task: dict) -> dict:
        raise SkillRejected("Policy: this request type is not permitted")


def test_internal_crash_produces_failed_state() -> None:
    """
    An unexpected RuntimeError inside a skill handler must produce a 'failed' task.

    This is coverage for the other side of the error/rejected distinction:
    - SkillRejected (deliberate policy refusal) → 'rejected'
    - Any other exception (unexpected crash)    → 'failed'

    Consumers seeing 'failed' know something crashed; they can retry or escalate.
    Consumers seeing 'rejected' know the agent deliberately declined (no retry needed).
    """
    agent = _CrashingAgent()
    app = agent.build_app()
    client = TestClient(app, raise_server_exceptions=False)

    message = _build_message("trigger the crash")
    resp = rpc_post(client, "message/send", {"message": message})

    print("\n--- internal crash response ---")
    print(json.dumps(resp, indent=2))
    print("--- end ---\n")

    assert "error" not in resp, (
        f"Expected a Task result (not RPC error), got: {resp.get('error')}"
    )
    task = resp["result"]
    state = task["status"]["state"]
    assert state == "failed", (
        f"Expected task state 'failed' for internal crash, got {state!r}. "
        "Unexpected exceptions must produce 'failed', not 'rejected'."
    )
    error_detail = (task.get("metadata") or {}).get("error", "")
    assert "Simulated unexpected internal failure" in error_detail, (
        f"Expected crash message in metadata.error, got: {error_detail!r}"
    )
    outcome = (task.get("metadata") or {}).get("outcome", "")
    assert outcome == "error", (
        f"Expected metadata.outcome='error' for internal crash, got {outcome!r}"
    )

    print("PASS: test_internal_crash_produces_failed_state")


def test_skill_rejected_from_handler_produces_rejected_state() -> None:
    """
    A SkillRejected raised inside a skill handler must produce a 'rejected' task.

    This tests the case where the skill itself (not just dispatch routing)
    decides to deliberately refuse a request.  Any skill handler can raise
    SkillRejected to signal a policy-level decline.
    """
    agent = _RejectingAgent()
    app = agent.build_app()
    client = TestClient(app, raise_server_exceptions=True)

    message = _build_message("trigger the rejection")
    resp = rpc_post(client, "message/send", {"message": message})

    print("\n--- skill handler rejection response ---")
    print(json.dumps(resp, indent=2))
    print("--- end ---\n")

    assert "error" not in resp, (
        f"Expected a Task result (not RPC error), got: {resp.get('error')}"
    )
    task = resp["result"]
    state = task["status"]["state"]
    assert state == "rejected", (
        f"Expected task state 'rejected' for SkillRejected, got {state!r}. "
        "SkillRejected from a skill handler must produce 'rejected', not 'failed'."
    )
    error_detail = (task.get("metadata") or {}).get("error", "")
    assert "Policy" in error_detail or "not permitted" in error_detail, (
        f"Expected rejection reason in metadata.error, got: {error_detail!r}"
    )
    outcome = (task.get("metadata") or {}).get("outcome", "")
    assert outcome == "rejected", (
        f"Expected metadata.outcome='rejected' for deliberate refusal, got {outcome!r}"
    )

    print("PASS: test_skill_rejected_from_handler_produces_rejected_state")


# ---------------------------------------------------------------------------
# Test runner
# ---------------------------------------------------------------------------

TESTS = [
    test_agent_card_primary_path,
    test_agent_card_legacy_path,
    test_echo_roundtrip,
    test_tasks_get,
    test_tasks_get_missing,
    test_unknown_method,
    test_invalid_params_missing_parts,
    test_invalid_params_missing_message,
    test_context_id_preserved,
    test_jsonrpc_version_validation,
    test_unknown_skill_id_single_skill_agent_fails_closed,
    test_valid_skill_id_on_single_skill_agent_dispatches,
    test_internal_crash_produces_failed_state,
    test_skill_rejected_from_handler_produces_rejected_state,
]


def main() -> None:
    passed = 0
    failed = 0
    errors: list[tuple[str, Exception]] = []

    print(f"\n{'='*60}")
    print("Travel Guild — A2A M0 Test Suite")
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
