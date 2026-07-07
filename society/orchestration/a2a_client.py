"""
a2a_client.py — Minimal A2A client helper.

Public API:
    send(base_url, text, *, context_id=None, timeout=30.0) -> str
        POST message/send to the agent at base_url,
        poll tasks/get until the Task reaches a terminal state,
        return the text from the first text artifact part.

    send_raw(base_url, message_dict, *, context_id=None, timeout=30.0) -> dict
        Low-level: send a fully constructed Message dict, return the final Task dict.

    get_card(base_url, *, timeout=10.0) -> dict
        Fetch and return the Agent Card JSON.

Terminal Task states (A2A v0.3 wire values):
    completed | failed | canceled | rejected

The client polls tasks/get with a short backoff if message/send returns a
non-terminal Task (e.g. 'working' or 'input-required').  For M0 the echo
agent always completes synchronously, so no poll is needed in practice,
but the polling logic is included for correctness and for use by the
orchestrator in M2+.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from typing import Any

import httpx


# A2A v0.3 terminal states (wire-format strings)
TERMINAL_STATES = frozenset({"completed", "failed", "canceled", "rejected"})

# Poll settings
_DEFAULT_POLL_INTERVAL = 0.25   # seconds between polls
_DEFAULT_MAX_POLLS     = 40     # ≈ 10 s of polling at 0.25 s


# ---------------------------------------------------------------------------
# Low-level helpers
# ---------------------------------------------------------------------------

def _build_rpc_request(method: str, params: dict[str, Any]) -> dict:
    return {
        "jsonrpc": "2.0",
        "id": str(uuid.uuid4()),
        "method": method,
        "params": params,
    }


def _build_message(text: str, context_id: str | None = None) -> dict:
    """Build a minimal user Message with a single text Part (camelCase wire format)."""
    msg: dict[str, Any] = {
        "kind": "message",
        "messageId": str(uuid.uuid4()),
        "role": "user",
        "parts": [{"kind": "text", "text": text}],
    }
    if context_id:
        msg["contextId"] = context_id
    return msg


def _extract_result_text(task: dict) -> str:
    """
    Extract the first text string from the task's artifacts.
    Falls back to a JSON representation of the first artifact if no text found.
    Raises RuntimeError for failed/rejected tasks.
    """
    import json as _json

    state = (task.get("status") or {}).get("state", "unknown")
    if state in ("failed", "rejected", "canceled"):
        err = (task.get("metadata") or {}).get("error", "")
        raise RuntimeError(
            f"Task {task.get('id')!r} ended in state {state!r}. "
            + (f"Error: {err}" if err else "")
        )

    for artifact in task.get("artifacts") or []:
        for part in artifact.get("parts") or []:
            if part.get("kind") == "text":
                return part["text"]
            if part.get("kind") == "data":
                return _json.dumps(part["data"])
    return ""


# ---------------------------------------------------------------------------
# Synchronous client (used by tests + orchestrator in non-async contexts)
# ---------------------------------------------------------------------------

def get_card(base_url: str, *, timeout: float = 10.0) -> dict:
    """Fetch the Agent Card from /.well-known/agent-card.json."""
    url = base_url.rstrip("/") + "/.well-known/agent-card.json"
    with httpx.Client(timeout=timeout) as client:
        resp = client.get(url)
        resp.raise_for_status()
        return resp.json()


def send_raw(
    base_url: str,
    message: dict,
    *,
    context_id: str | None = None,
    timeout: float = 30.0,
    poll_interval: float = _DEFAULT_POLL_INTERVAL,
    max_polls: int = _DEFAULT_MAX_POLLS,
) -> dict:
    """
    POST message/send; poll tasks/get until terminal; return final Task dict.

    Args:
        base_url:  Agent's base URL (e.g. "http://localhost:9100").
        message:   Full Message dict (camelCase, must have role + parts).
        context_id: Optional contextId to embed in the message.
        timeout:   HTTP request timeout in seconds.
        poll_interval: Seconds between polls.
        max_polls: Maximum number of tasks/get polls before giving up.
    Returns:
        The final Task dict (camelCase).
    Raises:
        httpx.HTTPStatusError: on non-2xx HTTP responses.
        RuntimeError: if max polls exceeded or RPC error returned.
    """
    rpc_url = base_url.rstrip("/") + "/"

    if context_id and "contextId" not in message:
        message = dict(message, contextId=context_id)

    with httpx.Client(timeout=timeout) as client:
        # --- message/send ---
        req_body = _build_rpc_request("message/send", {"message": message})
        resp = client.post(rpc_url, json=req_body)
        resp.raise_for_status()
        rpc_resp = resp.json()

        if "error" in rpc_resp:
            raise RuntimeError(
                f"message/send RPC error: {rpc_resp['error']}"
            )

        task = rpc_resp.get("result")
        if not isinstance(task, dict):
            raise RuntimeError(
                f"message/send returned unexpected result: {rpc_resp!r}"
            )

        # --- Poll until terminal state ---
        task_id = task.get("id")
        polls = 0
        while True:
            state = (task.get("status") or {}).get("state", "")
            if state in TERMINAL_STATES or state == "input-required":
                break
            if polls >= max_polls:
                raise RuntimeError(
                    f"Task {task_id!r} still in state {state!r} after "
                    f"{max_polls} polls"
                )
            time.sleep(poll_interval)
            polls += 1

            poll_req = _build_rpc_request("tasks/get", {"id": task_id})
            poll_resp = client.post(rpc_url, json=poll_req)
            poll_resp.raise_for_status()
            poll_rpc = poll_resp.json()
            if "error" in poll_rpc:
                raise RuntimeError(f"tasks/get RPC error: {poll_rpc['error']}")
            task = poll_rpc.get("result", task)

        return task


def send(
    base_url: str,
    text: str,
    *,
    context_id: str | None = None,
    timeout: float = 30.0,
) -> str:
    """
    High-level convenience: send text, return result text from artifact.

    Args:
        base_url:   Agent's base URL (e.g. "http://localhost:9100").
        text:       Input text to send.
        context_id: Optional A2A contextId (trip id in the orchestrator).
        timeout:    HTTP request timeout in seconds.
    Returns:
        Text content of the first text artifact part.
    Raises:
        RuntimeError: if the task failed/rejected, or no text artifact found.
    """
    message = _build_message(text, context_id=context_id)
    task = send_raw(base_url, message, context_id=context_id, timeout=timeout)
    return _extract_result_text(task)


# ---------------------------------------------------------------------------
# Async variants (for use inside async orchestrator in M2+)
# ---------------------------------------------------------------------------

async def async_get_card(base_url: str, *, timeout: float = 10.0) -> dict:
    url = base_url.rstrip("/") + "/.well-known/agent-card.json"
    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.get(url)
        resp.raise_for_status()
        return resp.json()


async def async_send_raw(
    base_url: str,
    message: dict,
    *,
    context_id: str | None = None,
    timeout: float = 30.0,
    poll_interval: float = _DEFAULT_POLL_INTERVAL,
    max_polls: int = _DEFAULT_MAX_POLLS,
) -> dict:
    """Async variant of send_raw."""
    rpc_url = base_url.rstrip("/") + "/"

    if context_id and "contextId" not in message:
        message = dict(message, contextId=context_id)

    async with httpx.AsyncClient(timeout=timeout) as client:
        req_body = _build_rpc_request("message/send", {"message": message})
        resp = await client.post(rpc_url, json=req_body)
        resp.raise_for_status()
        rpc_resp = resp.json()

        if "error" in rpc_resp:
            raise RuntimeError(f"message/send RPC error: {rpc_resp['error']}")

        task = rpc_resp.get("result")
        if not isinstance(task, dict):
            raise RuntimeError(
                f"message/send returned unexpected result: {rpc_resp!r}"
            )

        task_id = task.get("id")
        polls = 0
        while True:
            state = (task.get("status") or {}).get("state", "")
            if state in TERMINAL_STATES or state == "input-required":
                break
            if polls >= max_polls:
                raise RuntimeError(
                    f"Task {task_id!r} still in state {state!r} after "
                    f"{max_polls} polls"
                )
            await asyncio.sleep(poll_interval)
            polls += 1
            poll_req = _build_rpc_request("tasks/get", {"id": task_id})
            poll_resp = await client.post(rpc_url, json=poll_req)
            poll_resp.raise_for_status()
            poll_rpc = poll_resp.json()
            if "error" in poll_rpc:
                raise RuntimeError(f"tasks/get RPC error: {poll_rpc['error']}")
            task = poll_rpc.get("result", task)

        return task


async def async_send(
    base_url: str,
    text: str,
    *,
    context_id: str | None = None,
    timeout: float = 30.0,
) -> str:
    """Async variant of send."""
    message = _build_message(text, context_id=context_id)
    task = await async_send_raw(base_url, message, context_id=context_id,
                                timeout=timeout)
    return _extract_result_text(task)
