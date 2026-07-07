"""
test_ttl_enforcement.py — TTL enforcement for seeded provenance envelopes.

Every agent stamps a `_SEED_TTL_DAYS` freshness window into its provenance
envelope (fetched_at + ttl), but until now NOTHING ever checked it — a seeded
rule kept being served as confidently-current forever, even long past its own
stated freshness window (verified by grep: no call site in
society/orchestration/ or society/core/ ever read fetched_at+ttl before this
change).

This suite covers:
  1. The shared primitive (core.contracts.is_provenance_expired /
     stale_provenance_note / apply_ttl_downgrade) directly against a
     hand-seeded expired envelope (never expired: ttl=None; not yet expired;
     exactly at the boundary; past the boundary).
  2. The real wiring in each of the three agents that stamp+check TTL
     (compliance / health / risk): a stale seeded rule downgrades a
     confident verdict to the SAME "unverified" convention the codebase
     already uses for an unknown seed, with an honest stale-data note — and a
     FRESH seed (today within the window) is completely unaffected (no
     regression on the existing byte-identical behaviour).
  3. The risk A2A wire path specifically, because `risk.assess`'s handler
     used to silently DROP the `today` key the orchestrator already sent.
"""

from __future__ import annotations

import pytest
from starlette.testclient import TestClient

from core import contracts as C
from agents.compliance_agent import GateVerdict as ComplianceVerdict, check_eligibility
from agents.health_agent import GateVerdict as HealthVerdict, assess as health_assess
from agents import risk_agent as ra

# The seed baseline every agent shares: fetched 2026-06-10, ttl 90 days ->
# expires 2026-09-08 (>= that date is stale).
FRESH_TODAY = "2026-06-16"    # well inside the 90-day window
STALE_TODAY = "2026-09-09"    # 1 day past the 90-day window


# ===========================================================================
# 1. Shared primitive — hand-seeded envelopes (no agent involved)
# ===========================================================================

def _seeded_envelope(**overrides) -> dict:
    env = C.make_provenance(
        source="seed:unit-test-2026",
        fetched_at="2026-06-10",
        tier=C.SourceTier.SEEDED,
        ttl=90 * 24 * 3600,  # 90 days, seconds (the shared convention)
    )
    env.update(overrides)
    return env


def test_provenance_not_expired_within_window():
    env = _seeded_envelope()
    assert C.is_provenance_expired(env, as_of="2026-07-01") is False


def test_provenance_expired_past_window():
    env = _seeded_envelope()
    # fetched 2026-06-10 + 90d = 2026-09-08; one day later is unambiguously stale.
    assert C.is_provenance_expired(env, as_of="2026-09-09") is True


def test_provenance_expired_at_exact_boundary_day():
    env = _seeded_envelope()
    assert C.is_provenance_expired(env, as_of="2026-09-08") is True


def test_provenance_ttl_none_never_expires():
    # A fixed constant with NO freshness window by design must never be flagged
    # stale no matter how far `as_of` is pushed out.
    env = _seeded_envelope(ttl=None)
    assert C.is_provenance_expired(env, as_of="2099-01-01") is False


def test_provenance_no_as_of_never_evaluated():
    # No deterministic clock threaded in -> not evaluated (never a surprise
    # wall-clock read inside a otherwise-pure module).
    env = _seeded_envelope()
    assert C.is_provenance_expired(env, as_of=None) is False


def test_provenance_malformed_envelope_never_expired():
    assert C.is_provenance_expired({"not": "an envelope"}, as_of="2099-01-01") is False


def test_stale_note_mentions_source_and_window():
    env = _seeded_envelope()
    note = C.stale_provenance_note(env)
    assert "unit-test-2026" in note
    assert "90" in note
    assert "reconfirm" in note.lower()


def test_apply_ttl_downgrade_fires_on_expired_seed_and_downgrades_verified_true():
    """The literal 'seed an expired envelope, confirm the downgrade fires' case."""
    record = {"allowed": True, "provenance": _seeded_envelope()}
    fired = C.apply_ttl_downgrade(record, as_of=STALE_TODAY, verified_field="allowed")
    assert fired is True
    assert record["allowed"] is None            # confident True -> unverified None
    assert record["unverified_flag"] is True
    assert record["provenance_stale"] is True
    assert "stale" in record["stale_note"].lower()
    assert record["flag_advisory"] == record["stale_note"]


def test_apply_ttl_downgrade_noop_on_fresh_seed():
    record = {"allowed": True, "provenance": _seeded_envelope()}
    fired = C.apply_ttl_downgrade(record, as_of=FRESH_TODAY, verified_field="allowed")
    assert fired is False
    assert record["allowed"] is True             # untouched
    assert "provenance_stale" not in record
    assert "stale_note" not in record


def test_apply_ttl_downgrade_never_loosens_an_existing_hard_block():
    # allowed=False (a genuine hard block) must stay False even when the
    # underlying data is ALSO stale — staleness only ever adds honesty, it
    # never removes an existing block.
    record = {"allowed": False, "provenance": _seeded_envelope()}
    fired = C.apply_ttl_downgrade(record, as_of=STALE_TODAY, verified_field="allowed")
    assert fired is True
    assert record["allowed"] is False
    assert record["provenance_stale"] is True


def test_apply_ttl_downgrade_leaves_already_unverified_alone():
    record = {"allowed": None, "provenance": _seeded_envelope()}
    fired = C.apply_ttl_downgrade(record, as_of=STALE_TODAY, verified_field="allowed")
    assert fired is True
    assert record["allowed"] is None
    assert record["provenance_stale"] is True


# ===========================================================================
# 2. Real wiring — Compliance (check_eligibility)
# ===========================================================================

def test_compliance_stale_seed_downgrades_allow_to_flagged():
    # Thailand is visa-free for a US passport (kind=VISA_FREE, allowed=True on
    # the happy path). Push `today` past the seed's 90-day TTL.
    v = check_eligibility(
        legs=[{"dest_country": "TH", "departure_date": "2026-10-18"}],
        nationality="US", today=STALE_TODAY,
    )
    leg = v["per_leg"][0]
    assert leg["allowed"] is None
    assert leg["provenance_stale"] is True
    assert "reconfirm" in leg["stale_note"].lower()
    assert v["verdict"] == ComplianceVerdict.ALLOW_WITH_FLAGS
    assert v["bookable"] is True   # a stale-data flag is NOT a hard block


def test_compliance_fresh_seed_is_byte_identical_no_regression():
    v = check_eligibility(
        legs=[{"dest_country": "TH", "departure_date": "2026-06-18"}],
        nationality="US", today=FRESH_TODAY,
    )
    leg = v["per_leg"][0]
    assert leg["allowed"] is True
    assert "provenance_stale" not in leg
    assert v["verdict"] == ComplianceVerdict.ALLOW


# ===========================================================================
# 3. Real wiring — Health (assess)
# ===========================================================================

def test_health_stale_seed_downgrades_can_complete_to_flagged():
    # Thailand's CDC slate has no mandatory entry cert -> can_complete=True on
    # the happy path regardless of departure timing.
    v = health_assess(
        legs=[{"place_key": "thailand", "departure_date": "2026-10-18"}],
        today=STALE_TODAY,
    )
    d = v["per_destination"][0]
    assert d["can_complete"] is None
    assert d["provenance_stale"] is True
    assert "reconfirm" in d["stale_note"].lower()
    assert v["verdict"] == HealthVerdict.CAN_COMPLETE_WITH_FLAGS
    assert v["bookable"] is True


def test_health_fresh_seed_is_byte_identical_no_regression():
    v = health_assess(
        legs=[{"place_key": "thailand", "departure_date": "2026-06-18"}],
        today=FRESH_TODAY,
    )
    d = v["per_destination"][0]
    assert d["can_complete"] is True
    assert "provenance_stale" not in d
    assert v["verdict"] == HealthVerdict.CAN_COMPLETE


# ===========================================================================
# 4. Real wiring — Risk (assess_leg / assess + the A2A wire path)
# ===========================================================================

def test_risk_stale_seed_flags_an_otherwise_clean_leg():
    # Darwin OFF cyclone season fires zero hazard advisories on the happy path
    # (decisions.flag == False) — exactly the "confidently all-clear" case a
    # TTL check exists to catch.
    fresh = ra.assess_leg(city="darwin", checkin="2026-08-10", checkout="2026-08-17",
                           today=FRESH_TODAY)
    assert fresh["decisions"]["flag"] is False
    assert fresh["advisory"] == []

    stale = ra.assess_leg(city="darwin", checkin="2026-08-10", checkout="2026-08-17",
                           today=STALE_TODAY)
    assert stale["decisions"]["flag"] is True
    assert any(a["type"] == "stale_data" for a in stale["advisory"])
    assert C.RiskReasonCode.ADVISORY_ELEVATED.value in stale["reason_codes"]


def test_risk_assess_leg_without_today_is_byte_identical_no_regression():
    # Omitting `today` entirely (the pre-TTL call shape) must skip the TTL
    # check outright — never reach for a real wall-clock inside this
    # otherwise-pure module.
    sig = ra.assess_leg(city="darwin", checkin="2026-08-10", checkout="2026-08-17")
    assert sig["decisions"]["flag"] is False
    assert sig["advisory"] == []


def test_risk_assess_trip_level_rollup_reflects_stale_downgrade():
    result = ra.assess(
        legs=[{"leg_id": "leg-0", "city": "darwin", "checkin": "2026-08-10",
               "checkout": "2026-08-17"}],
        today=STALE_TODAY,
    )
    assert result["rollup"]["flagged_legs"] == ["leg-0"]
    assert C.RiskReasonCode.ADVISORY_ELEVATED.value in result["rollup"]["all_reason_codes"]


@pytest.fixture()
def risk_client() -> TestClient:
    return TestClient(ra.RiskAgent().build_app())


def test_risk_a2a_wire_path_honours_today_previously_silently_dropped(risk_client):
    """
    Regression guard: the orchestrator has always sent `today` in the
    risk.assess payload (D7 — the single frozen run-clock), but the handler
    used to silently ignore it (never read, never threaded to assess()). This
    drives the REAL A2A message/send path end-to-end to prove the wiring
    (not just the internal assess_leg/assess functions) actually applies it.
    """
    payload = {
        "legs": [{"leg_id": "leg-0", "city": "darwin", "checkin": "2026-08-10",
                   "checkout": "2026-08-17"}],
        "today": STALE_TODAY,
    }
    req = {
        "jsonrpc": "2.0", "id": "t", "method": "message/send",
        "params": {"message": {"role": "user",
                                "parts": [{"kind": "data", "data": payload}]}},
    }
    resp = risk_client.post("/", json=req)
    assert resp.status_code == 200, resp.text
    task = resp.json()["result"]
    data = None
    for art in task.get("artifacts", []):
        for part in art.get("parts", []):
            if part.get("kind") == "data":
                data = part["data"]
    assert data is not None
    assert data["rollup"]["flagged_legs"] == ["leg-0"]
