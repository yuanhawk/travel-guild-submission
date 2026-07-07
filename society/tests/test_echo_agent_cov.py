"""
test_echo_agent_cov.py — Comprehensive test coverage for echo_agent.py.

This module tests the EchoAgent implementation directly:
  1. Constructor: host/port parameters and defaults
  2. Card building: required fields, metadata structure, skill definitions
  3. Echo handler: text extraction, non-text parts, empty parts, edge cases
  4. Roundtrip behavior: full A2A message flow with TestClient
  5. Deterministic behaviors (no LLM, no network, no live APIs)

Uses Starlette TestClient for in-process ASGI transport (no actual HTTP binding).
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
from agents.a2a_agent import _text_part, _new_artifact
from orchestration.a2a_client import _build_message, _build_rpc_request


# ============================================================================
# Fixtures
# ============================================================================

def make_echo_client(host: str = "0.0.0.0", port: int = 9100) -> tuple[TestClient, EchoAgent]:
    """Create an EchoAgent instance and TestClient for in-process testing."""
    agent = EchoAgent(host=host, port=port)
    app = agent.build_app()
    return TestClient(app, raise_server_exceptions=True), agent


def rpc_post(client: TestClient, method: str, params: dict) -> dict:
    """POST a JSON-RPC 2.0 request; return the response dict."""
    body = _build_rpc_request(method, params)
    resp = client.post("/", json=body)
    assert resp.status_code == 200, f"HTTP {resp.status_code}: {resp.text}"
    return resp.json()


# ============================================================================
# Constructor Tests
# ============================================================================

class TestEchoAgentConstructor:
    """Test EchoAgent.__init__() with custom and default parameters."""

    def test_constructor_default_params(self) -> None:
        """EchoAgent() with no args uses defaults: host='0.0.0.0', port=9100."""
        agent = EchoAgent()
        assert agent._host == "0.0.0.0"
        assert agent._port == 9100
        print("PASS: test_constructor_default_params")

    def test_constructor_custom_host(self) -> None:
        """EchoAgent(host='127.0.0.1') sets custom host."""
        agent = EchoAgent(host="127.0.0.1")
        assert agent._host == "127.0.0.1"
        assert agent._port == 9100  # default port
        print("PASS: test_constructor_custom_host")

    def test_constructor_custom_port(self) -> None:
        """EchoAgent(port=8080) sets custom port."""
        agent = EchoAgent(port=8080)
        assert agent._host == "0.0.0.0"  # default host
        assert agent._port == 8080
        print("PASS: test_constructor_custom_port")

    def test_constructor_custom_host_and_port(self) -> None:
        """EchoAgent(host='localhost', port=5000) sets both."""
        agent = EchoAgent(host="localhost", port=5000)
        assert agent._host == "localhost"
        assert agent._port == 5000
        print("PASS: test_constructor_custom_host_and_port")


# ============================================================================
# Card Building Tests
# ============================================================================

class TestEchoAgentCardBuilding:
    """Test EchoAgent._build_card() and card metadata."""

    def test_card_required_fields(self) -> None:
        """Agent card contains all required A2A v0.3 fields."""
        agent = EchoAgent()
        card = agent._build_card()

        required = {"name", "description", "url", "version", "capabilities",
                    "defaultInputModes", "defaultOutputModes", "skills",
                    "protocolVersion", "preferredTransport"}
        missing = required - set(card.keys())
        assert not missing, f"Missing required fields: {missing}"
        print("PASS: test_card_required_fields")

    def test_card_name_and_description(self) -> None:
        """Agent card has correct name and non-empty description."""
        agent = EchoAgent()
        card = agent._build_card()

        assert card["name"] == "echo-agent"
        assert isinstance(card["description"], str)
        assert len(card["description"]) > 0
        assert "echo" in card["description"].lower()
        print("PASS: test_card_name_and_description")

    def test_card_url_includes_host_port(self) -> None:
        """Agent card URL is built from host and port."""
        agent = EchoAgent(host="localhost", port=9101)
        card = agent._build_card()

        assert card["url"] == "http://localhost:9101"
        print("PASS: test_card_url_includes_host_port")

    def test_card_version(self) -> None:
        """Agent card version is semantic."""
        agent = EchoAgent()
        card = agent._build_card()

        assert card["version"] == "1.0.0"
        print("PASS: test_card_version")

    def test_card_protocol_version(self) -> None:
        """Agent card declares A2A protocol version 0.3.0."""
        agent = EchoAgent()
        card = agent._build_card()

        assert card["protocolVersion"] == "0.3.0"
        print("PASS: test_card_protocol_version")

    def test_card_preferred_transport(self) -> None:
        """Agent card declares preferred transport as JSONRPC."""
        agent = EchoAgent()
        card = agent._build_card()

        assert card["preferredTransport"] == "JSONRPC"
        print("PASS: test_card_preferred_transport")

    def test_card_capabilities_structure(self) -> None:
        """Agent card capabilities are present and have expected flags."""
        agent = EchoAgent()
        card = agent._build_card()

        capabilities = card.get("capabilities", {})
        assert isinstance(capabilities, dict)
        # Echo agent explicitly sets streaming=False, pushNotifications=False
        assert capabilities.get("streaming") is False
        assert capabilities.get("pushNotifications") is False
        print("PASS: test_card_capabilities_structure")

    def test_card_default_modes(self) -> None:
        """Agent card specifies input/output modes."""
        agent = EchoAgent()
        card = agent._build_card()

        assert card["defaultInputModes"] == ["text"]
        assert card["defaultOutputModes"] == ["text"]
        print("PASS: test_card_default_modes")

    def test_card_skills_array(self) -> None:
        """Agent card skills is a non-empty array."""
        agent = EchoAgent()
        card = agent._build_card()

        skills = card.get("skills", [])
        assert isinstance(skills, list)
        assert len(skills) >= 1
        print("PASS: test_card_skills_array")

    def test_card_echo_skill_definition(self) -> None:
        """Echo skill has required metadata and examples."""
        agent = EchoAgent()
        card = agent._build_card()

        skills = card.get("skills", [])
        echo_skill = skills[0]

        assert echo_skill["id"] == "echo"
        assert echo_skill["name"] == "Echo"
        assert isinstance(echo_skill["description"], str)
        assert len(echo_skill["description"]) > 0
        assert isinstance(echo_skill.get("tags", []), list)
        assert "echo" in echo_skill["tags"]
        assert isinstance(echo_skill.get("examples", []), list)
        assert len(echo_skill["examples"]) > 0
        print("PASS: test_card_echo_skill_definition")


# ============================================================================
# Skill Registration Tests
# ============================================================================

class TestEchoAgentSkillRegistration:
    """Test EchoAgent._register_skills() and skill availability."""

    def test_echo_skill_registered(self) -> None:
        """Echo skill 'echo' is registered after __init__."""
        agent = EchoAgent()
        assert "echo" in agent._skills
        assert callable(agent._skills["echo"])
        print("PASS: test_echo_skill_registered")

    def test_only_echo_skill_registered(self) -> None:
        """Only the 'echo' skill is registered (single-skill agent)."""
        agent = EchoAgent()
        assert len(agent._skills) == 1
        assert set(agent._skills.keys()) == {"echo"}
        print("PASS: test_only_echo_skill_registered")


# ============================================================================
# Echo Handler Tests (Text Extraction & Artifact Formation)
# ============================================================================

class TestEchoHandlerTextExtraction:
    """Test _echo_handler() text extraction behavior."""

    def test_echo_handler_single_text_part(self) -> None:
        """Echo handler extracts and returns text from a single text part."""
        client, _ = make_echo_client()

        input_text = "Hello, echo agent!"
        message = _build_message(input_text)
        resp = rpc_post(client, "message/send", {"message": message})

        assert "error" not in resp
        task = resp["result"]
        assert task["status"]["state"] == "completed"
        assert len(task["artifacts"]) == 1

        artifact = task["artifacts"][0]
        assert artifact["name"] == "echo"
        assert len(artifact["parts"]) == 1
        part = artifact["parts"][0]
        assert part["kind"] == "text"
        assert part["text"] == input_text
        print("PASS: test_echo_handler_single_text_part")

    def test_echo_handler_preserves_whitespace(self) -> None:
        """Echo handler preserves leading/trailing/internal whitespace."""
        client, _ = make_echo_client()

        # Test various whitespace patterns
        test_cases = [
            "  leading spaces",
            "trailing spaces  ",
            "  both  ",
            "multiple\n\nline\nbreaks",
            "tab\tseparated\tvalues",
            "emoji 🚀 mixed in",
        ]

        for input_text in test_cases:
            message = _build_message(input_text)
            resp = rpc_post(client, "message/send", {"message": message})
            assert "error" not in resp
            artifact = resp["result"]["artifacts"][0]
            part = artifact["parts"][0]
            assert part["text"] == input_text, f"Whitespace not preserved: {input_text!r}"

        print("PASS: test_echo_handler_preserves_whitespace")

    def test_echo_handler_empty_string(self) -> None:
        """Echo handler correctly echoes an empty string (edge case)."""
        client, _ = make_echo_client()

        input_text = ""
        message = _build_message(input_text)
        resp = rpc_post(client, "message/send", {"message": message})

        assert "error" not in resp
        task = resp["result"]
        artifact = task["artifacts"][0]
        part = artifact["parts"][0]
        assert part["text"] == ""
        print("PASS: test_echo_handler_empty_string")

    def test_echo_handler_long_text(self) -> None:
        """Echo handler handles long multi-paragraph text."""
        client, _ = make_echo_client()

        # Simulate a realistic long prompt
        input_text = "\n".join([
            "Plan a 7-day trip to Japan.",
            "Budget: $3000",
            "Interests: culture, food, hiking",
            "Departure: Tokyo, return: Osaka",
        ])

        message = _build_message(input_text)
        resp = rpc_post(client, "message/send", {"message": message})

        assert "error" not in resp
        artifact = resp["result"]["artifacts"][0]
        part = artifact["parts"][0]
        assert part["text"] == input_text
        print("PASS: test_echo_handler_long_text")

    def test_echo_handler_special_characters(self) -> None:
        """Echo handler preserves special characters and Unicode."""
        client, _ = make_echo_client()

        input_text = "Special: !@#$%^&*() | Unicode: Ñoño, 日本, Москва"
        message = _build_message(input_text)
        resp = rpc_post(client, "message/send", {"message": message})

        assert "error" not in resp
        artifact = resp["result"]["artifacts"][0]
        part = artifact["parts"][0]
        assert part["text"] == input_text
        print("PASS: test_echo_handler_special_characters")

    def test_echo_handler_first_text_part_only(self) -> None:
        """Echo handler uses the FIRST text part when multiple text parts exist."""
        client, _ = make_echo_client()

        # Create a message with multiple text parts (unusual but valid)
        message = {
            "kind": "message",
            "messageId": str(uuid.uuid4()),
            "role": "user",
            "parts": [
                {"kind": "text", "text": "First text"},
                {"kind": "text", "text": "Second text (ignored)"},
                {"kind": "data", "data": {"some": "data"}},
            ],
        }
        resp = rpc_post(client, "message/send", {"message": message})

        assert "error" not in resp
        artifact = resp["result"]["artifacts"][0]
        part = artifact["parts"][0]
        # Should echo only the first text part
        assert part["text"] == "First text"
        print("PASS: test_echo_handler_first_text_part_only")


# ============================================================================
# Echo Handler Tests (Non-Text Part Handling)
# ============================================================================

class TestEchoHandlerNonTextParts:
    """Test _echo_handler() behavior when no text part is found."""

    def test_echo_handler_no_text_part_returns_data_artifact(self) -> None:
        """Echo handler with no text parts returns data artifact with original_parts."""
        client, _ = make_echo_client()

        # Message with only data parts, no text
        message = {
            "kind": "message",
            "messageId": str(uuid.uuid4()),
            "role": "user",
            "parts": [
                {"kind": "data", "data": {"key": "value"}},
            ],
        }
        resp = rpc_post(client, "message/send", {"message": message})

        assert "error" not in resp
        task = resp["result"]
        assert task["status"]["state"] == "completed"

        artifact = task["artifacts"][0]
        assert artifact["name"] == "echo"
        assert len(artifact["parts"]) == 1

        part = artifact["parts"][0]
        assert part["kind"] == "data"
        assert "original_parts" in part["data"]
        # Verify that the original message parts are captured
        assert part["data"]["original_parts"] == message["parts"]
        print("PASS: test_echo_handler_no_text_part_returns_data_artifact")

    def test_echo_handler_empty_parts_list_returns_data_artifact(self) -> None:
        """Echo handler with empty parts list returns data artifact."""
        client, _ = make_echo_client()

        message = {
            "kind": "message",
            "messageId": str(uuid.uuid4()),
            "role": "user",
            "parts": [],  # Empty parts; test framework normally prevents this, but test directly
        }
        # Bypass the framework validation by using the raw handler
        agent = EchoAgent()
        task = {"id": str(uuid.uuid4()), "status": {"state": "working"}}

        # Call handler directly (doesn't go through JSON-RPC framework validation)
        import asyncio
        artifact = asyncio.run(agent._echo_handler(message, task))

        assert artifact["name"] == "echo"
        assert len(artifact["parts"]) == 1
        part = artifact["parts"][0]
        assert part["kind"] == "data"
        assert part["data"]["original_parts"] == []
        print("PASS: test_echo_handler_empty_parts_list_returns_data_artifact")

    def test_echo_handler_text_part_with_empty_text(self) -> None:
        """Echo handler with text part having empty string value echoes it."""
        client, _ = make_echo_client()

        message = {
            "kind": "message",
            "messageId": str(uuid.uuid4()),
            "role": "user",
            "parts": [
                {"kind": "text", "text": ""},
            ],
        }
        resp = rpc_post(client, "message/send", {"message": message})

        assert "error" not in resp
        artifact = resp["result"]["artifacts"][0]
        part = artifact["parts"][0]
        assert part["kind"] == "text"
        assert part["text"] == ""
        print("PASS: test_echo_handler_text_part_with_empty_text")

    def test_echo_handler_text_part_missing_text_field(self) -> None:
        """Echo handler with text part missing 'text' field defaults to empty string."""
        client, _ = make_echo_client()

        agent = EchoAgent()
        message = {
            "kind": "message",
            "messageId": str(uuid.uuid4()),
            "role": "user",
            "parts": [
                {"kind": "text"},  # Missing "text" field
            ],
        }
        task = {"id": str(uuid.uuid4()), "status": {"state": "working"}}

        import asyncio
        artifact = asyncio.run(agent._echo_handler(message, task))

        part = artifact["parts"][0]
        assert part["kind"] == "text"
        # The handler uses part.get("text", "") so should default to ""
        assert part["text"] == ""
        print("PASS: test_echo_handler_text_part_missing_text_field")


# ============================================================================
# Artifact Structure Tests
# ============================================================================

class TestEchoArtifactStructure:
    """Test that returned artifacts have correct A2A structure."""

    def test_artifact_has_required_fields(self) -> None:
        """Echo artifact contains artifactId, name, and parts."""
        client, _ = make_echo_client()

        message = _build_message("test")
        resp = rpc_post(client, "message/send", {"message": message})

        artifact = resp["result"]["artifacts"][0]
        assert "artifactId" in artifact
        assert artifact["artifactId"] != ""
        assert artifact["name"] == "echo"
        assert "parts" in artifact
        assert isinstance(artifact["parts"], list)
        print("PASS: test_artifact_has_required_fields")

    def test_artifact_id_is_unique(self) -> None:
        """Multiple echo requests produce unique artifactId values."""
        client, _ = make_echo_client()

        artifact_ids = set()
        for i in range(3):
            message = _build_message(f"message {i}")
            resp = rpc_post(client, "message/send", {"message": message})
            artifact_id = resp["result"]["artifacts"][0]["artifactId"]
            assert artifact_id not in artifact_ids, "Duplicate artifactId found"
            artifact_ids.add(artifact_id)

        assert len(artifact_ids) == 3
        print("PASS: test_artifact_id_is_unique")


# ============================================================================
# Card Serving Tests (HTTP layer)
# ============================================================================

class TestEchoAgentCardServing:
    """Test card serving via the Starlette app."""

    def test_card_served_at_primary_path(self) -> None:
        """Agent card is served at GET /.well-known/agent-card.json."""
        client, _ = make_echo_client()
        resp = client.get("/.well-known/agent-card.json")

        assert resp.status_code == 200
        card = resp.json()
        assert card["name"] == "echo-agent"
        print("PASS: test_card_served_at_primary_path")

    def test_card_served_at_legacy_path(self) -> None:
        """Agent card is served at legacy path GET /.well-known/agent.json."""
        client, _ = make_echo_client()
        resp = client.get("/.well-known/agent.json")

        assert resp.status_code == 200
        card = resp.json()
        assert card["name"] == "echo-agent"
        print("PASS: test_card_served_at_legacy_path")

    def test_card_is_valid_json(self) -> None:
        """Agent card is valid JSON (can be parsed and serialized)."""
        client, _ = make_echo_client()
        resp = client.get("/.well-known/agent-card.json")

        card = resp.json()
        # Re-serialize to JSON and ensure it's valid
        card_json = json.dumps(card)
        assert isinstance(card_json, str)
        assert len(card_json) > 0
        print("PASS: test_card_is_valid_json")


# ============================================================================
# Integration Tests (Full A2A flow)
# ============================================================================

class TestEchoAgentIntegration:
    """Test full A2A message/send → completed task flow."""

    def test_full_roundtrip_with_custom_host_port(self) -> None:
        """Full A2A roundtrip with custom host/port agent."""
        client, agent = make_echo_client(host="127.0.0.1", port=7654)

        input_text = "Integration test"
        message = _build_message(input_text)
        resp = rpc_post(client, "message/send", {"message": message})

        assert "error" not in resp
        task = resp["result"]
        assert task["status"]["state"] == "completed"

        # Verify card URL reflects the host/port
        card = agent._build_card()
        assert card["url"] == "http://127.0.0.1:7654"
        print("PASS: test_full_roundtrip_with_custom_host_port")

    def test_multiple_sequential_messages(self) -> None:
        """Multiple sequential messages are handled independently."""
        client, _ = make_echo_client()

        texts = ["First", "Second", "Third"]
        task_ids = []

        for text in texts:
            message = _build_message(text)
            resp = rpc_post(client, "message/send", {"message": message})

            task = resp["result"]
            task_ids.append(task["id"])
            assert task["artifacts"][0]["parts"][0]["text"] == text

        # Verify all task IDs are unique
        assert len(set(task_ids)) == 3
        print("PASS: test_multiple_sequential_messages")

    def test_context_id_propagation(self) -> None:
        """contextId from message is preserved in the returned task."""
        client, _ = make_echo_client()

        context_id = "test-context-" + str(uuid.uuid4())
        message = _build_message("test", context_id=context_id)
        resp = rpc_post(client, "message/send", {"message": message})

        task = resp["result"]
        assert task["contextId"] == context_id
        print("PASS: test_context_id_propagation")

    def test_task_history_includes_user_and_agent_messages(self) -> None:
        """Task history contains both user input and agent reply."""
        client, _ = make_echo_client()

        input_text = "History test"
        message = _build_message(input_text)
        resp = rpc_post(client, "message/send", {"message": message})

        task = resp["result"]
        history = task["history"]
        assert len(history) >= 2

        roles = [m.get("role") for m in history]
        assert "user" in roles
        assert "agent" in roles

        # First message should be user
        assert history[0]["role"] == "user"
        # Last message should be agent
        assert history[-1]["role"] == "agent"
        print("PASS: test_task_history_includes_user_and_agent_messages")


# ============================================================================
# Determinism Tests (var-0 safety)
# ============================================================================

class TestEchoAgentDeterminism:
    """Verify echo agent behavior is deterministic and repeatable."""

    def test_same_input_produces_same_output(self) -> None:
        """Same input text always produces identical output."""
        input_text = "Deterministic test"

        results = []
        for _ in range(3):
            client, _ = make_echo_client()
            message = _build_message(input_text)
            resp = rpc_post(client, "message/send", {"message": message})
            artifact = resp["result"]["artifacts"][0]
            results.append(artifact["parts"][0]["text"])

        # All outputs should match input
        for output in results:
            assert output == input_text
        print("PASS: test_same_input_produces_same_output")

    def test_no_internal_state_leakage_between_requests(self) -> None:
        """Requests are independent; no state leaks between them."""
        client, _ = make_echo_client()

        inputs = ["Alpha", "Beta", "Gamma"]
        for text in inputs:
            message = _build_message(text)
            resp = rpc_post(client, "message/send", {"message": message})
            artifact = resp["result"]["artifacts"][0]
            # Each message echoes exactly its input, not mixed with prior inputs
            assert artifact["parts"][0]["text"] == text

        print("PASS: test_no_internal_state_leakage_between_requests")


# ============================================================================
# Test Runner
# ============================================================================

TESTS = [
    # Constructor tests
    TestEchoAgentConstructor.test_constructor_default_params,
    TestEchoAgentConstructor.test_constructor_custom_host,
    TestEchoAgentConstructor.test_constructor_custom_port,
    TestEchoAgentConstructor.test_constructor_custom_host_and_port,

    # Card building tests
    TestEchoAgentCardBuilding.test_card_required_fields,
    TestEchoAgentCardBuilding.test_card_name_and_description,
    TestEchoAgentCardBuilding.test_card_url_includes_host_port,
    TestEchoAgentCardBuilding.test_card_version,
    TestEchoAgentCardBuilding.test_card_protocol_version,
    TestEchoAgentCardBuilding.test_card_preferred_transport,
    TestEchoAgentCardBuilding.test_card_capabilities_structure,
    TestEchoAgentCardBuilding.test_card_default_modes,
    TestEchoAgentCardBuilding.test_card_skills_array,
    TestEchoAgentCardBuilding.test_card_echo_skill_definition,

    # Skill registration tests
    TestEchoAgentSkillRegistration.test_echo_skill_registered,
    TestEchoAgentSkillRegistration.test_only_echo_skill_registered,

    # Echo handler tests
    TestEchoHandlerTextExtraction.test_echo_handler_single_text_part,
    TestEchoHandlerTextExtraction.test_echo_handler_preserves_whitespace,
    TestEchoHandlerTextExtraction.test_echo_handler_empty_string,
    TestEchoHandlerTextExtraction.test_echo_handler_long_text,
    TestEchoHandlerTextExtraction.test_echo_handler_special_characters,
    TestEchoHandlerTextExtraction.test_echo_handler_first_text_part_only,

    # Non-text part handling
    TestEchoHandlerNonTextParts.test_echo_handler_no_text_part_returns_data_artifact,
    TestEchoHandlerNonTextParts.test_echo_handler_empty_parts_list_returns_data_artifact,
    TestEchoHandlerNonTextParts.test_echo_handler_text_part_with_empty_text,
    TestEchoHandlerNonTextParts.test_echo_handler_text_part_missing_text_field,

    # Artifact structure tests
    TestEchoArtifactStructure.test_artifact_has_required_fields,
    TestEchoArtifactStructure.test_artifact_id_is_unique,

    # Card serving tests
    TestEchoAgentCardServing.test_card_served_at_primary_path,
    TestEchoAgentCardServing.test_card_served_at_legacy_path,
    TestEchoAgentCardServing.test_card_is_valid_json,

    # Integration tests
    TestEchoAgentIntegration.test_full_roundtrip_with_custom_host_port,
    TestEchoAgentIntegration.test_multiple_sequential_messages,
    TestEchoAgentIntegration.test_context_id_propagation,
    TestEchoAgentIntegration.test_task_history_includes_user_and_agent_messages,

    # Determinism tests
    TestEchoAgentDeterminism.test_same_input_produces_same_output,
    TestEchoAgentDeterminism.test_no_internal_state_leakage_between_requests,
]


def main() -> None:
    """Run all tests."""
    passed = 0
    failed = 0
    errors: list[tuple[str, Exception]] = []

    print(f"\n{'='*70}")
    print("Travel Guild — Echo Agent Coverage Test Suite")
    print(f"{'='*70}\n")

    for test_fn in TESTS:
        name = test_fn.__name__
        try:
            test_fn()
            passed += 1
        except Exception as exc:
            failed += 1
            errors.append((name, exc))
            print(f"FAIL: {name}")
            import traceback
            traceback.print_exc()

    print(f"\n{'='*70}")
    print(f"Results: {passed} passed, {failed} failed out of {len(TESTS)} tests")
    print(f"{'='*70}\n")

    if errors:
        sys.exit(1)


if __name__ == "__main__":
    main()
