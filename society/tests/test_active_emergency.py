"""
#51 — OPT-IN LIVE active-emergency overlay (firewalled off the var-0 path).
The seasonal risk model is a frozen climatology (ADVISORY); a DECLARED active
emergency is a live fact surfaced ONLY in a separate top-level key, never on the
deterministic risk path. Mirrors the #32 dining-overlay firewall pattern.
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from orchestration.orchestrator import TravelOrchestrator


def _day_plans():
    return [
        {"leg_id": "leg-0", "city": "los angeles", "iso2": "US",
         "region": "us-west", "days": []},
        {"leg_id": "leg-1", "city": "sydney", "iso2": "AU",
         "region": "au-sydney", "days": []},
    ]


def _success_result():
    return {"outcome": "success", "day_plans": _day_plans()}


def _orch(emergency_client=None):
    orch = TravelOrchestrator(emergency_client=emergency_client)
    orch._trip_id = "t-emergency"
    return orch


# --------------------------------------------------------------------------
# APPEND-ONLY / var-0
# --------------------------------------------------------------------------
def test_not_opted_in_byte_identical():
    orch = _orch()
    orch._emergency_request = None
    result = _success_result()
    before = json.dumps(result, sort_keys=True)
    orch._maybe_check_active_emergencies(result)
    assert "active_emergencies" not in result
    assert json.dumps(result, sort_keys=True) == before
    print("[EMERG] PASS — not opted in → byte-identical, no key\n")


def test_check_flag_absent_no_key():
    orch = _orch(emergency_client=lambda q: {"active": True, "hazard": "wildfire"})
    orch._emergency_request = {}  # opted-in dict but no check flag
    result = _success_result()
    orch._maybe_check_active_emergencies(result)
    assert "active_emergencies" not in result
    print("[EMERG] PASS — check flag absent → no-op\n")


def test_day_plans_never_mutated():
    def client(q):
        return {"active": True, "hazard": "wildfire", "severity": "extreme",
                "source": "calfire", "as_of": "2026-06-21"}
    orch = _orch(emergency_client=client)
    orch._emergency_request = {"check": True}
    result = _success_result()
    before = json.dumps(result["day_plans"], sort_keys=True)
    orch._maybe_check_active_emergencies(result)
    assert json.dumps(result["day_plans"], sort_keys=True) == before, "day_plans mutated!"
    assert "active_emergencies" in result
    print("[EMERG] PASS — day_plans byte-identical; overlay isolated in own key\n")


# --------------------------------------------------------------------------
# HONESTY / fail-conservative
# --------------------------------------------------------------------------
def test_no_feed_honest_unavailable_never_allclear():
    orch = _orch(emergency_client=None)  # no feed configured
    orch._emergency_request = {"check": True}
    result = _success_result()
    orch._maybe_check_active_emergencies(result)
    ae = result["active_emergencies"]
    assert len(ae) == 2
    for e in ae:
        assert e["status"] == "unavailable"
        assert "unavailable" in e["note"].lower()
        # CRITICAL: an unconfigured feed must NEVER read as a safe all-clear.
        assert e["status"] != "clear"
    print("[EMERG] PASS — no feed → honest 'unavailable', never a fabricated all-clear\n")


def test_feed_raises_fail_conservative():
    def boom(q):
        raise RuntimeError("feed down")
    orch = _orch(emergency_client=boom)
    orch._emergency_request = {"check": True}
    result = _success_result()
    orch._maybe_check_active_emergencies(result)
    assert result["outcome"] == "success"
    assert all(e["status"] == "unavailable" for e in result["active_emergencies"])
    assert orch._call_emergency_feed({"city": "x"}) is None
    print("[EMERG] PASS — feed raises → None, honest note, still success\n")


def test_active_declares_do_not_travel():
    def client(q):
        # Only Los Angeles has an active wildfire; Sydney is clear.
        if q.get("city") == "los angeles":
            return {"active": True, "hazard": "wildfire", "severity": "extreme",
                    "headline": "Mandatory evacuation — Palisades fire",
                    "source": "calfire", "as_of": "2026-06-21"}
        return {"active": False, "source": "ses-nsw", "as_of": "2026-06-21"}
    orch = _orch(emergency_client=client)
    orch._emergency_request = {"check": True}
    result = _success_result()
    orch._maybe_check_active_emergencies(result)
    ae = {e["city"]: e for e in result["active_emergencies"]}
    la = ae["los angeles"]
    assert la["status"] == "active"
    assert "DO NOT TRAVEL" in la["notice"]
    assert la["hazard"] == "wildfire" and la["severity"] == "extreme"
    assert la["source"] == "calfire"  # carried verbatim, never invented
    assert "evacuation" in la["advice"].lower()
    syd = ae["sydney"]
    assert syd["status"] == "clear"
    print("[EMERG] PASS — active leg → DO NOT TRAVEL w/ verbatim feed fields; clear leg clear\n")


def test_partial_active_missing_fields_are_null_never_invented():
    """An active declaration missing hazard/severity/headline surfaces those as
    null — honest (no fabrication), and `advice` falls back to the conservative
    'do not travel' default rather than an invented specific."""
    def client(q):
        return {"active": True, "source": "gov-feed"}  # active but bare
    orch = _orch(emergency_client=client)
    orch._emergency_request = {"check": True}
    result = _success_result()
    orch._maybe_check_active_emergencies(result)
    e = result["active_emergencies"][0]
    assert e["status"] == "active"
    assert e["hazard"] is None and e["severity"] is None and e["headline"] is None
    assert "do not travel" in e["advice"].lower()  # safe default, not invented
    assert "DO NOT TRAVEL" in e["notice"]
    print("[EMERG] PASS — partial-active → null missing fields, conservative advice default\n")


if __name__ == "__main__":
    for t in (test_not_opted_in_byte_identical,
              test_check_flag_absent_no_key,
              test_day_plans_never_mutated,
              test_no_feed_honest_unavailable_never_allclear,
              test_feed_raises_fail_conservative,
              test_active_declares_do_not_travel,
              test_partial_active_missing_fields_are_null_never_invented):
        t()
    print("ALL #51 ACTIVE-EMERGENCY TESTS PASSED")
