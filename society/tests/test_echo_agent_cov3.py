"""
test_echo_agent_cov3.py — Coverage for echo_agent.main() entry point.

Targets the previously-uncovered lines 107-125 of agents/echo_agent.py:
the main() function body (logging setup, env-var parsing, EchoAgent
instantiation, app building, startup log messages, uvicorn.run invocation)
and the __main__ entry-point guard.

All deterministic / var-0-safe:
  * uvicorn.run is mocked — NO real server binds, NO network.
  * os.environ is patched per-test for default + custom host/port.
  * logger / logging.basicConfig are mocked to capture calls.
No LLM, no paid API, no live merchant.
"""

from __future__ import annotations

import logging
import runpy
import sys
import os

from unittest import mock

sys.path.insert(0, os.path.dirname(__file__))

from starlette.applications import Starlette

from agents import echo_agent
from agents.echo_agent import EchoAgent, main


# ---------------------------------------------------------------------------
# Default environment (lines 111-115, 121)
# ---------------------------------------------------------------------------

def test_main_with_default_environment():
    """No PORT/HOST env -> defaults 127.0.0.1:9100 (FIX 6); uvicorn.run gets the app."""
    with mock.patch.dict(os.environ, {}, clear=True), \
            mock.patch.object(echo_agent, "uvicorn") as m_uvicorn:
        main()

    assert m_uvicorn.run.call_count == 1
    args, kwargs = m_uvicorn.run.call_args
    # app is positional
    app = args[0] if args else kwargs.get("app")
    assert isinstance(app, Starlette)
    # Security FIX 6: agents bind to 127.0.0.1 by default (AGENT_BIND_HOST).
    assert kwargs["host"] == "127.0.0.1"
    assert kwargs["port"] == 9100
    assert kwargs["log_level"] == "info"


def test_main_with_custom_port_env():
    """PORT env string is int()-parsed and threaded into agent + uvicorn."""
    with mock.patch.dict(os.environ, {"PORT": "8080"}, clear=True), \
            mock.patch.object(echo_agent, "uvicorn") as m_uvicorn:
        main()

    _, kwargs = m_uvicorn.run.call_args
    assert kwargs["port"] == 8080
    assert isinstance(kwargs["port"], int)


def test_main_with_custom_host_env():
    """HOST env value is threaded into agent + uvicorn."""
    with mock.patch.dict(os.environ, {"HOST": "127.0.0.1"}, clear=True), \
            mock.patch.object(echo_agent, "uvicorn") as m_uvicorn:
        main()

    _, kwargs = m_uvicorn.run.call_args
    assert kwargs["host"] == "127.0.0.1"


# ---------------------------------------------------------------------------
# logging.basicConfig setup (lines 107-110)
# ---------------------------------------------------------------------------

def test_main_logging_basicconfig():
    """basicConfig called once with INFO level and the expected format."""
    with mock.patch.dict(os.environ, {}, clear=True), \
            mock.patch.object(echo_agent, "uvicorn"), \
            mock.patch.object(logging, "basicConfig") as m_basic:
        main()

    assert m_basic.call_count == 1
    _, kwargs = m_basic.call_args
    assert kwargs["level"] == logging.INFO
    fmt = kwargs["format"]
    assert "%(asctime)s" in fmt
    assert "%(levelname)-8s" in fmt
    assert "%(name)s" in fmt
    assert "%(message)s" in fmt


# ---------------------------------------------------------------------------
# Startup log messages (lines 117-119)
# ---------------------------------------------------------------------------

def test_main_logger_info_calls():
    """Exactly 3 logger.info startup messages, carrying host/port."""
    with mock.patch.dict(os.environ, {"HOST": "127.0.0.1", "PORT": "8080"}, clear=True), \
            mock.patch.object(echo_agent, "uvicorn"), \
            mock.patch.object(echo_agent.logger, "info") as m_info:
        main()

    assert m_info.call_count == 3

    # Render each call (template % args) and check content deterministically.
    rendered = []
    for call in m_info.call_args_list:
        tmpl = call.args[0]
        fmt_args = call.args[1:]
        rendered.append(tmpl % fmt_args if fmt_args else tmpl)

    assert "Echo agent starting on" in rendered[0]
    assert "127.0.0.1" in rendered[0] and "8080" in rendered[0]
    assert "Agent Card" in rendered[1]
    assert rendered[1].startswith("Echo") is False
    assert "http://127.0.0.1:8080" in rendered[1]
    assert "RPC endpoint" in rendered[2]
    assert "http://127.0.0.1:8080" in rendered[2]


# ---------------------------------------------------------------------------
# uvicorn.run invocation contract (line 121)
# ---------------------------------------------------------------------------

def test_main_uvicorn_run_arguments():
    """uvicorn.run called once with a Starlette app + custom host/port/log_level."""
    with mock.patch.dict(os.environ, {"HOST": "127.0.0.1", "PORT": "8000"}, clear=True), \
            mock.patch.object(echo_agent, "uvicorn") as m_uvicorn:
        main()

    assert m_uvicorn.run.call_count == 1
    args, kwargs = m_uvicorn.run.call_args
    app = args[0] if args else kwargs.get("app")
    assert isinstance(app, Starlette)
    assert kwargs == {"host": "127.0.0.1", "port": 8000, "log_level": "info"}


# ---------------------------------------------------------------------------
# Invalid PORT -> int() raises (error path on line 111)
# ---------------------------------------------------------------------------

def test_main_port_parsing_invalid():
    """Non-integer PORT propagates a ValueError from int()."""
    import pytest

    with mock.patch.dict(os.environ, {"PORT": "not-a-number"}, clear=True), \
            mock.patch.object(echo_agent, "uvicorn") as m_uvicorn:
        with pytest.raises(ValueError):
            main()

    # uvicorn must never be reached when parsing fails.
    m_uvicorn.run.assert_not_called()


# ---------------------------------------------------------------------------
# __main__ entry-point guard (lines 124-125)
# ---------------------------------------------------------------------------

def test_main_entry_point_guard_true():
    """Running the module as __main__ invokes main() (uvicorn mocked off)."""
    # runpy executes the module fresh under __name__ == "__main__", hitting the
    # guard at line 124-125. Patch uvicorn in the real module namespace so the
    # freshly-imported copy's `import uvicorn` resolves to a stub (sys.modules).
    fake_uvicorn = mock.MagicMock()
    with mock.patch.dict(os.environ, {}, clear=True), \
            mock.patch.dict(sys.modules, {"uvicorn": fake_uvicorn}):
        runpy.run_module("agents.echo_agent", run_name="__main__")

    assert fake_uvicorn.run.call_count == 1
    _, kwargs = fake_uvicorn.run.call_args
    # Security FIX 6: agents bind to 127.0.0.1 by default (AGENT_BIND_HOST).
    assert kwargs["host"] == "127.0.0.1"
    assert kwargs["port"] == 9100
    assert kwargs["log_level"] == "info"
