"""
test_insurance_agent_cov2.py — Coverage closure tests for insurance_agent.py

Deterministic, var-0-safe tests targeting uncovered regions:
  - Line 315: policy_clauses_public() return path
  - Line 500: assess_coverage() KeyError for unknown policy_id
  - Lines 519-523: Peril set dedup + medical_evacuation injection
  - Line 664: explain_exclusion() when peril not in assessment
  - Lines 693-708: explain_exclusion() branches (SUB_LIMITED, CONDITIONAL, COVERED with clauses)
  - Lines 718-732: explain_exclusion() branches without clauses
  - Lines 776-814: _llm_rationale() paths (no key, httpx import fail, exceptions)
  - Line 859: fetch_policy_terms() KeyError
  - Lines 1026, 1082, 1090, 1129, 1135: A2A handler ValueError paths
  - Lines 1140-1155: _explain_handler() assessment building + LLM path
  - Lines 1176-1215: _extract_payload() text part extraction

No network, no LLM, no paid APIs. Uses starlette TestClient + in-process functions.
"""

from __future__ import annotations

import json
import os
import sys
import uuid

sys.path.insert(0, os.path.dirname(__file__))

from starlette.testclient import TestClient

from core import contracts as C
from agents import insurance_agent as ins
from agents.insurance_agent import (
    CoverageStatus,
    DEFAULT_POLICY_ID,
    InsuranceAgent,
    assess_coverage,
    explain_exclusion,
    fetch_policy_terms,
    policy_clauses_public,
    _POLICY_CLAUSES,
    _POLICY_CATALOG,
)


# ===========================================================================
# Line 315: policy_clauses_public() return statement
# ===========================================================================

def test_policy_clauses_public_return_structure():
    """Line 315: policy_clauses_public() returns correct list of dicts."""
    clauses = policy_clauses_public("P-STD-01")
    assert isinstance(clauses, list)
    assert len(clauses) > 0
    for c in clauses:
        assert isinstance(c, dict)
        assert "clause_id" in c
        assert "clause_type" in c
        assert "peril_class" in c
        assert "clause_text" in c
        assert "sub_limit_cents" in c
        assert "condition_text" in c
        assert "provenance" in c
        assert "source_url" in c


def test_policy_clauses_public_prem_02():
    """Line 315: policy_clauses_public() also works for P-PREM-02."""
    clauses = policy_clauses_public("P-PREM-02")
    assert isinstance(clauses, list)
    assert len(clauses) > 0
    # P-PREM-02 should have different clauses than P-STD-01
    std_ids = {c["clause_id"] for c in policy_clauses_public("P-STD-01")}
    prem_ids = {c["clause_id"] for c in clauses}
    assert std_ids != prem_ids


# ===========================================================================
# Line 500: assess_coverage() KeyError for unknown policy_id
# ===========================================================================

def test_assess_coverage_unknown_policy_id_keyerror():
    """Line 500: assess_coverage() raises KeyError for unknown policy_id."""
    try:
        assess_coverage(peril_set=["medical"], policy_id="P-UNKNOWN-999")
        assert False, "should have raised KeyError"
    except KeyError as e:
        assert "unknown policy_id" in str(e).lower()


# ===========================================================================
# Lines 519-523: Peril set dedup + medical_evacuation injection
# ===========================================================================

def test_peril_set_dedup_preserves_order():
    """Lines 522-528: peril set dedup preserves first-seen order (deterministic)."""
    # Pass duplicates in a specific order
    perils = ["medical", "travel_delay", "medical", "civil_unrest", "travel_delay"]
    a = assess_coverage(peril_set=perils, policy_id="P-STD-01")
    peril_classes = [r["peril_class"] for r in a["per_peril"]]
    # Should be de-duped but preserve order: medical, travel_delay, civil_unrest
    assert peril_classes[0] == "medical"
    assert peril_classes[1] == "travel_delay"
    assert peril_classes[2] == "civil_unrest"
    assert len(peril_classes) == 3  # de-duped


def test_medical_evacuation_injected_from_top_level():
    """Line 518-520: evacuation_recommended at top level injects medical_evacuation."""
    a = assess_coverage(
        peril_set=["travel_delay"],
        health_assessment={"evacuation_recommended": True},
    )
    peril_classes = [r["peril_class"] for r in a["per_peril"]]
    assert "medical_evacuation" in peril_classes


def test_medical_evacuation_not_double_injected():
    """Line 519: if medical_evacuation already in peril_set, don't add twice."""
    a = assess_coverage(
        peril_set=["medical_evacuation", "travel_delay"],
        health_assessment={"evacuation_recommended": True},
    )
    peril_classes = [r["peril_class"] for r in a["per_peril"]]
    # Should appear only once
    assert peril_classes.count("medical_evacuation") == 1


def test_health_assessment_per_destination_evacuation_loop():
    """Lines 513-516: per_destination loop path for evacuation detection."""
    a = assess_coverage(
        peril_set=[],
        health_assessment={
            "evacuation_recommended": False,
            "per_destination": [
                {"place_key": "nepal", "evacuation_recommended": False},
                {"place_key": "south_sudan", "evacuation_recommended": True},
            ],
        },
    )
    peril_classes = [r["peril_class"] for r in a["per_peril"]]
    assert "medical_evacuation" in peril_classes


def test_health_assessment_per_destination_non_dict_skipped():
    """Lines 514: non-dict items in per_destination are safely skipped."""
    a = assess_coverage(
        peril_set=[],
        health_assessment={
            "evacuation_recommended": False,
            "per_destination": [
                "not a dict",  # should be skipped
                {"place_key": "valid", "evacuation_recommended": True},
            ],
        },
    )
    peril_classes = [r["peril_class"] for r in a["per_peril"]]
    assert "medical_evacuation" in peril_classes


def test_health_assessment_empty_dict():
    """Lines 508-520: health_assessment={} doesn't crash, has no effect."""
    a = assess_coverage(peril_set=["medical"], health_assessment={})
    peril_classes = [r["peril_class"] for r in a["per_peril"]]
    assert "medical_evacuation" not in peril_classes


# ===========================================================================
# Line 664: explain_exclusion() when peril not in assessment
# ===========================================================================

def test_explain_exclusion_peril_not_in_assessment():
    """Line 664: explain_exclusion() returns UNDETERMINED when peril missing from per_peril."""
    a = assess_coverage(peril_set=["medical"], policy_id="P-STD-01")
    # Ask about a peril that's not in the assessment
    result = explain_exclusion(a, "travel_delay")  # travel_delay not in assessment
    assert result["status"] == CoverageStatus.UNDETERMINED
    assert "was not assessed" in result["headline"]
    assert result["matched_clauses"] == []


# ===========================================================================
# Lines 693-708: explain_exclusion() branches with clauses
# ===========================================================================

def test_explain_exclusion_sublimit_with_clauses():
    """Line 692-699: SUB_LIMITED peril with clause text."""
    a = assess_coverage(peril_set=["medical"], policy_id="P-STD-01")
    result = explain_exclusion(a, "medical")
    assert result["status"] == CoverageStatus.SUB_LIMITED
    assert "SUB-LIMITS" in result["headline"]
    assert "capped at" in result["headline"]
    assert "5000000" in result["headline"]  # sub_limit_cents from clause


def test_explain_exclusion_conditional_with_clauses():
    """Line 700-705: CONDITIONAL peril with clause text."""
    a = assess_coverage(peril_set=["adventure_activity"], policy_id="P-STD-01")
    result = explain_exclusion(a, "adventure_activity")
    assert result["status"] == CoverageStatus.CONDITIONAL
    assert "ONLY CONDITIONALLY" in result["headline"]
    assert "adventure-activity add-on" in result["headline"]


def test_explain_exclusion_covered_with_clauses():
    """Line 706-711: COVERED peril with clause text."""
    a = assess_coverage(peril_set=["travel_delay"], policy_id="P-STD-01")
    result = explain_exclusion(a, "travel_delay")
    assert result["status"] == CoverageStatus.COVERED
    assert "covers" in result["headline"].lower()
    assert "COV-DELAY-1" in result["headline"]


def test_explain_exclusion_excluded_without_contrast_covered_list_empty():
    """Line 679-691: EXCLUDED branch when covered_perils list is empty."""
    # Build assessment with only excluded perils
    a = assess_coverage(peril_set=["civil_unrest", "war"], policy_id="P-STD-01")
    result = explain_exclusion(a, "civil_unrest")
    assert result["status"] == CoverageStatus.EXCLUDED
    headline = result["headline"]
    # The pure-exclusion headline is a clean EXCLUDES statement citing the clause id, with NO
    # "also covers"/"NOT for" contrast clause (covered list is empty). (Prev assertion was a
    # tautological OR — 'contrast' never appears, so it always passed.)
    assert "EXCLUDES" in headline
    assert "EXC-UNREST-1" in headline
    assert "NOT for" not in headline and "also cover" not in headline.lower()


# ===========================================================================
# Lines 718-732: explain_exclusion() branches WITHOUT clauses (clause text unavailable)
# ===========================================================================

def test_explain_exclusion_sublimit_no_clauses():
    """Line 718-724: SUB_LIMITED with no clause text available."""
    synthetic = {
        "policy_id": "P-STD-01",
        "per_peril": [
            {
                "peril_class": "medical",
                "status": CoverageStatus.SUB_LIMITED,
                "matched_clause_ids": ["NONEXISTENT-CLAUSE"],  # unavailable
                "sub_limit_cents": 5000000,
                "condition": None,
            }
        ],
        "covered_perils": [],
    }
    result = explain_exclusion(synthetic, "medical")
    assert result["status"] == CoverageStatus.SUB_LIMITED
    assert "SUB-LIMITS" in result["headline"]
    assert "clause text unavailable" in result["headline"]


def test_explain_exclusion_conditional_no_clauses():
    """Line 725-729: CONDITIONAL with no clause text available."""
    synthetic = {
        "policy_id": "P-STD-01",
        "per_peril": [
            {
                "peril_class": "adventure_activity",
                "status": CoverageStatus.CONDITIONAL,
                "matched_clause_ids": ["NONEXISTENT-COND"],
                "sub_limit_cents": None,
                "condition": "some condition",
            }
        ],
        "covered_perils": [],
    }
    result = explain_exclusion(synthetic, "adventure_activity")
    assert result["status"] == CoverageStatus.CONDITIONAL
    assert "ONLY CONDITIONALLY" in result["headline"]
    assert "conditions unspecified" in result["headline"]


def test_explain_exclusion_excluded_no_clauses():
    """Line 712-717: EXCLUDED with no clause text (clause text unavailable)."""
    synthetic = {
        "policy_id": "P-STD-01",
        "per_peril": [
            {
                "peril_class": "war",
                "status": CoverageStatus.EXCLUDED,
                "matched_clause_ids": ["NONEXISTENT-EXCLUDE"],
                "sub_limit_cents": None,
                "condition": None,
            }
        ],
        "covered_perils": [],
    }
    result = explain_exclusion(synthetic, "war")
    assert result["status"] == CoverageStatus.EXCLUDED
    assert "EXCLUDES" in result["headline"]
    assert "do NOT assume covered" in result["headline"]


def test_explain_exclusion_else_branch_covered_no_clauses():
    """Line 730-735: final else (COVERED or UNDETERMINED with no clauses)."""
    synthetic = {
        "policy_id": "P-STD-01",
        "per_peril": [
            {
                "peril_class": "supplier_insolvency",
                "status": CoverageStatus.UNDETERMINED,
                "matched_clause_ids": [],
                "sub_limit_cents": None,
                "condition": None,
            }
        ],
        "covered_perils": [],
    }
    result = explain_exclusion(synthetic, "supplier_insolvency")
    assert result["status"] == CoverageStatus.UNDETERMINED
    assert "no clause addressing" in result["headline"]
    assert "UNDETERMINED" in result["headline"]


# ===========================================================================
# Line 677: region parameter in explain_exclusion()
# ===========================================================================

def test_explain_exclusion_region_in_where_clause():
    """Line 677: region parameter is included in where clause for contrast (when covered_perils exist)."""
    # For EXCLUDED with contrast, region appears in the contrast message
    a = assess_coverage(peril_set=["civil_unrest", "travel_delay"], policy_id="P-STD-01")
    result_with_region = explain_exclusion(a, "civil_unrest", region="South Sudan")
    # Region appears in the contrast sentence within the headline
    assert "South Sudan" in result_with_region["headline"]
    
    # Without region, it's not included
    result_no_region = explain_exclusion(a, "civil_unrest", region=None)
    assert "South Sudan" not in result_no_region["headline"]


# ===========================================================================
# Lines 776-814: _llm_rationale() paths
# ===========================================================================

def test_llm_rationale_no_api_key(monkeypatch):
    """Line 776-777: returns None when DASHSCOPE_API_KEY is empty."""
    monkeypatch.setattr(ins, "DASHSCOPE_API_KEY", "")
    clauses = [{"clause_text": "test clause"}]
    result = ins._llm_rationale("civil_unrest", CoverageStatus.EXCLUDED, clauses)
    assert result is None


def test_llm_rationale_httpx_import_fails(monkeypatch):
    """Line 778-781: returns None when httpx import fails."""
    monkeypatch.setattr(ins, "DASHSCOPE_API_KEY", "sk-test-key")
    
    # Mock the import to fail
    import builtins
    original_import = builtins.__import__
    
    def mock_import(name, *args, **kwargs):
        if name == "httpx":
            raise ImportError("httpx not available")
        return original_import(name, *args, **kwargs)
    
    monkeypatch.setattr(builtins, "__import__", mock_import)
    clauses = [{"clause_text": "test clause"}]
    result = ins._llm_rationale("civil_unrest", CoverageStatus.EXCLUDED, clauses)
    assert result is None


def test_llm_rationale_general_exception(monkeypatch):
    """Line 812-814: exception in LLM call logs and returns None."""
    monkeypatch.setattr(ins, "DASHSCOPE_API_KEY", "sk-test-key")
    
    # Mock httpx.post to raise an exception
    class MockHttpx:
        @staticmethod
        def post(*args, **kwargs):
            raise RuntimeError("Network error")
    
    import sys
    monkeypatch.setitem(sys.modules, "httpx", MockHttpx())
    
    clauses = [{"clause_text": "test clause"}]
    result = ins._llm_rationale("civil_unrest", CoverageStatus.EXCLUDED, clauses)
    assert result is None


# ===========================================================================
# Line 859: fetch_policy_terms() KeyError
# ===========================================================================

def test_fetch_policy_terms_unknown_policy_keyerror():
    """Line 858-859: fetch_policy_terms() raises KeyError for unknown policy."""
    try:
        fetch_policy_terms("P-UNKNOWN-XYZ")
        assert False, "should have raised KeyError"
    except KeyError as e:
        assert "unknown policy_id" in str(e).lower()


# ===========================================================================
# Lines 1026, 1082, 1090: A2A handler ValueError paths
# ===========================================================================

def test_assess_handler_non_dict_payload(monkeypatch):
    """Line 1025-1029: _assess_handler raises ValueError for non-dict payload."""
    agent = InsuranceAgent()
    client = TestClient(agent.build_app())
    
    body = {
        "jsonrpc": "2.0", "id": str(uuid.uuid4()), "method": "message/send",
        "params": {"message": {
            "messageId": str(uuid.uuid4()), "role": "user",
            "parts": [{"kind": "data", "data": "not a dict or list"}],  # string, not dict
            "metadata": {"skillId": "insurance.assess_coverage"},
        }},
    }
    resp = client.post("/", json=body)
    assert resp.status_code == 200
    task = resp.json()["result"]
    assert task["status"]["state"] == "failed"


def test_coverage_gap_handler_non_dict_payload():
    """Line 1081-1086: _coverage_gap_handler raises ValueError for non-dict payload."""
    agent = InsuranceAgent()
    client = TestClient(agent.build_app())
    
    body = {
        "jsonrpc": "2.0", "id": str(uuid.uuid4()), "method": "message/send",
        "params": {"message": {
            "messageId": str(uuid.uuid4()), "role": "user",
            "parts": [{"kind": "data", "data": "not a dict"}],
            "metadata": {"skillId": "insurance.coverage_gap"},
        }},
    }
    resp = client.post("/", json=body)
    assert resp.status_code == 200
    task = resp.json()["result"]
    assert task["status"]["state"] == "failed"


def test_coverage_gap_handler_missing_user_policy():
    """Line 1088-1092: _coverage_gap_handler raises ValueError when user_policy missing."""
    agent = InsuranceAgent()
    client = TestClient(agent.build_app())
    
    body = {
        "jsonrpc": "2.0", "id": str(uuid.uuid4()), "method": "message/send",
        "params": {"message": {
            "messageId": str(uuid.uuid4()), "role": "user",
            "parts": [{"kind": "data", "data": {"peril_set": ["medical"]}}],  # no user_policy
            "metadata": {"skillId": "insurance.coverage_gap"},
        }},
    }
    resp = client.post("/", json=body)
    assert resp.status_code == 200
    task = resp.json()["result"]
    assert task["status"]["state"] == "failed"


def test_coverage_gap_handler_user_policy_not_dict():
    """Line 1089: user_policy must be dict, not string."""
    agent = InsuranceAgent()
    client = TestClient(agent.build_app())
    
    body = {
        "jsonrpc": "2.0", "id": str(uuid.uuid4()), "method": "message/send",
        "params": {"message": {
            "messageId": str(uuid.uuid4()), "role": "user",
            "parts": [{"kind": "data", "data": {"user_policy": "not a dict"}}],
            "metadata": {"skillId": "insurance.coverage_gap"},
        }},
    }
    resp = client.post("/", json=body)
    assert resp.status_code == 200
    task = resp.json()["result"]
    assert task["status"]["state"] == "failed"


# ===========================================================================
# Lines 1129, 1135: _explain_handler ValueError paths
# ===========================================================================

def test_explain_handler_non_dict_payload():
    """Line 1128-1132: _explain_handler raises ValueError for non-dict payload."""
    agent = InsuranceAgent()
    client = TestClient(agent.build_app())
    
    body = {
        "jsonrpc": "2.0", "id": str(uuid.uuid4()), "method": "message/send",
        "params": {"message": {
            "messageId": str(uuid.uuid4()), "role": "user",
            "parts": [{"kind": "data", "data": "not a dict"}],
            "metadata": {"skillId": "insurance.explain_exclusion"},
        }},
    }
    resp = client.post("/", json=body)
    assert resp.status_code == 200
    task = resp.json()["result"]
    assert task["status"]["state"] == "failed"


def test_explain_handler_missing_peril_class():
    """Line 1133-1135: _explain_handler raises ValueError when peril_class missing."""
    agent = InsuranceAgent()
    client = TestClient(agent.build_app())
    
    body = {
        "jsonrpc": "2.0", "id": str(uuid.uuid4()), "method": "message/send",
        "params": {"message": {
            "messageId": str(uuid.uuid4()), "role": "user",
            "parts": [{"kind": "data", "data": {}}],  # no peril_class
            "metadata": {"skillId": "insurance.explain_exclusion"},
        }},
    }
    resp = client.post("/", json=body)
    assert resp.status_code == 200
    task = resp.json()["result"]
    assert task["status"]["state"] == "failed"


def test_explain_handler_empty_string_peril_class():
    """Line 1133-1135: peril_class empty string is also invalid."""
    agent = InsuranceAgent()
    client = TestClient(agent.build_app())
    
    body = {
        "jsonrpc": "2.0", "id": str(uuid.uuid4()), "method": "message/send",
        "params": {"message": {
            "messageId": str(uuid.uuid4()), "role": "user",
            "parts": [{"kind": "data", "data": {"peril_class": ""}}],
            "metadata": {"skillId": "insurance.explain_exclusion"},
        }},
    }
    resp = client.post("/", json=body)
    assert resp.status_code == 200
    task = resp.json()["result"]
    assert task["status"]["state"] == "failed"


# ===========================================================================
# Lines 1140-1155: _explain_handler assessment building + LLM path
# ===========================================================================

def test_explain_handler_standalone_assessment_build():
    """Line 1140-1145: _explain_handler builds assessment when none supplied."""
    agent = InsuranceAgent()
    client = TestClient(agent.build_app())
    
    body = {
        "jsonrpc": "2.0", "id": str(uuid.uuid4()), "method": "message/send",
        "params": {"message": {
            "messageId": str(uuid.uuid4()), "role": "user",
            "parts": [{"kind": "data", "data": {
                "peril_class": "civil_unrest",
                "policy_id": "P-STD-01",
                "insured_trip_cost_cents": 100000,
            }}],  # no assessment provided
            "metadata": {"skillId": "insurance.explain_exclusion"},
        }},
    }
    resp = client.post("/", json=body)
    assert resp.status_code == 200
    task = resp.json()["result"]
    assert task["status"]["state"] == "completed"
    data = task["artifacts"][0]["parts"][0]["data"]
    assert data["status"] == CoverageStatus.EXCLUDED


def test_explain_handler_with_supplied_assessment():
    """Lines 1137-1148: _explain_handler uses supplied assessment."""
    agent = InsuranceAgent()
    client = TestClient(agent.build_app())
    
    # First, get an assessment
    assess_body = {
        "jsonrpc": "2.0", "id": str(uuid.uuid4()), "method": "message/send",
        "params": {"message": {
            "messageId": str(uuid.uuid4()), "role": "user",
            "parts": [{"kind": "data", "data": {
                "peril_set": ["civil_unrest", "travel_delay"],
                "policy_id": "P-STD-01",
            }}],
            "metadata": {"skillId": "insurance.assess_coverage"},
        }},
    }
    assess_resp = client.post("/", json=assess_body)
    assessment = assess_resp.json()["result"]["artifacts"][0]["parts"][0]["data"]
    
    # Now explain using that assessment
    explain_body = {
        "jsonrpc": "2.0", "id": str(uuid.uuid4()), "method": "message/send",
        "params": {"message": {
            "messageId": str(uuid.uuid4()), "role": "user",
            "parts": [{"kind": "data", "data": {
                "peril_class": "civil_unrest",
                "assessment": assessment,
            }}],
            "metadata": {"skillId": "insurance.explain_exclusion"},
        }},
    }
    explain_resp = client.post("/", json=explain_body)
    assert explain_resp.status_code == 200
    task = explain_resp.json()["result"]
    assert task["status"]["state"] == "completed"


def test_explain_handler_llm_disabled(monkeypatch):
    """Line 1152-1157: use_llm=False → rationale_source is 'deterministic'."""
    monkeypatch.setattr(ins, "DASHSCOPE_API_KEY", "")
    agent = InsuranceAgent()
    client = TestClient(agent.build_app())
    
    body = {
        "jsonrpc": "2.0", "id": str(uuid.uuid4()), "method": "message/send",
        "params": {"message": {
            "messageId": str(uuid.uuid4()), "role": "user",
            "parts": [{"kind": "data", "data": {
                "peril_class": "civil_unrest",
                "use_llm": False,
            }}],
            "metadata": {"skillId": "insurance.explain_exclusion"},
        }},
    }
    resp = client.post("/", json=body)
    assert resp.status_code == 200
    data = resp.json()["result"]["artifacts"][0]["parts"][0]["data"]
    assert data["rationale_source"] == "deterministic"
    assert "llm_rationale" not in data


def test_explain_handler_llm_enabled_but_no_key(monkeypatch):
    """Line 1152-1155: use_llm=True but no key → llm_rationale=None, falls back to deterministic."""
    monkeypatch.setattr(ins, "DASHSCOPE_API_KEY", "")
    agent = InsuranceAgent()
    client = TestClient(agent.build_app())
    
    body = {
        "jsonrpc": "2.0", "id": str(uuid.uuid4()), "method": "message/send",
        "params": {"message": {
            "messageId": str(uuid.uuid4()), "role": "user",
            "parts": [{"kind": "data", "data": {
                "peril_class": "civil_unrest",
                "use_llm": True,
            }}],
            "metadata": {"skillId": "insurance.explain_exclusion"},
        }},
    }
    resp = client.post("/", json=body)
    assert resp.status_code == 200
    data = resp.json()["result"]["artifacts"][0]["parts"][0]["data"]
    assert data["llm_rationale"] is None
    assert data["rationale_source"] == "deterministic"


# ===========================================================================
# Lines 1176-1215: _extract_payload() text part extraction
# ===========================================================================

def test_extract_payload_text_part_json():
    """Lines 1181-1188: extract payload from text part with JSON."""
    agent = InsuranceAgent()
    client = TestClient(agent.build_app())
    
    payload_dict = {
        "peril_class": "medical",
        "policy_id": "P-STD-01",
    }
    
    body = {
        "jsonrpc": "2.0", "id": str(uuid.uuid4()), "method": "message/send",
        "params": {"message": {
            "messageId": str(uuid.uuid4()), "role": "user",
            "parts": [{"kind": "text", "text": json.dumps(payload_dict)}],
            "metadata": {"skillId": "insurance.explain_exclusion"},
        }},
    }
    resp = client.post("/", json=body)
    assert resp.status_code == 200
    task = resp.json()["result"]
    assert task["status"]["state"] == "completed"


def test_extract_payload_text_part_invalid_json():
    """Line 1184: invalid JSON in text part is skipped."""
    agent = InsuranceAgent()
    client = TestClient(agent.build_app())
    
    body = {
        "jsonrpc": "2.0", "id": str(uuid.uuid4()), "method": "message/send",
        "params": {"message": {
            "messageId": str(uuid.uuid4()), "role": "user",
            "parts": [
                {"kind": "text", "text": "not valid json"},  # invalid
                {"kind": "data", "data": {"peril_class": "medical"}},  # fallback to data part
            ],
            "metadata": {"skillId": "insurance.explain_exclusion"},
        }},
    }
    resp = client.post("/", json=body)
    assert resp.status_code == 200
    task = resp.json()["result"]
    # Should succeed because data part was used as fallback
    assert task["status"]["state"] == "completed"


def test_extract_payload_text_part_json_array():
    """Line 1185: text part with JSON array is accepted."""
    agent = InsuranceAgent()
    client = TestClient(agent.build_app())
    
    body = {
        "jsonrpc": "2.0", "id": str(uuid.uuid4()), "method": "message/send",
        "params": {"message": {
            "messageId": str(uuid.uuid4()), "role": "user",
            "parts": [{"kind": "text", "text": '[1, 2, 3]'}],
            "metadata": {"skillId": "insurance.assess_coverage"},
        }},
    }
    resp = client.post("/", json=body)
    assert resp.status_code == 200
    task = resp.json()["result"]
    # Array is accepted but assess_coverage will fail (no peril_set key)
    assert task["status"]["state"] == "failed"


def test_extract_payload_no_parts():
    """Line 1171: message with no parts returns None → JSON-RPC error."""
    agent = InsuranceAgent()
    client = TestClient(agent.build_app())
    
    body = {
        "jsonrpc": "2.0", "id": str(uuid.uuid4()), "method": "message/send",
        "params": {"message": {
            "messageId": str(uuid.uuid4()), "role": "user",
            "parts": [],
            "metadata": {"skillId": "insurance.assess_coverage"},
        }},
    }
    resp = client.post("/", json=body)
    assert resp.status_code == 200
    data = resp.json()
    # Empty parts array triggers JSON-RPC error
    assert "error" in data


def test_extract_payload_data_part_dict_directly():
    """Lines 1172-1175: data part with dict is extracted directly."""
    agent = InsuranceAgent()
    client = TestClient(agent.build_app())
    
    body = {
        "jsonrpc": "2.0", "id": str(uuid.uuid4()), "method": "message/send",
        "params": {"message": {
            "messageId": str(uuid.uuid4()), "role": "user",
            "parts": [{"kind": "data", "data": {"peril_set": [], "policy_id": "P-STD-01"}}],
            "metadata": {"skillId": "insurance.assess_coverage"},
        }},
    }
    resp = client.post("/", json=body)
    assert resp.status_code == 200
    task = resp.json()["result"]
    assert task["status"]["state"] == "completed"


def test_extract_payload_data_part_list_directly():
    """Lines 1173-1175: data part with list is extracted directly."""
    agent = InsuranceAgent()
    client = TestClient(agent.build_app())
    
    body = {
        "jsonrpc": "2.0", "id": str(uuid.uuid4()), "method": "message/send",
        "params": {"message": {
            "messageId": str(uuid.uuid4()), "role": "user",
            "parts": [{"kind": "data", "data": ["medical"]}],
            "metadata": {"skillId": "insurance.assess_coverage"},
        }},
    }
    resp = client.post("/", json=body)
    assert resp.status_code == 200
    task = resp.json()["result"]
    # List is accepted but assess_coverage will fail (peril_set should be in dict)
    assert task["status"]["state"] == "failed"


def test_extract_payload_data_part_string_json():
    """Line 1176-1180: data part with JSON string is parsed."""
    agent = InsuranceAgent()
    client = TestClient(agent.build_app())
    
    body = {
        "jsonrpc": "2.0", "id": str(uuid.uuid4()), "method": "message/send",
        "params": {"message": {
            "messageId": str(uuid.uuid4()), "role": "user",
            "parts": [{"kind": "data", "data": '{"peril_set": [], "policy_id": "P-STD-01"}'}],
            "metadata": {"skillId": "insurance.assess_coverage"},
        }},
    }
    resp = client.post("/", json=body)
    assert resp.status_code == 200
    task = resp.json()["result"]
    assert task["status"]["state"] == "completed"


def test_extract_payload_data_part_string_invalid_json():
    """Line 1177-1180: invalid JSON string in data part is skipped."""
    agent = InsuranceAgent()
    client = TestClient(agent.build_app())
    
    body = {
        "jsonrpc": "2.0", "id": str(uuid.uuid4()), "method": "message/send",
        "params": {"message": {
            "messageId": str(uuid.uuid4()), "role": "user",
            "parts": [
                {"kind": "data", "data": "not json"},
                {"kind": "data", "data": {"peril_set": [], "policy_id": "P-STD-01"}},
            ],
            "metadata": {"skillId": "insurance.assess_coverage"},
        }},
    }
    resp = client.post("/", json=body)
    assert resp.status_code == 200
    task = resp.json()["result"]
    # Should succeed on second part
    assert task["status"]["state"] == "completed"


if __name__ == "__main__":
    import subprocess
    raise SystemExit(subprocess.call(["python3", "-m", "pytest", __file__, "-v"]))
