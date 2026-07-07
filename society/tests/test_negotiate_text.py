"""
test_negotiate_text.py — Integration tests for the POST /negotiate_text endpoint
and the Denpasar→Bali alias in intent_parser.

CI-safe: forces the deterministic (no-LLM) path by patching DASHSCOPE_API_KEY
to "" so no network calls are made.  Uses Starlette TestClient.

Coverage:
  1. test_negotiate_text_catalog_city   — free text naming a catalog city + budget
                                          → non-clarification booked result.
  2. test_negotiate_text_denpasar_alias — "8 days in denpasar, beach, $3000"
                                          → city resolves to "bali".
  3. test_negotiate_text_fictional_city — fictional city (Narnia) → needs_clarification.
  4. test_negotiate_text_missing_text   — empty body → 400.
  5. test_denpasar_island_alias         — "denpasar island" → bali alias in parser.
"""

from __future__ import annotations

import os
import sys
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(__file__))

# Import server module — uses the shared app factory.
from orchestration import server as server_mod
from starlette.testclient import TestClient


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _build_app():
    """Build a fresh Starlette app instance."""
    return server_mod.build_app()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_negotiate_text_catalog_city() -> None:
    """
    Free text naming a catalog city (bali) with a budget should produce a
    booked result (not needs_clarification) on the deterministic path.
    """
    app = _build_app()
    with patch("utils.intent_parser.DASHSCOPE_API_KEY", ""):
        # Use context manager to trigger lifespan (populates _state.orch etc.)
        with TestClient(app, raise_server_exceptions=True) as client:
            resp = client.post(
                "/negotiate_text",
                json={"text": "7 days in bali, beach, $3000", "user_id": "test-user"},
            )
    assert resp.status_code == 200, f"HTTP {resp.status_code}: {resp.text}"
    data = resp.json()
    # Must NOT be a clarification response
    assert not data.get("needs_clarification"), (
        f"Expected booked result, got needs_clarification: {data.get('reason')!r}"
    )
    # Must have an outcome field (success or cannot_satisfy are both valid;
    # the important thing is the parser did NOT return needs_clarification)
    assert "outcome" in data, f"Expected 'outcome' key in result: {data}"
    print(f"PASS: test_negotiate_text_catalog_city [outcome={data['outcome']!r}]")


def test_negotiate_text_denpasar_alias() -> None:
    """
    "8 days in denpasar, beach, $3000" should resolve city to "bali" via the
    CITY_ALIASES / CITY_SLUG_MAP alias, producing a booked result (not clarification).
    Also directly verifies the alias in intent_parser.
    """
    from utils import intent_parser as ip

    # Direct parser-level check: the alias must be present and correct.
    assert ip.CITY_ALIASES.get("denpasar") == "bali", (
        "CITY_ALIASES['denpasar'] must map to 'bali'"
    )
    assert ip.CITY_SLUG_MAP.get("denpasar") == "bali", (
        "CITY_SLUG_MAP['denpasar'] must map to 'bali'"
    )

    # End-to-end: parse_intent should resolve denpasar → bali.
    with patch("utils.intent_parser.DASHSCOPE_API_KEY", ""):
        result = ip.parse_intent("8 days in denpasar, beach, $3000", user_id="guest")

    assert not result.get("needs_clarification"), (
        f"Denpasar should resolve to bali, got needs_clarification: {result.get('reason')!r}"
    )
    legs = result.get("legs", [])
    assert len(legs) >= 1, f"Expected at least 1 leg, got: {legs}"
    for leg in legs:
        assert leg["city"] == "bali", (
            f"Expected city='bali' (denpasar alias), got {leg['city']!r}"
        )
    print(f"PASS: test_negotiate_text_denpasar_alias [legs={[l['city'] for l in legs]}]")


def test_negotiate_text_fictional_city() -> None:
    """
    A fictional city (Narnia) should return needs_clarification — honest decline.
    """
    app = _build_app()
    with patch("utils.intent_parser.DASHSCOPE_API_KEY", ""):
        with TestClient(app, raise_server_exceptions=True) as client:
            resp = client.post(
                "/negotiate_text",
                json={"text": "5 days in Narnia, adventure, $2000"},
            )
    assert resp.status_code == 200, f"HTTP {resp.status_code}: {resp.text}"
    data = resp.json()
    assert data.get("needs_clarification") is True, (
        f"Expected needs_clarification=True for fictional city, got: {data}"
    )
    assert "reason" in data and data["reason"], (
        f"needs_clarification response must include a reason: {data}"
    )
    print(f"PASS: test_negotiate_text_fictional_city [reason={data['reason'][:80]!r}]")


def test_negotiate_text_missing_text() -> None:
    """
    POST /negotiate_text with empty or missing 'text' field → HTTP 400.
    """
    app = _build_app()
    with patch("utils.intent_parser.DASHSCOPE_API_KEY", ""):
        with TestClient(app, raise_server_exceptions=True) as client:
            # Empty text
            resp = client.post("/negotiate_text", json={"text": ""})
    assert resp.status_code == 400, (
        f"Expected 400 for empty text, got {resp.status_code}: {resp.text}"
    )
    print("PASS: test_negotiate_text_missing_text")


def test_denpasar_island_alias() -> None:
    """
    "denpasar island" multi-word alias must also resolve to "bali".
    """
    from utils import intent_parser as ip

    assert ip.CITY_ALIASES.get("denpasar island") == "bali", (
        "CITY_ALIASES['denpasar island'] must map to 'bali'"
    )
    assert ip.CITY_SLUG_MAP.get("denpasar island") == "bali", (
        "CITY_SLUG_MAP['denpasar island'] must map to 'bali'"
    )

    # The multi-word alias is listed first so the length-sorted scanner matches
    # "denpasar island" before the single-word "denpasar"; both resolve to bali.
    with patch("utils.intent_parser.DASHSCOPE_API_KEY", ""):
        result = ip.parse_intent("6 days in denpasar island, beach, $2500", user_id="guest")

    assert not result.get("needs_clarification"), (
        f"'denpasar island' should resolve to bali, got clarification: {result.get('reason')!r}"
    )
    legs = result.get("legs", [])
    assert len(legs) >= 1, f"Expected at least 1 leg, got: {legs}"
    for leg in legs:
        assert leg["city"] == "bali", (
            f"Expected city='bali' (denpasar island alias), got {leg['city']!r}"
        )
    print(f"PASS: test_denpasar_island_alias [legs={[l['city'] for l in legs]}]")


def test_negotiate_text_denpasar_endpoint() -> None:
    """End-to-end through the ENDPOINT: 'denpasar' → bali booking (not just the
    parser in isolation)."""
    app = _build_app()
    with patch("utils.intent_parser.DASHSCOPE_API_KEY", ""):
        with TestClient(app, raise_server_exceptions=True) as client:
            resp = client.post(
                "/negotiate_text",
                json={"text": "8 days in denpasar, beach, $3000"},
            )
    assert resp.status_code == 200, f"HTTP {resp.status_code}: {resp.text}"
    data = resp.json()
    assert not data.get("needs_clarification"), (
        f"denpasar should resolve via the endpoint, got clarification: {data.get('reason')!r}"
    )
    legs = data.get("legs", [])
    assert legs and all(l.get("city") == "bali" for l in legs), (
        f"Expected all legs city='bali' via endpoint denpasar alias, got {[l.get('city') for l in legs]}"
    )
    print(f"PASS: test_negotiate_text_denpasar_endpoint [legs={[l.get('city') for l in legs]}]")


def test_negotiate_text_too_long() -> None:
    """Oversized free text → HTTP 400 (DoS guard), not a slow/hung request."""
    app = _build_app()
    big = "8 days in bali, beach, $3000. " + ("filler " * 1000)  # > 2000 chars
    with patch("utils.intent_parser.DASHSCOPE_API_KEY", ""):
        with TestClient(app, raise_server_exceptions=True) as client:
            resp = client.post("/negotiate_text", json={"text": big})
    assert resp.status_code == 400, (
        f"Expected 400 for oversized text, got {resp.status_code}: {resp.text[:120]}"
    )
    print("PASS: test_negotiate_text_too_long")


# ---------------------------------------------------------------------------
# Test runner (also pytest-compatible)
# ---------------------------------------------------------------------------

TESTS = [
    test_negotiate_text_catalog_city,
    test_negotiate_text_denpasar_alias,
    test_negotiate_text_fictional_city,
    test_negotiate_text_missing_text,
    test_denpasar_island_alias,
    test_negotiate_text_denpasar_endpoint,
    test_negotiate_text_too_long,
]

if __name__ == "__main__":
    import traceback

    passed = failed = 0
    for fn in TESTS:
        try:
            fn()
            passed += 1
        except Exception as exc:
            failed += 1
            print(f"FAIL: {fn.__name__} — {exc}")
            traceback.print_exc()

    print(f"\n{passed} passed, {failed} failed out of {len(TESTS)} tests")
    if failed:
        sys.exit(1)
