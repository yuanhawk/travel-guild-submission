"""
test_a2a_agent_cov.py — Comprehensive test coverage for a2a_agent.py module.

This module tests the A2AAgent base class and its helper functions directly:
  1. Helper functions: _rpc_error, _rpc_result, _now_iso, _new_task, etc.
  2. Dispatch logic: single-skill, multi-skill, no-skill scenarios
  3. Message/Send flow: role validation, artifact generation, metadata
  4. Error handling: crashes vs rejections, error codes
  5. Task state transitions and history building
  6. Card caching and URL building
  7. Deterministic UUID generation

Uses Starlette TestClient for in-process ASGI transport (no actual HTTP binding).
All tests are var-0-safe: no live LLM, no network, no paid APIs.
"""

from __future__ import annotations

import datetime
import json
import sys
import os
import uuid
import asyncio
from unittest.mock import patch

# Ensure we can import from the society/ directory
sys.path.insert(0, os.path.dirname(__file__))

from starlette.testclient import TestClient

from agents.a2a_agent import (
    A2AAgent,
    SkillRejected,
    _rpc_error,
    _rpc_result,
    _now_iso,
    _new_task,
    _task_transition,
    _new_message,
    _text_part,
    _data_part,
    _new_artifact,
    PARSE_ERROR,
    INVALID_REQUEST,
    METHOD_NOT_FOUND,
    INVALID_PARAMS,
    INTERNAL_ERROR,
)
from orchestration.a2a_client import _build_message, _build_rpc_request


# ============================================================================
# Helper Function Tests
# ============================================================================

class TestRpcErrorFunction:
    """Test _rpc_error() helper function."""

    def test_rpc_error_basic_without_data(self) -> None:
        """_rpc_error() produces a valid JSON-RPC 2.0 error object without data field."""
        result = _rpc_error("test-id", -32600, "Invalid Request")

        assert result["jsonrpc"] == "2.0"
        assert result["id"] == "test-id"
        assert "error" in result
        assert result["error"]["code"] == -32600
        assert result["error"]["message"] == "Invalid Request"
        assert "data" not in result["error"]
        print("PASS: test_rpc_error_basic_without_data")

    def test_rpc_error_with_data(self) -> None:
        """_rpc_error() includes data field when provided."""
        data = {"taskId": "abc123"}
        result = _rpc_error("test-id", INVALID_PARAMS, "Missing field", data=data)

        assert result["error"]["code"] == INVALID_PARAMS
        assert result["error"]["data"] == data
        print("PASS: test_rpc_error_with_data")

    def test_rpc_error_with_none_id(self) -> None:
        """_rpc_error() handles None as RPC id."""
        result = _rpc_error(None, PARSE_ERROR, "Parse error")

        assert result["id"] is None
        assert result["error"]["code"] == PARSE_ERROR
        print("PASS: test_rpc_error_with_none_id")

    def test_rpc_error_numeric_id(self) -> None:
        """_rpc_error() preserves numeric RPC id."""
        result = _rpc_error(42, METHOD_NOT_FOUND, "Method not found")

        assert result["id"] == 42
        assert result["error"]["code"] == METHOD_NOT_FOUND
        print("PASS: test_rpc_error_numeric_id")


class TestRpcResultFunction:
    """Test _rpc_result() helper function."""

    def test_rpc_result_basic(self) -> None:
        """_rpc_result() produces a valid JSON-RPC 2.0 result object."""
        task_dict = {"id": "task-123", "status": "completed"}
        result = _rpc_result("request-id", task_dict)

        assert result["jsonrpc"] == "2.0"
        assert result["id"] == "request-id"
        assert result["result"] == task_dict
        assert "error" not in result
        print("PASS: test_rpc_result_basic")

    def test_rpc_result_with_null_result(self) -> None:
        """_rpc_result() handles None as result."""
        result = _rpc_result("req-1", None)

        assert result["result"] is None
        print("PASS: test_rpc_result_with_null_result")

    def test_rpc_result_with_list_result(self) -> None:
        """_rpc_result() handles list as result."""
        data = [{"id": "1"}, {"id": "2"}]
        result = _rpc_result("batch", data)

        assert result["result"] == data
        print("PASS: test_rpc_result_with_list_result")


class TestNowIsoFunction:
    """Test _now_iso() ISO 8601 timestamp generation."""

    def test_now_iso_format(self) -> None:
        """_now_iso() returns a valid ISO 8601 UTC timestamp."""
        ts = _now_iso()

        assert isinstance(ts, str)
        # ISO 8601 format: YYYY-MM-DDTHH:MM:SS+00:00
        # Should parse back to datetime
        parsed = datetime.datetime.fromisoformat(ts)
        assert parsed.tzinfo is not None  # Must have timezone info
        print("PASS: test_now_iso_format")

    def test_now_iso_utc(self) -> None:
        """_now_iso() produces UTC timestamps (timezone offset matches UTC)."""
        ts = _now_iso()
        parsed = datetime.datetime.fromisoformat(ts)

        # Check that it's UTC (offset should be +00:00)
        assert parsed.tzinfo == datetime.timezone.utc
        print("PASS: test_now_iso_utc")

    def test_now_iso_freshness(self) -> None:
        """_now_iso() timestamp is recent (within last second)."""
        before = datetime.datetime.now(datetime.timezone.utc)
        ts = _now_iso()
        after = datetime.datetime.now(datetime.timezone.utc)
        parsed = datetime.datetime.fromisoformat(ts)

        assert before <= parsed <= after
        print("PASS: test_now_iso_freshness")


class TestNewTaskFunction:
    """Test _new_task() task factory."""

    def test_new_task_default_context_id(self) -> None:
        """_new_task() generates a context_id if none provided."""
        task = _new_task()

        assert "id" in task
        assert "contextId" in task
        assert task["contextId"] != ""
        assert task["status"]["state"] == "submitted"
        assert task["kind"] == "task"
        assert "artifacts" in task
        assert "history" in task
        assert "metadata" in task
        assert isinstance(task["artifacts"], list)
        assert len(task["artifacts"]) == 0
        print("PASS: test_new_task_default_context_id")

    def test_new_task_explicit_context_id(self) -> None:
        """_new_task(context_id=...) uses the provided context_id."""
        context_id = "my-context-xyz"
        task = _new_task(context_id=context_id)

        assert task["contextId"] == context_id
        print("PASS: test_new_task_explicit_context_id")

    def test_new_task_unique_ids(self) -> None:
        """Multiple _new_task() calls produce unique task IDs."""
        task1 = _new_task()
        task2 = _new_task()

        assert task1["id"] != task2["id"]
        # Context IDs should also be different (generated independently)
        assert task1["contextId"] != task2["contextId"]
        print("PASS: test_new_task_unique_ids")

    def test_new_task_timestamp_present(self) -> None:
        """_new_task() includes a timestamp in the status."""
        task = _new_task()

        assert "timestamp" in task["status"]
        ts = task["status"]["timestamp"]
        # Should be a valid ISO 8601 string
        parsed = datetime.datetime.fromisoformat(ts)
        assert parsed is not None
        print("PASS: test_new_task_timestamp_present")


class TestTaskTransitionFunction:
    """Test _task_transition() state mutation."""

    def test_task_transition_state_change(self) -> None:
        """_task_transition() updates the task state."""
        task = _new_task()
        assert task["status"]["state"] == "submitted"

        _task_transition(task, "working")
        assert task["status"]["state"] == "working"

        _task_transition(task, "completed")
        assert task["status"]["state"] == "completed"
        print("PASS: test_task_transition_state_change")

    def test_task_transition_timestamp_updated(self) -> None:
        """_task_transition() updates the timestamp."""
        task = _new_task()
        original_ts = task["status"]["timestamp"]

        import time
        time.sleep(0.01)  # Ensure time passes

        _task_transition(task, "working")
        new_ts = task["status"]["timestamp"]

        # Timestamps should be different (new one is later)
        assert original_ts != new_ts
        original_dt = datetime.datetime.fromisoformat(original_ts)
        new_dt = datetime.datetime.fromisoformat(new_ts)
        assert new_dt >= original_dt
        print("PASS: test_task_transition_timestamp_updated")


class TestNewMessageFunction:
    """Test _new_message() message factory."""

    def test_new_message_basic(self) -> None:
        """_new_message() creates a valid message dict."""
        parts = [{"kind": "text", "text": "hello"}]
        msg = _new_message("user", parts, "task-1", "ctx-1")

        assert msg["kind"] == "message"
        assert msg["role"] == "user"
        assert msg["parts"] == parts
        assert msg["taskId"] == "task-1"
        assert msg["contextId"] == "ctx-1"
        assert "messageId" in msg
        print("PASS: test_new_message_basic")

    def test_new_message_explicit_message_id(self) -> None:
        """_new_message() uses provided messageId."""
        msg_id = "msg-custom-123"
        msg = _new_message("agent", [], "task-1", "ctx-1", message_id=msg_id)

        assert msg["messageId"] == msg_id
        print("PASS: test_new_message_explicit_message_id")

    def test_new_message_generated_message_id(self) -> None:
        """_new_message() generates a messageId if not provided."""
        msg = _new_message("user", [], "task-1", "ctx-1")

        assert "messageId" in msg
        assert msg["messageId"] != ""
        # Should be a UUID
        uuid.UUID(msg["messageId"])
        print("PASS: test_new_message_generated_message_id")

    def test_new_message_agent_role(self) -> None:
        """_new_message() works with agent role."""
        msg = _new_message("agent", [{"kind": "text", "text": "response"}], "task-1", "ctx-1")

        assert msg["role"] == "agent"
        print("PASS: test_new_message_agent_role")


class TestTextPartFunction:
    """Test _text_part() part factory."""

    def test_text_part_basic(self) -> None:
        """_text_part() creates a text part."""
        text = "Hello, world!"
        part = _text_part(text)

        assert part["kind"] == "text"
        assert part["text"] == text
        print("PASS: test_text_part_basic")

    def test_text_part_empty_string(self) -> None:
        """_text_part() works with empty string."""
        part = _text_part("")

        assert part["kind"] == "text"
        assert part["text"] == ""
        print("PASS: test_text_part_empty_string")

    def test_text_part_unicode(self) -> None:
        """_text_part() preserves Unicode characters."""
        text = "Москва 日本 Ñoño 🚀"
        part = _text_part(text)

        assert part["text"] == text
        print("PASS: test_text_part_unicode")


class TestDataPartFunction:
    """Test _data_part() part factory."""

    def test_data_part_basic(self) -> None:
        """_data_part() creates a data part with dict."""
        data = {"key": "value", "nested": {"x": 1}}
        part = _data_part(data)

        assert part["kind"] == "data"
        assert part["data"] == data
        print("PASS: test_data_part_basic")

    def test_data_part_list(self) -> None:
        """_data_part() works with list data."""
        data = [1, 2, 3]
        part = _data_part(data)

        assert part["kind"] == "data"
        assert part["data"] == data
        print("PASS: test_data_part_list")

    def test_data_part_scalar(self) -> None:
        """_data_part() works with scalar data."""
        part = _data_part(42)

        assert part["kind"] == "data"
        assert part["data"] == 42
        print("PASS: test_data_part_scalar")


class TestNewArtifactFunction:
    """Test _new_artifact() artifact factory."""

    def test_new_artifact_basic(self) -> None:
        """_new_artifact() creates a valid artifact."""
        parts = [{"kind": "text", "text": "result"}]
        artifact = _new_artifact("output", parts)

        assert artifact["name"] == "output"
        assert artifact["parts"] == parts
        assert "artifactId" in artifact
        assert artifact["artifactId"] != ""
        print("PASS: test_new_artifact_basic")

    def test_new_artifact_explicit_id(self) -> None:
        """_new_artifact() uses provided artifactId."""
        artifact_id = "art-custom-123"
        artifact = _new_artifact("output", [], artifact_id=artifact_id)

        assert artifact["artifactId"] == artifact_id
        print("PASS: test_new_artifact_explicit_id")

    def test_new_artifact_generated_id(self) -> None:
        """_new_artifact() generates artifactId if not provided."""
        artifact = _new_artifact("output", [])

        assert "artifactId" in artifact
        uuid.UUID(artifact["artifactId"])
        print("PASS: test_new_artifact_generated_id")

    def test_new_artifact_unique_ids(self) -> None:
        """Multiple _new_artifact() calls produce unique IDs."""
        art1 = _new_artifact("out1", [])
        art2 = _new_artifact("out2", [])

        assert art1["artifactId"] != art2["artifactId"]
        print("PASS: test_new_artifact_unique_ids")


# ============================================================================
# Multi-Skill Dispatch Tests
# ============================================================================

class _MultiSkillAgent(A2AAgent):
    """Test agent with multiple registered skills."""

    def _build_card(self) -> dict:
        return {
            "name": "multi-skill-agent",
            "description": "Test agent with multiple skills",
            "url": "http://0.0.0.0:9999",
            "version": "1.0.0",
            "protocolVersion": "0.3.0",
            "preferredTransport": "JSONRPC",
            "capabilities": {},
            "skills": [
                {"id": "skill-a", "name": "Skill A", "description": "First skill", "tags": []},
                {"id": "skill-b", "name": "Skill B", "description": "Second skill", "tags": []},
            ],
        }

    def _register_skills(self) -> None:
        self.register_skill("skill-a", self._skill_a_handler)
        self.register_skill("skill-b", self._skill_b_handler)

    async def _skill_a_handler(self, message: dict, task: dict) -> dict:
        return _new_artifact("result-a", [_text_part("handled by A")])

    async def _skill_b_handler(self, message: dict, task: dict) -> dict:
        return _new_artifact("result-b", [_text_part("handled by B")])


class TestMultiSkillDispatch:
    """Test dispatch logic with multiple registered skills."""

    def test_multi_skill_explicit_skill_a(self) -> None:
        """Multiple skills: explicit skillId='skill-a' routes to skill A."""
        agent = _MultiSkillAgent()
        app = agent.build_app()
        client = TestClient(app, raise_server_exceptions=True)

        message = _build_message("test")
        message["metadata"] = {"skillId": "skill-a"}

        body = _build_rpc_request("message/send", {"message": message})
        resp = client.post("/", json=body)
        result = resp.json()

        assert "error" not in result
        task = result["result"]
        assert task["status"]["state"] == "completed"
        artifact = task["artifacts"][0]
        assert artifact["name"] == "result-a"
        assert artifact["parts"][0]["text"] == "handled by A"
        print("PASS: test_multi_skill_explicit_skill_a")

    def test_multi_skill_explicit_skill_b(self) -> None:
        """Multiple skills: explicit skillId='skill-b' routes to skill B."""
        agent = _MultiSkillAgent()
        app = agent.build_app()
        client = TestClient(app, raise_server_exceptions=True)

        message = _build_message("test")
        message["metadata"] = {"skillId": "skill-b"}

        body = _build_rpc_request("message/send", {"message": message})
        resp = client.post("/", json=body)
        result = resp.json()

        assert "error" not in result
        task = result["result"]
        artifact = task["artifacts"][0]
        assert artifact["name"] == "result-b"
        assert artifact["parts"][0]["text"] == "handled by B"
        print("PASS: test_multi_skill_explicit_skill_b")

    def test_multi_skill_no_skill_id_error(self) -> None:
        """Multiple skills: no skillId raises SkillRejected (must disambiguate)."""
        agent = _MultiSkillAgent()
        app = agent.build_app()
        client = TestClient(app, raise_server_exceptions=True)

        message = _build_message("test")
        # No metadata.skillId

        body = _build_rpc_request("message/send", {"message": message})
        resp = client.post("/", json=body)
        result = resp.json()

        assert "error" not in result
        task = result["result"]
        assert task["status"]["state"] == "rejected"
        assert "Multiple skills" in (task.get("metadata") or {}).get("error", "")
        print("PASS: test_multi_skill_no_skill_id_error")

    def test_multi_skill_unknown_skill_id(self) -> None:
        """Multiple skills: unknown skillId raises SkillRejected (fail closed)."""
        agent = _MultiSkillAgent()
        app = agent.build_app()
        client = TestClient(app, raise_server_exceptions=True)

        message = _build_message("test")
        message["metadata"] = {"skillId": "skill-does-not-exist"}

        body = _build_rpc_request("message/send", {"message": message})
        resp = client.post("/", json=body)
        result = resp.json()

        assert "error" not in result
        task = result["result"]
        assert task["status"]["state"] == "rejected"
        assert "Unknown skillId" in (task.get("metadata") or {}).get("error", "")
        print("PASS: test_multi_skill_unknown_skill_id")


# ============================================================================
# Message/Send Edge Cases
# ============================================================================

class _TestAgent(A2AAgent):
    """Minimal test agent for edge case testing."""

    def _build_card(self) -> dict:
        return {
            "name": "test-agent",
            "description": "Test agent",
            "url": "http://0.0.0.0:9998",
            "version": "1.0.0",
            "protocolVersion": "0.3.0",
            "preferredTransport": "JSONRPC",
            "capabilities": {},
            "skills": [{"id": "test", "name": "Test", "description": "Test skill", "tags": []}],
        }

    def _register_skills(self) -> None:
        self.register_skill("test", self._test_handler)

    async def _test_handler(self, message: dict, task: dict) -> dict:
        # Default: just echo the message ID
        msg_id = message.get("messageId", "no-id")
        return _new_artifact("test", [_text_part(msg_id)])


class TestMessageSendEdgeCases:
    """Test edge cases in message/send handling."""

    def test_invalid_role_non_user_or_agent(self) -> None:
        """message.role must be 'user' or 'agent', not arbitrary string."""
        agent = _TestAgent()
        app = agent.build_app()
        client = TestClient(app, raise_server_exceptions=True)

        message = _build_message("test")
        message["role"] = "invalid-role"

        body = _build_rpc_request("message/send", {"message": message})
        resp = client.post("/", json=body)
        result = resp.json()

        assert "error" in result
        assert result["error"]["code"] == INVALID_PARAMS
        assert "role" in result["error"]["message"].lower()
        print("PASS: test_invalid_role_non_user_or_agent")

    def test_missing_role_defaults_to_user(self) -> None:
        """message without role field defaults to 'user'."""
        agent = _TestAgent()
        app = agent.build_app()
        client = TestClient(app, raise_server_exceptions=True)

        message = {
            "kind": "message",
            "messageId": str(uuid.uuid4()),
            "parts": [{"kind": "text", "text": "test"}],
        }
        # No "role" field

        body = _build_rpc_request("message/send", {"message": message})
        resp = client.post("/", json=body)
        result = resp.json()

        assert "error" not in result
        task = result["result"]
        assert task["status"]["state"] == "completed"
        print("PASS: test_missing_role_defaults_to_user")

    def test_artifact_without_artifact_id_gets_generated(self) -> None:
        """If skill handler returns artifact without artifactId, one is generated."""

        class _NoIdArtifactAgent(A2AAgent):
            def _build_card(self) -> dict:
                return {
                    "name": "noid-agent",
                    "description": "Returns artifact without ID",
                    "url": "http://0.0.0.0:9997",
                    "version": "1.0.0",
                    "protocolVersion": "0.3.0",
                    "preferredTransport": "JSONRPC",
                    "capabilities": {},
                    "skills": [{"id": "noid", "name": "NoID", "description": "", "tags": []}],
                }

            def _register_skills(self) -> None:
                self.register_skill("noid", self._handler)

            async def _handler(self, message: dict, task: dict) -> dict:
                # Return artifact without artifactId (unusual but tested)
                return {"name": "output", "parts": [{"kind": "text", "text": "result"}]}

        agent = _NoIdArtifactAgent()
        app = agent.build_app()
        client = TestClient(app, raise_server_exceptions=True)

        message = _build_message("test")
        body = _build_rpc_request("message/send", {"message": message})
        resp = client.post("/", json=body)
        result = resp.json()

        assert "error" not in result
        task = result["result"]
        artifact = task["artifacts"][0]
        assert "artifactId" in artifact
        assert artifact["artifactId"] != ""
        print("PASS: test_artifact_without_artifact_id_gets_generated")

    def test_message_without_message_id_gets_generated(self) -> None:
        """Input message without messageId gets one assigned."""
        agent = _TestAgent()
        app = agent.build_app()
        client = TestClient(app, raise_server_exceptions=True)

        message = {
            "kind": "message",
            "role": "user",
            "parts": [{"kind": "text", "text": "test"}],
        }
        # No messageId

        body = _build_rpc_request("message/send", {"message": message})
        resp = client.post("/", json=body)
        result = resp.json()

        assert "error" not in result
        task = result["result"]
        # Check history: the stored message should have a messageId
        stored_msg = task["history"][0]
        assert "messageId" in stored_msg
        assert stored_msg["messageId"] != ""
        print("PASS: test_message_without_message_id_gets_generated")


# ============================================================================
# Task State and History Tests
# ============================================================================

class _StateTransitionAgent(A2AAgent):
    """Agent that can transition task to various states."""

    def __init__(self, return_state: str = "completed"):
        self.return_state = return_state
        super().__init__()

    def _build_card(self) -> dict:
        return {
            "name": "state-agent",
            "description": "Tests state transitions",
            "url": "http://0.0.0.0:9996",
            "version": "1.0.0",
            "protocolVersion": "0.3.0",
            "preferredTransport": "JSONRPC",
            "capabilities": {},
            "skills": [{"id": "state", "name": "State", "description": "", "tags": []}],
        }

    def _register_skills(self) -> None:
        self.register_skill("state", self._handler)

    async def _handler(self, message: dict, task: dict) -> dict:
        if self.return_state != "completed":
            _task_transition(task, self.return_state)
        return _new_artifact("result", [_text_part("done")])


class TestTaskStateAndHistory:
    """Test task state transitions and history building."""

    def test_agent_reply_added_to_history(self) -> None:
        """Agent reply is appended to task history after skill execution."""
        agent = _TestAgent()
        app = agent.build_app()
        client = TestClient(app, raise_server_exceptions=True)

        message = _build_message("input")
        body = _build_rpc_request("message/send", {"message": message})
        resp = client.post("/", json=body)
        result = resp.json()

        task = result["result"]
        history = task["history"]

        assert len(history) >= 2
        # First should be user message
        assert history[0]["role"] == "user"
        # Last should be agent reply
        assert history[-1]["role"] == "agent"
        assert history[-1]["taskId"] == task["id"]
        assert history[-1]["contextId"] == task["contextId"]
        print("PASS: test_agent_reply_added_to_history")

    def test_skill_can_transition_to_input_required(self) -> None:
        """Skill handler can transition task to 'input-required' (non-terminal)."""
        agent = _StateTransitionAgent(return_state="input-required")
        app = agent.build_app()
        client = TestClient(app, raise_server_exceptions=True)

        message = _build_message("test")
        body = _build_rpc_request("message/send", {"message": message})
        resp = client.post("/", json=body)
        result = resp.json()

        task = result["result"]
        assert task["status"]["state"] == "input-required"
        print("PASS: test_skill_can_transition_to_input_required")

    def test_skill_transition_prevents_auto_complete(self) -> None:
        """When skill transitions task to non-'working' state, auto-complete doesn't override."""
        agent = _StateTransitionAgent(return_state="input-required")
        app = agent.build_app()
        client = TestClient(app, raise_server_exceptions=True)

        message = _build_message("test")
        body = _build_rpc_request("message/send", {"message": message})
        resp = client.post("/", json=body)
        result = resp.json()

        task = result["result"]
        # Should remain 'input-required', not auto-transitioned to 'completed'
        assert task["status"]["state"] == "input-required"
        print("PASS: test_skill_transition_prevents_auto_complete")


# ============================================================================
# Card Caching Tests
# ============================================================================

class TestCardCaching:
    """Test agent card caching behavior."""

    def test_card_cached_on_second_request(self) -> None:
        """Agent card is built once and cached."""

        class _CardCountingAgent(A2AAgent):
            def __init__(self):
                self.build_card_count = 0
                super().__init__()

            def _build_card(self) -> dict:
                self.build_card_count += 1
                return {
                    "name": "counting-agent",
                    "description": "Counts card builds",
                    "url": "http://0.0.0.0:9995",
                    "version": "1.0.0",
                    "protocolVersion": "0.3.0",
                    "preferredTransport": "JSONRPC",
                    "capabilities": {},
                    "skills": [{"id": "count", "name": "Count", "description": "", "tags": []}],
                }

            def _register_skills(self) -> None:
                self.register_skill("count", self._handler)

            async def _handler(self, message: dict, task: dict) -> dict:
                return _new_artifact("result", [_text_part("ok")])

        agent = _CardCountingAgent()
        app = agent.build_app()
        client = TestClient(app, raise_server_exceptions=True)

        # First request builds card
        resp1 = client.get("/.well-known/agent-card.json")
        assert resp1.status_code == 200
        assert agent.build_card_count == 1

        # Second request uses cached card
        resp2 = client.get("/.well-known/agent-card.json")
        assert resp2.status_code == 200
        assert agent.build_card_count == 1  # Still 1, not 2

        # Verify both responses are identical
        card1 = resp1.json()
        card2 = resp2.json()
        assert card1 == card2
        print("PASS: test_card_cached_on_second_request")


# ============================================================================
# Error Code Tests
# ============================================================================

class TestJsonRpcErrorCodes:
    """Test all JSON-RPC error code paths."""

    def test_parse_error_on_malformed_json(self) -> None:
        """Malformed JSON body returns PARSE_ERROR (-32700)."""
        agent = _TestAgent()
        app = agent.build_app()
        client = TestClient(app, raise_server_exceptions=True)

        # Send invalid JSON
        resp = client.post("/", content="{invalid json")
        assert resp.status_code == 400
        result = resp.json()

        assert "error" in result
        assert result["error"]["code"] == PARSE_ERROR
        print("PASS: test_parse_error_on_malformed_json")

    def test_invalid_request_non_dict_payload(self) -> None:
        """Non-dict JSON payload returns INVALID_REQUEST (-32600)."""
        agent = _TestAgent()
        app = agent.build_app()
        client = TestClient(app, raise_server_exceptions=True)

        # Send a list instead of dict
        resp = client.post("/", json=[1, 2, 3])
        assert resp.status_code == 400
        result = resp.json()

        assert "error" in result
        assert result["error"]["code"] == INVALID_REQUEST
        print("PASS: test_invalid_request_non_dict_payload")


# ============================================================================
# UCP Profile Route Tests
# ============================================================================

class TestUcpProfileRoute:
    """Test /.well-known/ucp route."""

    def test_ucp_profile_returns_empty_when_no_env_var(self) -> None:
        """UCP profile returns empty signing_keys when UCP_CLIENT_SIGNING_KEY_PATH not set."""
        agent = _TestAgent()
        app = agent.build_app()
        client = TestClient(app, raise_server_exceptions=True)

        # Ensure env var is not set
        with patch.dict(os.environ, {}, clear=True):
            resp = client.get("/.well-known/ucp")
            assert resp.status_code == 200
            result = resp.json()
            assert "signing_keys" in result
            assert result["signing_keys"] == []
        print("PASS: test_ucp_profile_returns_empty_when_no_env_var")


# ============================================================================
# UUID Uniqueness Tests
# ============================================================================

class TestUuidUniqueness:
    """Test that all generated UUIDs are unique."""

    def test_task_ids_unique(self) -> None:
        """Multiple new tasks get unique IDs."""
        task_ids = set()
        for _ in range(10):
            task = _new_task()
            assert task["id"] not in task_ids
            task_ids.add(task["id"])
        print("PASS: test_task_ids_unique")

    def test_artifact_ids_unique(self) -> None:
        """Multiple new artifacts get unique IDs."""
        artifact_ids = set()
        for _ in range(10):
            artifact = _new_artifact("test", [])
            assert artifact["artifactId"] not in artifact_ids
            artifact_ids.add(artifact["artifactId"])
        print("PASS: test_artifact_ids_unique")

    def test_message_ids_unique(self) -> None:
        """Multiple new messages get unique IDs."""
        message_ids = set()
        for _ in range(10):
            msg = _new_message("user", [], "task", "ctx")
            assert msg["messageId"] not in message_ids
            message_ids.add(msg["messageId"])
        print("PASS: test_message_ids_unique")


# ============================================================================
# Test Runner
# ============================================================================

TESTS = [
    # Helper function tests
    TestRpcErrorFunction.test_rpc_error_basic_without_data,
    TestRpcErrorFunction.test_rpc_error_with_data,
    TestRpcErrorFunction.test_rpc_error_with_none_id,
    TestRpcErrorFunction.test_rpc_error_numeric_id,
    TestRpcResultFunction.test_rpc_result_basic,
    TestRpcResultFunction.test_rpc_result_with_null_result,
    TestRpcResultFunction.test_rpc_result_with_list_result,
    TestNowIsoFunction.test_now_iso_format,
    TestNowIsoFunction.test_now_iso_utc,
    TestNowIsoFunction.test_now_iso_freshness,
    TestNewTaskFunction.test_new_task_default_context_id,
    TestNewTaskFunction.test_new_task_explicit_context_id,
    TestNewTaskFunction.test_new_task_unique_ids,
    TestNewTaskFunction.test_new_task_timestamp_present,
    TestTaskTransitionFunction.test_task_transition_state_change,
    TestTaskTransitionFunction.test_task_transition_timestamp_updated,
    TestNewMessageFunction.test_new_message_basic,
    TestNewMessageFunction.test_new_message_explicit_message_id,
    TestNewMessageFunction.test_new_message_generated_message_id,
    TestNewMessageFunction.test_new_message_agent_role,
    TestTextPartFunction.test_text_part_basic,
    TestTextPartFunction.test_text_part_empty_string,
    TestTextPartFunction.test_text_part_unicode,
    TestDataPartFunction.test_data_part_basic,
    TestDataPartFunction.test_data_part_list,
    TestDataPartFunction.test_data_part_scalar,
    TestNewArtifactFunction.test_new_artifact_basic,
    TestNewArtifactFunction.test_new_artifact_explicit_id,
    TestNewArtifactFunction.test_new_artifact_generated_id,
    TestNewArtifactFunction.test_new_artifact_unique_ids,

    # Multi-skill dispatch tests
    TestMultiSkillDispatch.test_multi_skill_explicit_skill_a,
    TestMultiSkillDispatch.test_multi_skill_explicit_skill_b,
    TestMultiSkillDispatch.test_multi_skill_no_skill_id_error,
    TestMultiSkillDispatch.test_multi_skill_unknown_skill_id,

    # Message/send edge cases
    TestMessageSendEdgeCases.test_invalid_role_non_user_or_agent,
    TestMessageSendEdgeCases.test_missing_role_defaults_to_user,
    TestMessageSendEdgeCases.test_artifact_without_artifact_id_gets_generated,
    TestMessageSendEdgeCases.test_message_without_message_id_gets_generated,

    # Task state and history tests
    TestTaskStateAndHistory.test_agent_reply_added_to_history,
    TestTaskStateAndHistory.test_skill_can_transition_to_input_required,
    TestTaskStateAndHistory.test_skill_transition_prevents_auto_complete,

    # Card caching tests
    TestCardCaching.test_card_cached_on_second_request,

    # Error code tests
    TestJsonRpcErrorCodes.test_parse_error_on_malformed_json,
    TestJsonRpcErrorCodes.test_invalid_request_non_dict_payload,

    # UCP profile tests
    TestUcpProfileRoute.test_ucp_profile_returns_empty_when_no_env_var,

    # UUID uniqueness tests
    TestUuidUniqueness.test_task_ids_unique,
    TestUuidUniqueness.test_artifact_ids_unique,
    TestUuidUniqueness.test_message_ids_unique,
]


def main() -> None:
    """Run all tests."""
    passed = 0
    failed = 0
    errors: list[tuple[str, Exception]] = []

    print(f"\n{'='*70}")
    print("Travel Guild — A2A Agent Module Coverage Test Suite")
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
