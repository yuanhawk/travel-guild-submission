"""test_place_photo_error_paths.py — Coverage for GET /place_photo error branches.

Three previously uncovered paths (server.py lines 2203–2218):
  A. Missing ?ref= param → 400
  B. Unknown/expired ref (resolve_photo_name returns None) → 404
  C. fetch_place_photo returns None (key not set / fetch failed) → 200 unavailable

All deterministic, no network, no Places API key required.
"""

from __future__ import annotations

import sys
import os
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import pytest
from starlette.testclient import TestClient
from orchestration import server


@pytest.fixture(scope="module")
def client():
    return TestClient(server.app, raise_server_exceptions=False)


def test_place_photo_missing_ref_returns_400(client):
    """GET /place_photo with no ?ref= → 400 + JSON error."""
    resp = client.get("/place_photo")
    assert resp.status_code == 400
    data = resp.json()
    assert "ref" in data.get("error", "").lower()


def test_place_photo_unknown_ref_returns_404(client):
    """GET /place_photo?ref=nonexistent → resolve_photo_name returns None → 404."""
    with patch("utils.places_card.resolve_photo_name", return_value=None):
        resp = client.get("/place_photo?ref=nonexistent-opaque-ref")
    assert resp.status_code == 404
    data = resp.json()
    assert "unknown" in data.get("error", "").lower() or "expired" in data.get("error", "").lower()


def test_place_photo_fetch_none_returns_200_unavailable():
    """GET /place_photo?ref=valid → resolve returns a name but fetch returns None → 200 unavailable.

    Uses a lifespan-aware client because the handler calls _state.loop.run_in_executor,
    which requires _state.loop to be initialized via the app's lifespan context.
    """
    with (
        patch("utils.places_card.resolve_photo_name", return_value="places/photo/ABC123"),
        patch("utils.places_card.fetch_place_photo", return_value=None),
    ):
        with TestClient(server.app, raise_server_exceptions=False) as lc:
            resp = lc.get("/place_photo?ref=valid-ref-key-not-set")
    assert resp.status_code == 200
    data = resp.json()
    assert data.get("status") == "unavailable"
    # Verify Places API key is never exposed
    body_text = resp.text
    assert "AIza" not in body_text
    assert "api_key" not in body_text.lower()
