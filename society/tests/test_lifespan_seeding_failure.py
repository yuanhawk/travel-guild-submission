"""test_lifespan_seeding_failure.py — Coverage for startup seed_demo_users failure path.

The lifespan handler (server.py lines 1000–1005) catches any seeding exception and
logs a warning — startup must never abort due to a seeding error. This test confirms
the server stays healthy even when seed_demo_users raises.
"""

from __future__ import annotations

import sys
import os
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from starlette.testclient import TestClient
from orchestration import server


def test_startup_survives_seeding_failure():
    """seed_demo_users raises → server still starts; /health returns 200."""
    with patch(
        "orchestration.demo_users.seed_demo_users",
        side_effect=RuntimeError("simulated seed failure"),
    ):
        with TestClient(server.app, raise_server_exceptions=False) as client:
            resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json().get("status") == "ok"
