"""
test_health_git_sha.py — GET /health reports the running build's git SHA.

Regression coverage for the deploy-staleness incident: a stale staging
process can silently be missing a merged fix with no way to tell from the
outside. /health now exposes "git_sha" (resolved once at import time, never
shelled out per-request — see orchestration/server.py::_resolve_git_sha) so a
deploy script can diff it against `git rev-parse origin/main`.

Does NOT hardcode a real SHA (would break on every commit) — asserts the
field is present and shaped like either a 40-char hex commit SHA or the
"unknown" fallback used when git resolution fails.

var-0-safe: no live network, no LLM, no paid APIs.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(__file__))

from starlette.testclient import TestClient

from orchestration import server

_SHA_RE = re.compile(r"^[0-9a-f]{40}$")


class TestHealthGitSha:
    def test_health_reports_git_sha_field(self) -> None:
        """GET /health includes a 'git_sha' key."""
        app = server.build_app()
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.get("/health")
        assert resp.status_code == 200
        body = resp.json()
        assert "git_sha" in body
        print("PASS: test_health_reports_git_sha_field")

    def test_git_sha_is_full_hex_or_unknown(self) -> None:
        """git_sha is a full 40-char hex commit SHA, or the honest 'unknown' fallback."""
        app = server.build_app()
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.get("/health")
        sha = resp.json()["git_sha"]
        assert sha == "unknown" or _SHA_RE.match(sha), (
            f"git_sha {sha!r} is neither 'unknown' nor a 40-char hex SHA"
        )
        print("PASS: test_git_sha_is_full_hex_or_unknown")

    def test_resolve_git_sha_never_raises_and_returns_string(self) -> None:
        """_resolve_git_sha() must never crash the health check — always a str."""
        result = server._resolve_git_sha()
        assert isinstance(result, str)
        assert result == "unknown" or _SHA_RE.match(result)
        print("PASS: test_resolve_git_sha_never_raises_and_returns_string")

    def test_module_level_git_sha_matches_resolver(self) -> None:
        """The cached _GIT_SHA (resolved once at import) is consistent with the resolver."""
        assert isinstance(server._GIT_SHA, str)
        assert server._GIT_SHA == "unknown" or _SHA_RE.match(server._GIT_SHA)
        print("PASS: test_module_level_git_sha_matches_resolver")


class TestHealthGitShaFallback:
    """Regression coverage for the path the real deploy target actually hits:
    no .git dir (rsync --exclude='.git'), so `git rev-parse HEAD` can't run
    and _resolve_git_sha() must fall through to GIT_SHA env / "unknown".
    This is what ops/deploy-staging.sh's SHA stamp (writing GIT_SHA= into the
    systemd EnvironmentFile before restart) depends on actually working.
    """

    def test_resolve_git_sha_falls_back_to_env_var_when_git_unavailable(self) -> None:
        """When `git rev-parse HEAD` fails (e.g. no .git dir on the deploy
        target), _resolve_git_sha() must fall back to the GIT_SHA env var —
        this is exactly what ops/deploy-staging.sh relies on."""
        fake_sha = "a" * 40
        failed = subprocess.CompletedProcess(args=["git"], returncode=128, stdout="", stderr="fatal: not a git repository")
        with patch("subprocess.run", return_value=failed), patch.dict(os.environ, {"GIT_SHA": fake_sha}, clear=False):
            assert server._resolve_git_sha() == fake_sha
        print("PASS: test_resolve_git_sha_falls_back_to_env_var_when_git_unavailable")

    def test_resolve_git_sha_falls_back_to_unknown_when_git_and_env_both_absent(self) -> None:
        """When git resolution fails AND no GIT_SHA env var is set (e.g. a
        deploy target that predates ops/deploy-staging.sh's SHA stamp),
        _resolve_git_sha() must return the honest 'unknown' fallback rather
        than raising or fabricating a value."""
        failed = subprocess.CompletedProcess(args=["git"], returncode=128, stdout="", stderr="fatal: not a git repository")
        with patch("subprocess.run", return_value=failed), patch.dict(os.environ, {}, clear=False) as _env:
            os.environ.pop("GIT_SHA", None)
            assert server._resolve_git_sha() == "unknown"
        print("PASS: test_resolve_git_sha_falls_back_to_unknown_when_git_and_env_both_absent")

    def test_resolve_git_sha_falls_back_when_git_raises(self) -> None:
        """If invoking git itself raises (e.g. FileNotFoundError — git binary
        missing, or a timeout), _resolve_git_sha() must still degrade to the
        env var / 'unknown' instead of propagating the exception (the
        function must never crash the health check)."""
        fake_sha = "b" * 40
        with patch("subprocess.run", side_effect=FileNotFoundError("git not found")), patch.dict(os.environ, {"GIT_SHA": fake_sha}, clear=False):
            assert server._resolve_git_sha() == fake_sha
        print("PASS: test_resolve_git_sha_falls_back_when_git_raises")
