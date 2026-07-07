"""
a2a_agent.py — Reusable A2A (Agent2Agent) server base.

Wire contract: A2A protocol v0.3 / v1.0 JSON-RPC 2.0 over HTTP.
  - GET  /.well-known/agent-card.json  →  AgentCard (camelCase JSON)
  - POST /                             →  JSON-RPC 2.0 dispatcher
       method "message/send"  →  create Task, run skill, return Task
       method "tasks/get"     →  fetch Task by id

Task lifecycle (§1.1 of design doc, real A2A spec):
  submitted → working → completed | failed | rejected

  "failed"   — unexpected internal error / crash inside a skill handler.
  "rejected" — deliberate policy-level refusal (unknown skillId, guard check,
               authorization failure).  Raise SkillRejected to produce this state.

Data shapes (wire-format keys — camelCase, validated against a2a-sdk v0.3 types):
  AgentCard:  name, description, url, version, capabilities,
              defaultInputModes, defaultOutputModes, skills[], protocolVersion,
              preferredTransport
  Task:       id, contextId, status{state, timestamp}, history[], artifacts[], kind
  Message:    messageId, role, parts[], contextId, taskId, kind
  Part:       {kind:"text", text:...} | {kind:"data", data:...}
  Artifact:   artifactId, name, parts[]
  TaskState:  "submitted" | "working" | "input-required" | "completed"
              | "failed" | "canceled" | "rejected" | "auth-required"

Subclassing pattern:
    class MyAgent(A2AAgent):
        def _build_card(self) -> dict:
            return {...}  # camelCase AgentCard JSON

        def _register_skills(self):
            self.register_skill("skill-id", self._my_skill_handler)

        async def _my_skill_handler(self, message: dict, task: dict) -> dict:
            # message: the inbound Message (camelCase dict)
            # task:    the current Task (camelCase dict, mutable)
            # return:  Artifact dict {artifactId, name, parts[]}
            ...
"""

from __future__ import annotations

import asyncio
import datetime
import json
import logging
import uuid
from typing import Any, Callable, Awaitable

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Deliberate-refusal exception — policy-level decline (not a crash).
# Raising this inside a skill handler or dispatch produces task state
# "rejected" so consumers can distinguish a fail-closed policy decision
# from an unexpected internal error ("failed").
# ---------------------------------------------------------------------------
class SkillRejected(Exception):
    """Raised to signal a deliberate refusal by dispatch or a skill handler.

    Any code that intentionally declines a request (unknown skillId, policy
    guard, authorization check, etc.) should raise SkillRejected rather than
    a generic exception.  The framework will set the Task state to 'rejected'
    rather than 'failed', letting callers distinguish a deliberate policy
    decline from an unexpected internal crash.
    """


# ---------------------------------------------------------------------------
# JSON-RPC error codes (per spec §4.3 / JSON-RPC 2.0 standard)
# ---------------------------------------------------------------------------
PARSE_ERROR      = -32700
INVALID_REQUEST  = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS   = -32602
INTERNAL_ERROR   = -32603


def _rpc_error(rpc_id: Any, code: int, message: str, data: Any = None) -> dict:
    err: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        err["data"] = data
    return {"jsonrpc": "2.0", "id": rpc_id, "error": err}


def _rpc_result(rpc_id: Any, result: Any) -> dict:
    return {"jsonrpc": "2.0", "id": rpc_id, "result": result}


# ---------------------------------------------------------------------------
# Task factory helpers (all camelCase — the real A2A wire format)
# ---------------------------------------------------------------------------
_PROTOCOL_VERSION = "0.3.0"
_PREFERRED_TRANSPORT = "JSONRPC"


def _now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _new_task(context_id: str | None = None) -> dict:
    """Return a fresh Task in 'submitted' state."""
    return {
        "kind": "task",
        "id": str(uuid.uuid4()),
        "contextId": context_id or str(uuid.uuid4()),
        "status": {"state": "submitted", "timestamp": _now_iso()},
        "history": [],
        "artifacts": [],
        "metadata": {},
    }


def _task_transition(task: dict, state: str) -> None:
    task["status"] = {"state": state, "timestamp": _now_iso()}


def _new_message(role: str, parts: list[dict], task_id: str, context_id: str,
                 message_id: str | None = None) -> dict:
    return {
        "kind": "message",
        "messageId": message_id or str(uuid.uuid4()),
        "role": role,
        "parts": parts,
        "taskId": task_id,
        "contextId": context_id,
    }


def _text_part(text: str) -> dict:
    return {"kind": "text", "text": text}


def _data_part(data: Any) -> dict:
    return {"kind": "data", "data": data}


def _new_artifact(name: str, parts: list[dict],
                  artifact_id: str | None = None) -> dict:
    return {
        "artifactId": artifact_id or str(uuid.uuid4()),
        "name": name,
        "parts": parts,
    }


# ---------------------------------------------------------------------------
# Base A2A agent class
# ---------------------------------------------------------------------------
SkillHandler = Callable[[dict, dict], Awaitable[dict]]


class A2AAgent:
    """
    Reusable A2A server base.

    Subclass, override _build_card() and _register_skills(), then call
    build_app() to get a Starlette ASGI application.
    """

    def __init__(self) -> None:
        self._skills: dict[str, SkillHandler] = {}
        self._tasks: dict[str, dict] = {}          # in-memory task store
        self._card: dict | None = None
        self._register_skills()

    # ------------------------------------------------------------------
    # Subclass API
    # ------------------------------------------------------------------

    def _build_card(self) -> dict:
        """Return the AgentCard as a camelCase dict. Override in subclass."""
        raise NotImplementedError("Subclass must implement _build_card()")

    def _register_skills(self) -> None:
        """Call register_skill(...) for each skill. Override in subclass."""
        pass

    def register_skill(self, skill_id: str, handler: SkillHandler) -> None:
        """Register a skill handler.  handler(message_dict, task_dict) → Artifact dict."""
        self._skills[skill_id] = handler

    # ------------------------------------------------------------------
    # Skill dispatch: find the right skill from a message/send call.
    # The design does NOT pre-route by skill_id in the RPC call;
    # instead the first matching skill is used (one agent = one skill in M0).
    # For multi-skill agents we look at the 'skillId' metadata field.
    # ------------------------------------------------------------------

    async def _dispatch(self, message: dict, task: dict) -> dict:
        """Choose and invoke a skill handler; return Artifact or raise.

        Dispatch rules (fail-closed contract):
          1. If a non-empty skillId is supplied:
             a. If it matches a registered skill → invoke it.
             b. If it does NOT match → raise immediately (fail closed).
                Never silently fall through to a different skill.
          2. If no skillId is supplied:
             a. If exactly one skill is registered → invoke it (M0 fallback).
             b. If multiple skills → raise (caller must supply skillId).
          3. If no skills are registered → raise.
        """
        skill_id = (message.get("metadata") or {}).get("skillId")
        if skill_id:
            # Caller explicitly requested a skill — fail closed if not found.
            if skill_id in self._skills:
                return await self._skills[skill_id](message, task)
            raise SkillRejected(
                f"Unknown skillId {skill_id!r}. "
                f"Available: {sorted(self._skills)}"
            )
        # No skillId supplied — apply fallback rules.
        if not self._skills:
            raise SkillRejected("No skills registered")
        # Fallback: use the single registered skill (correct for M0 echo agent)
        if len(self._skills) == 1:
            handler = next(iter(self._skills.values()))
            return await handler(message, task)
        raise SkillRejected(
            f"Multiple skills registered; caller must supply metadata.skillId. "
            f"Available: {sorted(self._skills)}"
        )

    # ------------------------------------------------------------------
    # JSON-RPC method handlers
    # ------------------------------------------------------------------

    async def _handle_message_send(self, rpc_id: Any, params: dict) -> dict:
        """
        message/send — create a Task, run the matching skill, return Task.

        Params shape (camelCase, per A2A v0.3 wire):
          { "message": { "messageId": "...", "role": "user", "parts": [...],
                         "contextId": "..." (optional) },
            "configuration": { ... } (optional) }
        """
        message = params.get("message")
        if not message or not isinstance(message, dict):
            return _rpc_error(rpc_id, INVALID_PARAMS,
                              "params.message is required")
        role = message.get("role", "user")
        if role not in ("user", "agent"):
            return _rpc_error(rpc_id, INVALID_PARAMS,
                              f"message.role must be 'user' or 'agent', got {role!r}")
        parts = message.get("parts")
        if not isinstance(parts, list) or not parts:
            return _rpc_error(rpc_id, INVALID_PARAMS,
                              "message.parts must be a non-empty list")

        context_id = message.get("contextId")
        task = _new_task(context_id)
        task_id = task["id"]
        task["contextId"] = context_id or task["contextId"]

        # Stamp inbound message with task/context ids and add to history
        msg = dict(message)
        msg["taskId"] = task_id
        msg["contextId"] = task["contextId"]
        msg.setdefault("messageId", str(uuid.uuid4()))
        msg.setdefault("kind", "message")
        task["history"].append(msg)

        # Transition: submitted → working
        _task_transition(task, "working")
        self._tasks[task_id] = task

        try:
            artifact = await self._dispatch(message, task)
            # Ensure artifact has required fields
            if "artifactId" not in artifact:
                artifact["artifactId"] = str(uuid.uuid4())

            task["artifacts"].append(artifact)

            # Add agent reply message to history
            agent_reply = _new_message(
                role="agent",
                parts=artifact.get("parts", []),
                task_id=task_id,
                context_id=task["contextId"],
            )
            task["history"].append(agent_reply)

            # Skill handler may have already transitioned the task to a
            # non-working terminal/pause state (e.g. "input-required").
            # Only auto-complete if still in "working" state.
            if task["status"]["state"] == "working":
                _task_transition(task, "completed")
        except SkillRejected as exc:
            # Deliberate policy-level refusal (e.g. unknown skillId, guard check).
            # Use "rejected" so consumers can distinguish a fail-closed decline
            # from an unexpected crash.  Do NOT log as an error — this is
            # intentional behaviour, not a system fault.
            logger.warning("Skill request rejected for task %s: %s", task_id, exc)
            _task_transition(task, "rejected")
            task["metadata"]["error"] = str(exc)
            task["metadata"]["outcome"] = "rejected"
        except Exception as exc:
            # Unexpected internal error / crash inside a skill handler.
            logger.exception("Skill dispatch failed for task %s", task_id)
            _task_transition(task, "failed")
            task["metadata"]["error"] = str(exc)
            task["metadata"]["outcome"] = "error"

        return _rpc_result(rpc_id, task)

    async def _handle_tasks_get(self, rpc_id: Any, params: dict) -> dict:
        """
        tasks/get — return a stored Task by id.

        Params shape: { "id": "<task-id>" }
        """
        task_id = params.get("id")
        if not task_id or not isinstance(task_id, str):
            return _rpc_error(rpc_id, INVALID_PARAMS,
                              "params.id (task id) is required")
        task = self._tasks.get(task_id)
        if task is None:
            return _rpc_error(rpc_id, INVALID_PARAMS,
                              f"Task {task_id!r} not found",
                              data={"taskId": task_id})
        return _rpc_result(rpc_id, task)

    # ------------------------------------------------------------------
    # HTTP handlers (Starlette routes)
    # ------------------------------------------------------------------

    async def _route_agent_card(self, request: Request) -> Response:
        if self._card is None:
            self._card = self._build_card()
        return JSONResponse(self._card)

    async def _route_ucp_profile(self, request: Request) -> Response:
        # #53 — publish this agent's RFC 9421 signing key as an EC P-256 JWK set so
        # the UCP merchant's key resolver can VERIFY our signed requests (completes
        # the live NHI loop: client signs -> merchant fetches this profile -> grants
        # the server-side tier). FAIL-CONSERVATIVE: returns an empty signing_keys
        # array when no key is configured or on any error (honest "unsigned", never
        # a crash, never a fabricated key).
        try:
            import os as _os
            from utils import ucp_signing
            key_path = _os.environ.get("UCP_CLIENT_SIGNING_KEY_PATH")
            if not key_path:
                return JSONResponse({"signing_keys": []})
            priv = ucp_signing.load_or_create_key(key_path)
            kid = _os.environ.get("UCP_CLIENT_KID") or ucp_signing.derive_kid(priv)
            return JSONResponse(ucp_signing.agent_profile_document(priv, kid))
        except Exception:  # noqa: BLE001 — never crash key publication
            return JSONResponse({"signing_keys": []})

    async def _route_rpc(self, request: Request) -> Response:
        # Parse body
        try:
            body = await request.body()
            payload = json.loads(body)
        except Exception:
            return JSONResponse(
                _rpc_error(None, PARSE_ERROR, "Parse error"),
                status_code=400,
            )

        # Validate JSON-RPC envelope
        if not isinstance(payload, dict):
            return JSONResponse(
                _rpc_error(None, INVALID_REQUEST, "Request must be a JSON object"),
                status_code=400,
            )

        rpc_id = payload.get("id")
        jsonrpc = payload.get("jsonrpc")
        if jsonrpc != "2.0":
            return JSONResponse(
                _rpc_error(rpc_id, INVALID_REQUEST,
                           f"jsonrpc must be '2.0', got {jsonrpc!r}"),
                status_code=400,
            )

        method = payload.get("method")
        params = payload.get("params") or {}

        if method == "message/send":
            result = await self._handle_message_send(rpc_id, params)
        elif method == "tasks/get":
            result = await self._handle_tasks_get(rpc_id, params)
        else:
            result = _rpc_error(
                rpc_id, METHOD_NOT_FOUND,
                f"Method {method!r} not found. Supported: message/send, tasks/get",
            )

        return JSONResponse(result)

    # ------------------------------------------------------------------
    # ASGI app builder
    # ------------------------------------------------------------------

    def build_app(self) -> Starlette:
        """Return the Starlette ASGI application."""
        routes = [
            Route("/.well-known/agent-card.json",
                  self._route_agent_card, methods=["GET"]),
            # Legacy path still referenced by design doc (v0.2 convention)
            Route("/.well-known/agent.json",
                  self._route_agent_card, methods=["GET"]),
            # #53 — RFC 9421 signing-key publication for UCP merchant verification.
            Route("/.well-known/ucp", self._route_ucp_profile, methods=["GET"]),
            # JSON-RPC endpoint: real A2A spec uses "/" as DEFAULT_RPC_URL
            Route("/", self._route_rpc, methods=["POST"]),
        ]
        return Starlette(routes=routes)
