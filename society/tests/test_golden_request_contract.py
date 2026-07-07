"""
#167 Golden-REQUEST contract test — structural mirror of #120's golden-envelope.

For every canonical frontend request in tests/fixtures/golden_requests/*.json,
send it through the REAL Starlette app (TestClient) and assert the backend does
NOT reject it at the required-field gate. Proves the frontend's actually-
constructed request body/params pass validation and reach a genuine domain
outcome. The SAME json files are deep-equal'd by the frontend (api.golden.test.ts,
web/src/lib/__fixtures__/golden_requests/), so drift on either side
breaks a test.

Fixtures are shared source-of-truth; the orchestrator is stubbed (only the
field-gate + dispatch is under test, not plan generation).
"""
from __future__ import annotations

import glob
import json
import os
import sys
from unittest.mock import patch

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from starlette.testclient import TestClient
from orchestration import server
from orchestration.store import SqliteDashboardStore, set_store
from orchestration.demo_users import seed_demo_users

FIXTURE_DIR = os.path.join(os.path.dirname(__file__), "fixtures", "golden_requests")
_PATHS = sorted(glob.glob(os.path.join(FIXTURE_DIR, "*.json")))
_IDS = [os.path.basename(p) for p in _PATHS]
_NO_FIXTURES = len(_PATHS) == 0

# Reasons/errors that mean "you failed to supply a required/valid-shape field".
# NOT domain outcomes like unknown_plan / plan_cancelled / not_trip_owner.
_MISSING_FIELD_MARKERS = (
    "required", "missing", "must be", "non-empty",
    "too long", "invalid json", "unknown mode",
)


def _load(path: str) -> dict:
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def _client() -> TestClient:
    store = SqliteDashboardStore(":memory:")
    seed_demo_users(store)  # demo-lena etc. exist -> identity endpoints reach 200/401, never a route-404
    set_store(store)
    return TestClient(server.build_app())


def _is_shape_rejection(status_code: int, body) -> tuple[bool, str]:
    """True IFF the backend rejected the request for a MISSING/MALFORMED field."""
    if status_code == 400:
        # Every 400 in these handlers is a field/shape rejection; our fixtures
        # supply all required fields, so a 400 is a genuine contract break.
        return True, f"HTTP 400: {body}"
    if not isinstance(body, dict):
        return False, ""
    outcome = str(body.get("outcome") or "").lower()
    reason = str(body.get("reason") or "").lower()
    err = str(body.get("error") or "").lower()
    # /confirm returns invalid_request at HTTP 200 for a MISSING key -- catch it
    # by reason text, while letting unknown_plan (a real domain outcome) pass.
    if outcome == "invalid_request" and any(m in reason for m in _MISSING_FIELD_MARKERS):
        return True, f"HTTP 200 missing-field invalid_request: reason={reason!r}"
    if err and any(m in err for m in _MISSING_FIELD_MARKERS):
        return True, f"missing-field error: {err!r}"
    return False, ""


_LIGHT_PLAN = {
    "outcome": "plan_ready",
    "legs": [{"leg_id": "l0", "city": "tokyo", "checkin": "2026-10-01", "checkout": "2026-10-08"}],
    "day_plans": [],
    "idempotency_key": "gr-neg",
}


def _dispatch(client: TestClient, fx: dict):
    kind, method = fx["kind"], fx["method"].upper()
    if kind == "body":
        return client.request(method, fx["path"], json=fx["body"])
    if kind == "query":
        return client.get(fx["path"], params=fx.get("query", {}))
    if kind == "path_query":
        path = fx["path_template"].format(**fx["path_params"])
        return client.get(path, params=fx.get("query", {}))
    if kind == "url_only":  # place_photo -- real GET with the ref
        return client.get(fx["path"], params=fx.get("query", {}))
    raise AssertionError(f"unknown fixture kind {kind!r}")


@pytest.mark.skipif(_NO_FIXTURES, reason="No golden_requests fixtures found")
@pytest.mark.parametrize("fixture_path", _PATHS, ids=_IDS)
def test_backend_accepts_frontend_request_shape(fixture_path: str) -> None:
    fx = _load(fixture_path)
    # Stub ONLY the heavy orchestrator entrypoint (reached solely by /negotiate_text);
    # every other endpoint short-circuits on the empty/seeded store before it.
    with patch("utils.intent_parser.negotiate_from_text", return_value=dict(_LIGHT_PLAN)):
        client = _client()
        with client:
            r = _dispatch(client, fx)
    # 1. Never a 500 (the shape must not crash the handler).
    assert r.status_code != 500, (
        f"{fx['endpoint']}: handler 500'd on the canonical shape\n{r.text[:500]}"
    )
    # 2. Never a route-404 (405 for a mistyped path/method).
    assert r.status_code != 405, (
        f"{fx['endpoint']}: method not allowed -- path/method contract drift"
    )
    body = None
    try:
        body = r.json()
    except Exception:
        body = None  # place_photo bytes / non-JSON is fine
    # 3. THE CONTRACT: the required-field gate must NOT have rejected our valid shape.
    rejected, why = _is_shape_rejection(r.status_code, body)
    assert not rejected, (
        f"{fx['endpoint']} ({fx['method']} {fx.get('path') or fx.get('path_template')}): "
        f"backend REJECTED the frontend's canonical request shape at the field gate -- {why}. "
        f"This is exactly the /place_card-class drift this test exists to catch."
    )


def test_all_17_endpoints_have_a_fixture() -> None:
    """Regression lock: the enumerated FE HTTP surface is 17 calls -- every one pinned."""
    assert len(_PATHS) == 17, f"expected 17 golden-request fixtures, found {len(_PATHS)}: {_IDS}"


def test_all_fixtures_valid_json_with_required_meta() -> None:
    for p in _PATHS:
        fx = _load(p)
        key = "path" if "path" in fx else "path_template"
        for k in ("endpoint", "method", key, "kind"):
            assert k in fx, f"{os.path.basename(p)} missing meta key {k!r}"
        assert fx["kind"] in ("body", "query", "path_query", "url_only")
