"""test_telegram_link_endpoint.py — GET /telegram/link (Telegram "Connect" flow).

Covers:
- missing user_id -> 400 (matches this file's trips_list-style error shape)
- SECURITY (user_id-trust gap fix): missing/wrong/bogus session_token -> 401,
  no link token minted; a correct session_token for the matching user_id
  still works (regression-check the happy path). This is the regression lock
  for the vulnerability an security review found: previously ANYONE who
  knew/guessed a user_id (e.g. "demo-mei") could mint a link token and
  hijack that user's Telegram alert channel — a session_token (from
  POST /session/login) is now REQUIRED and must belong to the claimed user_id.
- TELEGRAM_BOT_USERNAME unset -> {"available": False, "reason": ...} (honest
  degradation, HTTP 200 — mirrors telegram_notify.py's empty-token gating)
- TELEGRAM_BOT_USERNAME set -> {"available": True, "deep_link": "https://t.me/<bot>?start=<token>",
  "expires_in_seconds": ...}
- the issued link token actually resolves via telegram_link.resolve_link_token
- POST /session/login: known demo user -> token; unknown user -> 404;
  missing user_id / invalid JSON -> 400

NOTE: `generate_link_token` itself still does NOT require the claimed user_id
to be a known demo user at generation time (that validation happens at
RESOLVE time, in the bot's /start handler) — that unit-level property is
covered directly in test_telegram_link.py, since the new session_token gate
at the HTTP layer means an unrecognized user_id can never reach this endpoint
in the first place (no session can ever be minted for it).
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from orchestration.store import SqliteDashboardStore, set_store


def _client():
    from starlette.testclient import TestClient
    from orchestration.server import build_app
    set_store(SqliteDashboardStore(":memory:"))   # fresh store; lifespan seeds it
    return TestClient(build_app())


def _login(c, user_id: str) -> str:
    r = c.post("/session/login", json={"user_id": user_id})
    assert r.status_code == 200, r.text
    return r.json()["session_token"]


def test_missing_user_id_400():
    with _client() as c:
        r = c.get("/telegram/link")
        assert r.status_code == 400


def test_missing_session_token_401_no_link_minted(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_USERNAME", "TravelGuildBot")
    with _client() as c:
        r = c.get("/telegram/link", params={"user_id": "demo-mei"})
        assert r.status_code == 401
        assert "deep_link" not in r.json()


def test_wrong_user_session_token_401_no_link_minted(monkeypatch):
    """A valid session token minted for a DIFFERENT user must not authorize
    issuing a link token for the claimed user_id — the core regression lock
    for the hijack vulnerability the audit found."""
    monkeypatch.setenv("TELEGRAM_BOT_USERNAME", "TravelGuildBot")
    with _client() as c:
        other_token = _login(c, "demo-alex")
        r = c.get("/telegram/link", params={"user_id": "demo-mei", "session_token": other_token})
        assert r.status_code == 401
        assert "deep_link" not in r.json()


def test_bogus_session_token_401():
    with _client() as c:
        r = c.get("/telegram/link", params={"user_id": "demo-mei", "session_token": "not-a-real-token"})
        assert r.status_code == 401


def test_correct_session_token_still_succeeds(monkeypatch):
    """Regression-check: the happy path (matching, valid session_token) still works."""
    monkeypatch.setenv("TELEGRAM_BOT_USERNAME", "TravelGuildBot")
    with _client() as c:
        token = _login(c, "demo-mei")
        r = c.get("/telegram/link", params={"user_id": "demo-mei", "session_token": token})
        assert r.status_code == 200
        assert r.json()["available"] is True


def test_bot_not_configured_honest_unavailable(monkeypatch):
    monkeypatch.delenv("TELEGRAM_BOT_USERNAME", raising=False)
    with _client() as c:
        token = _login(c, "demo-mei")
        r = c.get("/telegram/link", params={"user_id": "demo-mei", "session_token": token})
        assert r.status_code == 200
        body = r.json()
        assert body["available"] is False
        assert "reason" in body
        assert "deep_link" not in body


def test_bot_configured_issues_deep_link(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_USERNAME", "TravelGuildBot")
    with _client() as c:
        token = _login(c, "demo-mei")
        r = c.get("/telegram/link", params={"user_id": "demo-mei", "session_token": token})
        assert r.status_code == 200
        body = r.json()
        assert body["available"] is True
        assert body["deep_link"].startswith("https://t.me/TravelGuildBot?start=")
        assert body["expires_in_seconds"] == 900


def test_issued_token_actually_resolves(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_USERNAME", "TravelGuildBot")
    with _client() as c:
        token = _login(c, "demo-alex")
        r = c.get("/telegram/link", params={"user_id": "demo-alex", "session_token": token})
        deep_link = r.json()["deep_link"]
        link_token = deep_link.split("?start=")[1]

    from utils.telegram_link import resolve_link_token
    assert resolve_link_token(link_token) == "demo-alex"


# ---------------------------------------------------------------------------
# POST /session/login
# ---------------------------------------------------------------------------

def test_login_known_user_returns_token():
    with _client() as c:
        r = c.post("/session/login", json={"user_id": "demo-mei"})
        assert r.status_code == 200
        token = r.json()["session_token"]
        assert isinstance(token, str) and len(token) >= 24


def test_login_unknown_user_404():
    with _client() as c:
        r = c.post("/session/login", json={"user_id": "totally-unknown-user"})
        assert r.status_code == 404


def test_login_missing_user_id_400():
    with _client() as c:
        assert c.post("/session/login", json={}).status_code == 400


def test_login_invalid_json_400():
    with _client() as c:
        r = c.post("/session/login", data="{bad", headers={"content-type": "application/json"})
        assert r.status_code == 400
