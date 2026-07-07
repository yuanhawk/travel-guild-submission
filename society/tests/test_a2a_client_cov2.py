"""
test_a2a_client_cov2.py — Coverage for orchestration/a2a_client.py

Deterministic, var-0-safe tests for the uncovered line ranges of
society/orchestration/a2a_client.py. No real network, no LLM, no paid APIs:
all HTTP is mocked at the httpx.Client / httpx.AsyncClient layer, and both
time.sleep and asyncio.sleep are stubbed so polling loops run instantly.

Targeted regions:
  * _extract_result_text (lines 79-92): failed/rejected/canceled error states,
    error-message formatting, empty/absent artifacts, data-part JSON fallback,
    first-text-wins ordering.
  * get_card (lines 101-105): URL trailing-slash strip, timeout, raise_for_status.
  * send_raw (lines 133-179): context_id merge / preserve, RPC error handling,
    non-dict result, polling loop to terminal, input-required break, max_polls,
    poll RPC error.
  * send (lines 202-204): _build_message -> send_raw -> _extract_result_text.
  * async_get_card (lines 212-216): real execution via asyncio.run + mocked
    AsyncClient.
  * async_send_raw (lines 229-270): real async send + poll, context_id, errors,
    max_polls, input-required break.
  * async_send (lines 281-284): real async wrapper execution.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from contextlib import asynccontextmanager
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(__file__))

import httpx
import pytest

from orchestration.a2a_client import (
    TERMINAL_STATES,
    _build_message,
    _build_rpc_request,
    _extract_result_text,
    async_get_card,
    async_send,
    async_send_raw,
    get_card,
    send,
    send_raw,
)


# ---------------------------------------------------------------------------
# Sync httpx.Client mock helpers
# ---------------------------------------------------------------------------

def _sync_resp(json_value):
    """A MagicMock standing in for an httpx.Response with the given .json()."""
    resp = MagicMock(spec=httpx.Response)
    resp.json.return_value = json_value
    resp.raise_for_status.return_value = None
    return resp


def _patch_sync_client(*, get_value=None, post_router=None):
    """
    Patch orchestration.a2a_client.httpx.Client with a context-manager mock.

    get_value:   value returned by resp.json() for client.get().
    post_router: callable(method, json_body) -> json-dict for client.post().
    Returns the (patcher_ctx, mock_client) so callers can inspect calls.
    """
    mock_client = MagicMock()
    mock_client.__enter__.return_value = mock_client
    mock_client.__exit__.return_value = None

    if get_value is not None:
        mock_client.get.return_value = _sync_resp(get_value)

    if post_router is not None:
        def _post(url, json=None):
            method = (json or {}).get("method", "")
            return _sync_resp(post_router(method, json))
        mock_client.post.side_effect = _post

    patcher = patch("orchestration.a2a_client.httpx.Client", return_value=mock_client)
    return patcher, mock_client


# ---------------------------------------------------------------------------
# Async httpx.AsyncClient mock helpers
# ---------------------------------------------------------------------------

class _FakeAsyncResp:
    def __init__(self, json_value):
        self._json = json_value

    def json(self):
        return self._json

    def raise_for_status(self):
        return None


class _FakeAsyncClient:
    """Minimal stand-in for httpx.AsyncClient usable as an async context mgr."""

    def __init__(self, *, get_value=None, post_router=None, recorder=None):
        self._get_value = get_value
        self._post_router = post_router
        self._rec = recorder if recorder is not None else {}
        self._rec.setdefault("get_calls", [])
        self._rec.setdefault("post_calls", [])

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get(self, url):
        self._rec["get_calls"].append(url)
        return _FakeAsyncResp(self._get_value)

    async def post(self, url, json=None):
        self._rec["post_calls"].append((url, json))
        method = (json or {}).get("method", "")
        return _FakeAsyncResp(self._post_router(method, json))


def _patch_async_client(factory):
    """Patch orchestration.a2a_client.httpx.AsyncClient with `factory(**kwargs)`."""
    return patch("orchestration.a2a_client.httpx.AsyncClient", side_effect=factory)


# ===========================================================================
# _extract_result_text — lines 79-92
# ===========================================================================

class TestExtractResultText:
    def test_failed_state_with_error_appends_error_text(self):
        task = {
            "id": "t-fail",
            "status": {"state": "failed"},
            "metadata": {"error": "skill blew up"},
        }
        with pytest.raises(RuntimeError) as exc:
            _extract_result_text(task)
        msg = str(exc.value)
        assert "t-fail" in msg and "failed" in msg
        assert "Error: skill blew up" in msg

    def test_rejected_state_raises(self):
        task = {"id": "t-rej", "status": {"state": "rejected"},
                "metadata": {"error": "policy"}}
        with pytest.raises(RuntimeError) as exc:
            _extract_result_text(task)
        assert "rejected" in str(exc.value) and "policy" in str(exc.value)

    def test_canceled_state_raises(self):
        task = {"id": "t-can", "status": {"state": "canceled"}}
        with pytest.raises(RuntimeError) as exc:
            _extract_result_text(task)
        assert "canceled" in str(exc.value)

    def test_failed_state_without_error_omits_error_prefix(self):
        # Lines 80-83: err is falsy -> no "Error: " suffix.
        task = {"id": "t-bare", "status": {"state": "failed"}}
        with pytest.raises(RuntimeError) as exc:
            _extract_result_text(task)
        assert "Error:" not in str(exc.value)

    def test_text_part_returned(self):
        task = {
            "status": {"state": "completed"},
            "artifacts": [{"parts": [{"kind": "text", "text": "hello"}]}],
        }
        assert _extract_result_text(task) == "hello"

    def test_absent_artifacts_returns_empty(self):
        # Line 86: `task.get("artifacts") or []` with no artifacts key.
        task = {"status": {"state": "completed"}}
        assert _extract_result_text(task) == ""

    def test_empty_artifacts_and_empty_parts_returns_empty(self):
        # Line 86-87: iterate empty artifacts list and artifact w/ no parts.
        task = {
            "status": {"state": "completed"},
            "artifacts": [{"parts": []}, {}],
        }
        assert _extract_result_text(task) == ""

    def test_data_part_json_fallback(self):
        # Lines 90-92: non-text 'data' part is JSON-serialized.
        payload = {"b": 2, "a": 1, "nested": {"x": [1, 2]}}
        task = {
            "status": {"state": "completed"},
            "artifacts": [{"parts": [{"kind": "data", "data": payload}]}],
        }
        out = _extract_result_text(task)
        assert json.loads(out) == payload

    def test_text_preferred_when_text_part_first(self):
        task = {
            "status": {"state": "completed"},
            "artifacts": [{"parts": [
                {"kind": "text", "text": "the-text"},
                {"kind": "data", "data": {"x": 1}},
            ]}],
        }
        assert _extract_result_text(task) == "the-text"

    def test_unknown_state_not_treated_as_error(self):
        # Line 78: missing status -> "unknown" default, not terminal.
        task = {"artifacts": [{"parts": [{"kind": "text", "text": "ok"}]}]}
        assert _extract_result_text(task) == "ok"


# ===========================================================================
# get_card — lines 101-105
# ===========================================================================

class TestGetCard:
    def test_success_and_url_strip(self):
        card = {"name": "agent-x"}
        patcher, mock_client = _patch_sync_client(get_value=card)
        with patcher:
            result = get_card("http://localhost:9100///")
        assert result == card
        called_url = mock_client.get.call_args[0][0]
        assert called_url == "http://localhost:9100/.well-known/agent-card.json"

    def test_timeout_passed_through(self):
        patcher, mock_client = _patch_sync_client(get_value={"name": "a"})
        with patcher as MockClient:
            get_card("http://h", timeout=3.5)
        MockClient.assert_called_once_with(timeout=3.5)

    def test_raise_for_status_propagates(self):
        mock_client = MagicMock()
        mock_client.__enter__.return_value = mock_client
        mock_client.__exit__.return_value = None
        bad = MagicMock(spec=httpx.Response)
        bad.raise_for_status.side_effect = httpx.HTTPStatusError(
            "boom", request=MagicMock(), response=bad
        )
        mock_client.get.return_value = bad
        with patch("orchestration.a2a_client.httpx.Client", return_value=mock_client):
            with pytest.raises(httpx.HTTPStatusError):
                get_card("http://h")


# ===========================================================================
# send_raw — lines 133-179
# ===========================================================================

class TestSendRaw:
    def _completed_task(self, tid="t1", text="done"):
        return {
            "id": tid,
            "status": {"state": "completed"},
            "artifacts": [{"parts": [{"kind": "text", "text": text}]}],
        }

    def test_immediate_completion_no_poll(self):
        def router(method, body):
            assert method == "message/send"
            return {"result": self._completed_task()}

        patcher, mock_client = _patch_sync_client(post_router=router)
        with patcher:
            msg = _build_message("hi")
            task = send_raw("http://localhost:9100", msg)
        assert task["status"]["state"] == "completed"
        assert mock_client.post.call_count == 1  # send only, no poll

    def test_rpc_url_single_trailing_slash(self):
        def router(method, body):
            return {"result": self._completed_task()}

        patcher, mock_client = _patch_sync_client(post_router=router)
        with patcher:
            send_raw("http://localhost:9100///", _build_message("x"))
        url = mock_client.post.call_args[0][0]
        assert url == "http://localhost:9100/"

    def test_context_id_merged_when_absent(self):
        captured = {}

        def router(method, body):
            captured["msg"] = body["params"]["message"]
            return {"result": self._completed_task()}

        patcher, _ = _patch_sync_client(post_router=router)
        with patcher:
            msg = {"kind": "message", "parts": [{"kind": "text", "text": "hi"}]}
            send_raw("http://h", msg, context_id="ctx-9")
        assert captured["msg"]["contextId"] == "ctx-9"

    def test_existing_context_id_preserved(self):
        captured = {}

        def router(method, body):
            captured["msg"] = body["params"]["message"]
            return {"result": self._completed_task()}

        patcher, _ = _patch_sync_client(post_router=router)
        with patcher:
            msg = {"kind": "message", "contextId": "orig",
                   "parts": [{"kind": "text", "text": "hi"}]}
            send_raw("http://h", msg, context_id="new")
        assert captured["msg"]["contextId"] == "orig"

    def test_send_rpc_error_raises(self):
        def router(method, body):
            return {"error": {"code": -32600, "message": "bad"}}

        patcher, _ = _patch_sync_client(post_router=router)
        with patcher:
            with pytest.raises(RuntimeError) as exc:
                send_raw("http://h", _build_message("x"))
        assert "message/send RPC error" in str(exc.value)

    def test_non_dict_result_raises(self):
        def router(method, body):
            return {"result": "not-a-dict"}

        patcher, _ = _patch_sync_client(post_router=router)
        with patcher:
            with pytest.raises(RuntimeError) as exc:
                send_raw("http://h", _build_message("x"))
        assert "unexpected result" in str(exc.value)

    def test_polls_until_terminal(self):
        calls = {"n": 0}

        def router(method, body):
            if method == "message/send":
                return {"result": {"id": "tp", "status": {"state": "working"},
                                   "artifacts": []}}
            calls["n"] += 1
            if calls["n"] >= 2:
                return {"result": self._completed_task("tp")}
            return {"result": {"id": "tp", "status": {"state": "working"},
                               "artifacts": []}}

        patcher, mock_client = _patch_sync_client(post_router=router)
        with patcher, patch("orchestration.a2a_client.time.sleep") as slp:
            task = send_raw("http://h", _build_message("x"), poll_interval=0.01)
        assert task["status"]["state"] == "completed"
        assert slp.called
        assert mock_client.post.call_count >= 3  # send + >=2 polls

    def test_input_required_breaks_loop(self):
        def router(method, body):
            if method == "message/send":
                return {"result": {"id": "ti",
                                   "status": {"state": "input-required"},
                                   "artifacts": []}}
            raise AssertionError("should not poll after input-required")

        patcher, mock_client = _patch_sync_client(post_router=router)
        with patcher:
            task = send_raw("http://h", _build_message("x"))
        assert task["status"]["state"] == "input-required"
        assert mock_client.post.call_count == 1

    def test_max_polls_exceeded_raises(self):
        def router(method, body):
            return {"result": {"id": "ts", "status": {"state": "working"},
                               "artifacts": []}}

        patcher, _ = _patch_sync_client(post_router=router)
        with patcher, patch("orchestration.a2a_client.time.sleep"):
            with pytest.raises(RuntimeError) as exc:
                send_raw("http://h", _build_message("x"), max_polls=2)
        assert "after 2 polls" in str(exc.value)

    def test_poll_rpc_error_raises(self):
        def router(method, body):
            if method == "message/send":
                return {"result": {"id": "te", "status": {"state": "working"},
                                   "artifacts": []}}
            return {"error": {"code": -1, "message": "poll-fail"}}

        patcher, _ = _patch_sync_client(post_router=router)
        with patcher, patch("orchestration.a2a_client.time.sleep"):
            with pytest.raises(RuntimeError) as exc:
                send_raw("http://h", _build_message("x"))
        assert "tasks/get RPC error" in str(exc.value)


# ===========================================================================
# send — lines 202-204
# ===========================================================================

class TestSend:
    def test_send_end_to_end_text(self):
        def router(method, body):
            return {"result": {
                "id": "t",
                "status": {"state": "completed"},
                "artifacts": [{"parts": [{"kind": "text", "text": "reply"}]}],
            }}

        patcher, _ = _patch_sync_client(post_router=router)
        with patcher:
            out = send("http://h", "hello", context_id="c1", timeout=12.0)
        assert out == "reply"

    def test_send_passes_through_to_send_raw(self):
        captured = {}

        def fake_send_raw(base_url, message, *, context_id=None, timeout=30.0):
            captured.update(base_url=base_url, message=message,
                            context_id=context_id, timeout=timeout)
            return {"status": {"state": "completed"},
                    "artifacts": [{"parts": [{"kind": "text", "text": "ok"}]}]}

        with patch("orchestration.a2a_client.send_raw", side_effect=fake_send_raw):
            out = send("http://h", "txt", context_id="cid", timeout=7.0)
        assert out == "ok"
        assert captured["context_id"] == "cid"
        assert captured["timeout"] == 7.0
        assert captured["message"]["parts"][0]["text"] == "txt"

    def test_send_propagates_error_state(self):
        def router(method, body):
            return {"result": {"id": "t", "status": {"state": "failed"},
                               "metadata": {"error": "x"}, "artifacts": []}}

        patcher, _ = _patch_sync_client(post_router=router)
        with patcher:
            with pytest.raises(RuntimeError) as exc:
                send("http://h", "txt")
        assert "failed" in str(exc.value)


# ===========================================================================
# async_get_card — lines 212-216  (real execution)
# ===========================================================================

class TestAsyncGetCard:
    def test_async_get_card_executes(self):
        rec = {}
        card = {"name": "async-agent"}

        def factory(**kwargs):
            return _FakeAsyncClient(get_value=card, recorder=rec)

        with _patch_async_client(factory):
            out = asyncio.run(async_get_card("http://localhost:9100/", timeout=4.0))
        assert out == card
        assert rec["get_calls"] == [
            "http://localhost:9100/.well-known/agent-card.json"
        ]


# ===========================================================================
# async_send_raw — lines 229-270  (real execution)
# ===========================================================================

class TestAsyncSendRaw:
    def _completed(self, tid="t", text="done"):
        return {"id": tid, "status": {"state": "completed"},
                "artifacts": [{"parts": [{"kind": "text", "text": text}]}]}

    def test_immediate_completion(self):
        rec = {}

        def router(method, body):
            return {"result": self._completed()}

        def factory(**kwargs):
            return _FakeAsyncClient(post_router=router, recorder=rec)

        with _patch_async_client(factory):
            task = asyncio.run(async_send_raw("http://h", _build_message("x")))
        assert task["status"]["state"] == "completed"
        assert len(rec["post_calls"]) == 1
        # URL normalization
        assert rec["post_calls"][0][0] == "http://h/"

    def test_context_id_merge(self):
        rec = {}

        def router(method, body):
            return {"result": self._completed()}

        def factory(**kwargs):
            return _FakeAsyncClient(post_router=router, recorder=rec)

        with _patch_async_client(factory):
            msg = {"kind": "message", "parts": [{"kind": "text", "text": "hi"}]}
            asyncio.run(async_send_raw("http://h", msg, context_id="ctxA"))
        sent_msg = rec["post_calls"][0][1]["params"]["message"]
        assert sent_msg["contextId"] == "ctxA"

    def test_send_rpc_error(self):
        def router(method, body):
            return {"error": {"message": "bad"}}

        def factory(**kwargs):
            return _FakeAsyncClient(post_router=router)

        with _patch_async_client(factory):
            with pytest.raises(RuntimeError) as exc:
                asyncio.run(async_send_raw("http://h", _build_message("x")))
        assert "message/send RPC error" in str(exc.value)

    def test_non_dict_result(self):
        def router(method, body):
            return {"result": 123}

        def factory(**kwargs):
            return _FakeAsyncClient(post_router=router)

        with _patch_async_client(factory):
            with pytest.raises(RuntimeError) as exc:
                asyncio.run(async_send_raw("http://h", _build_message("x")))
        assert "unexpected result" in str(exc.value)

    def test_polls_until_terminal(self):
        state = {"n": 0}

        def router(method, body):
            if method == "message/send":
                return {"result": {"id": "tp", "status": {"state": "working"},
                                   "artifacts": []}}
            state["n"] += 1
            if state["n"] >= 2:
                return {"result": self._completed("tp")}
            return {"result": {"id": "tp", "status": {"state": "working"},
                               "artifacts": []}}

        def factory(**kwargs):
            return _FakeAsyncClient(post_router=router)

        async def _noop(_):
            return None

        with _patch_async_client(factory), \
                patch("orchestration.a2a_client.asyncio.sleep", side_effect=_noop):
            task = asyncio.run(
                async_send_raw("http://h", _build_message("x"), poll_interval=0.01)
            )
        assert task["status"]["state"] == "completed"

    def test_input_required_breaks(self):
        def router(method, body):
            return {"result": {"id": "ti",
                               "status": {"state": "input-required"},
                               "artifacts": []}}

        rec = {}

        def factory(**kwargs):
            return _FakeAsyncClient(post_router=router, recorder=rec)

        with _patch_async_client(factory):
            task = asyncio.run(async_send_raw("http://h", _build_message("x")))
        assert task["status"]["state"] == "input-required"
        assert len(rec["post_calls"]) == 1

    def test_max_polls_exceeded(self):
        def router(method, body):
            return {"result": {"id": "ts", "status": {"state": "working"},
                               "artifacts": []}}

        def factory(**kwargs):
            return _FakeAsyncClient(post_router=router)

        async def _noop(_):
            return None

        with _patch_async_client(factory), \
                patch("orchestration.a2a_client.asyncio.sleep", side_effect=_noop):
            with pytest.raises(RuntimeError) as exc:
                asyncio.run(
                    async_send_raw("http://h", _build_message("x"), max_polls=2)
                )
        assert "after 2 polls" in str(exc.value)

    def test_poll_rpc_error(self):
        def router(method, body):
            if method == "message/send":
                return {"result": {"id": "te", "status": {"state": "working"},
                                   "artifacts": []}}
            return {"error": {"message": "poll-fail"}}

        def factory(**kwargs):
            return _FakeAsyncClient(post_router=router)

        async def _noop(_):
            return None

        with _patch_async_client(factory), \
                patch("orchestration.a2a_client.asyncio.sleep", side_effect=_noop):
            with pytest.raises(RuntimeError) as exc:
                asyncio.run(async_send_raw("http://h", _build_message("x")))
        assert "tasks/get RPC error" in str(exc.value)


# ===========================================================================
# async_send — lines 281-284  (real execution)
# ===========================================================================

class TestAsyncSend:
    def test_async_send_end_to_end(self):
        rec = {}

        def router(method, body):
            return {"result": {
                "id": "t",
                "status": {"state": "completed"},
                "artifacts": [{"parts": [{"kind": "text", "text": "areply"}]}],
            }}

        def factory(**kwargs):
            return _FakeAsyncClient(post_router=router, recorder=rec)

        with _patch_async_client(factory):
            out = asyncio.run(
                async_send("http://h", "hello", context_id="c1", timeout=9.0)
            )
        assert out == "areply"
        # message built from text + contextId merged
        sent_msg = rec["post_calls"][0][1]["params"]["message"]
        assert sent_msg["parts"][0]["text"] == "hello"
        assert sent_msg["contextId"] == "c1"

    def test_async_send_propagates_error_state(self):
        def router(method, body):
            return {"result": {"id": "t", "status": {"state": "failed"},
                               "metadata": {"error": "boom"}, "artifacts": []}}

        def factory(**kwargs):
            return _FakeAsyncClient(post_router=router)

        with _patch_async_client(factory):
            with pytest.raises(RuntimeError) as exc:
                asyncio.run(async_send("http://h", "txt"))
        assert "failed" in str(exc.value)


# ---------------------------------------------------------------------------
# Sanity: terminal-state constant integrity (guards the loop conditions).
# ---------------------------------------------------------------------------

def test_terminal_states_constant():
    assert TERMINAL_STATES == frozenset(
        {"completed", "failed", "canceled", "rejected"}
    )
    # input-required is deliberately NOT terminal but breaks the poll loop.
    assert "input-required" not in TERMINAL_STATES


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
