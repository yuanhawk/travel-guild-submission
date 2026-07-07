"""
test_fraud_agent_cov2.py — Coverage gap closure for fraud_agent.py.

Targets uncovered lines: 293-341 (_assert_seed_table_valid validation paths),
370/372-373 (solvency_profile_public null paths), 393-395 (solvency_table_public
collection), 441-459 (validate_consent_token edge cases), 620-670 (validate_rationale
+ _llm_rationale paths), 803-891 (A2A handlers and main).

CI-safe: no LLM, no network, no DashScope. Deterministic, var-0.
"""

from __future__ import annotations

import os
import sys
import json
import uuid
from unittest import mock

sys.path.insert(0, os.path.dirname(__file__))

from starlette.testclient import TestClient

from agents import fraud_agent as fraud
from agents.fraud_agent import (
    FraudAgent,
    RiskBand,
    canonical_counterparty_id,
    fetch_solvency_profile,
    solvency_profile_public,
    solvency_table_public,
    validate_consent_token,
    validate_rationale,
    _llm_rationale,
    explain,
    vet,
    vet_counterparty,
    _assert_seed_table_valid,
    _SOLVENCY_BY_COUNTERPARTY,
)

# Seeded test fixtures
CLEAR_CP = "carrier-singapore-airlines"      # score 91 → clear
ELEVATED_CP = "carrier-valuair-lcc"          # score 52 → elevated
BLOCKED_CP = "carrier-skylark-budget-air"    # score 28 → blocked
UNKNOWN_CP = "carrier-who-knows-airlines"    # no seeded profile → unknown


def consent_for(counterparty_id: str, band: str, nonce: str = "n1") -> str:
    """A VALID structured consent grant."""
    return f"consent:{counterparty_id}:{band}:{nonce}"


# ===========================================================================
# Lines 293–341: _assert_seed_table_valid() — validation error paths
# ===========================================================================

def test_assert_seed_table_valid_passes_on_module_load():
    """
    Line 304: The module-load assertion runs and passes (via import).
    Verify the seed table passes all checks: valid scores, valid kinds, has source.
    """
    # Re-run the validation explicitly to confirm no exceptions.
    fraud._assert_seed_table_valid()  # Must not raise.


def test_seed_table_structure_has_valid_scores():
    """Lines 290–295: Score must be an int in [0, 100]."""
    for cid, row in _SOLVENCY_BY_COUNTERPARTY.items():
        score = row.get("solvency_score")
        assert isinstance(score, int), f"{cid}: score must be int, got {type(score)}"
        assert 0 <= score <= 100, f"{cid}: score {score} out of 0–100"


def test_seed_table_structure_has_valid_kind():
    """Lines 296–298: kind must be in the closed set (TRANSPORT, HOTEL, OTA)."""
    from core.contracts import CounterpartyKind
    valid_kinds = frozenset(c.value for c in CounterpartyKind) | {"ota"}
    for cid, row in _SOLVENCY_BY_COUNTERPARTY.items():
        kind = row.get("kind")
        assert kind in valid_kinds, f"{cid}: kind {kind!r} not in {sorted(valid_kinds)}"


def test_seed_table_structure_has_source():
    """Lines 300–301: Every counterparty row must have a 'source' field (provenance)."""
    for cid, row in _SOLVENCY_BY_COUNTERPARTY.items():
        assert row.get("source"), f"{cid}: missing provenance source"


# ===========================================================================
# Lines 368–373: solvency_profile_public() — null paths
# ===========================================================================

def test_solvency_profile_public_returns_none_for_null_id():
    """
    Lines 368–370: canonical_counterparty_id(None/empty) → None,
    solvency_profile_public returns None.
    """
    assert solvency_profile_public(None) is None
    assert solvency_profile_public("") is None
    assert solvency_profile_public("   ") is None


def test_solvency_profile_public_returns_none_for_unknown_counterparty():
    """
    Lines 371–373: fetch_solvency_profile(unknown_cid) returns (None, _),
    solvency_profile_public returns None (same gap the society sees).
    """
    assert solvency_profile_public("carrier-unknown-airline") is None


def test_solvency_profile_public_returns_dict_for_known_counterparty():
    """
    Lines 374–384: For a known counterparty, returns the full profile dict.
    """
    p = solvency_profile_public(BLOCKED_CP)
    assert isinstance(p, dict)
    assert p["counterparty_id"] == BLOCKED_CP
    assert p["catalog_id"] == BLOCKED_CP
    assert p["kind"] == "transport"
    assert p["solvency_score"] == 28
    assert p["risk_band"] == RiskBand.BLOCKED
    assert p["committable_without_consent"] is False
    assert "source" in p


# ===========================================================================
# Lines 387–396: solvency_table_public() — full table accessor
# ===========================================================================

def test_solvency_table_public_returns_all_seeded():
    """
    Lines 393–396: Collects all profiles via list comprehension,
    stable order (sorted by counterparty_id).
    """
    table = solvency_table_public()
    assert isinstance(table, list)
    assert len(table) == len(_SOLVENCY_BY_COUNTERPARTY)
    # Verify sorted order.
    ids = [p["counterparty_id"] for p in table]
    assert ids == sorted(_SOLVENCY_BY_COUNTERPARTY.keys())


def test_solvency_table_public_all_entries_valid():
    """
    Lines 393–396: Every entry in the public table is valid and non-None.
    """
    table = solvency_table_public()
    for p in table:
        assert p is not None
        assert "counterparty_id" in p
        assert "risk_band" in p
        assert "solvency_score" in p


# ===========================================================================
# Lines 435–459: validate_consent_token() — edge cases and boundaries
# ===========================================================================

def test_validate_consent_token_rejects_empty_token():
    """Lines 435–437: Empty or whitespace token returns False."""
    assert validate_consent_token(
        "", counterparty_id=BLOCKED_CP, observed_band=RiskBand.BLOCKED
    ) is False
    assert validate_consent_token(
        "   ", counterparty_id=BLOCKED_CP, observed_band=RiskBand.BLOCKED
    ) is False


def test_validate_consent_token_rejects_committable_band():
    """
    Lines 440–441: If observed_band is committable (CLEAR/ELEVATED),
    consent token is meaningless → return False (no override needed).
    """
    assert validate_consent_token(
        consent_for(CLEAR_CP, RiskBand.CLEAR),
        counterparty_id=CLEAR_CP,
        observed_band=RiskBand.CLEAR  # committable band
    ) is False
    assert validate_consent_token(
        consent_for(ELEVATED_CP, RiskBand.ELEVATED),
        counterparty_id=ELEVATED_CP,
        observed_band=RiskBand.ELEVATED  # committable band
    ) is False


def test_validate_consent_token_rejects_if_null_counterparty():
    """
    Lines 442–444: If canonical_counterparty_id(counterparty_id) returns None,
    validation fails (no counterparty to bind to).
    """
    assert validate_consent_token(
        consent_for(BLOCKED_CP, RiskBand.BLOCKED),
        counterparty_id=None,
        observed_band=RiskBand.BLOCKED
    ) is False


def test_validate_consent_token_rejects_wrong_field_count():
    """
    Lines 445–447: Token must split into exactly 4 fields
    (scheme, counterparty_id, band, nonce).
    """
    # 3 fields:
    assert validate_consent_token(
        "consent:carrier-a:blocked",
        counterparty_id=BLOCKED_CP,
        observed_band=RiskBand.BLOCKED
    ) is False
    # 5 fields (colon in a field):
    assert validate_consent_token(
        "consent:carrier:a:blocked:n1:extra",
        counterparty_id="carrier:a",
        observed_band=RiskBand.BLOCKED
    ) is False


def test_validate_consent_token_accepts_case_insensitive_scheme():
    """
    Lines 449–450: Scheme check is case-insensitive (scheme.strip().lower()).
    """
    # UPPERCASE scheme should still pass:
    assert validate_consent_token(
        f"CONSENT:{BLOCKED_CP}:{RiskBand.BLOCKED}:n1",
        counterparty_id=BLOCKED_CP,
        observed_band=RiskBand.BLOCKED
    ) is True


def test_validate_consent_token_rejects_wrong_scheme_value():
    """
    Lines 449–450: Scheme must be "consent" (case-insensitive), not other words.
    """
    assert validate_consent_token(
        f"grant:{BLOCKED_CP}:{RiskBand.BLOCKED}:n1",
        counterparty_id=BLOCKED_CP,
        observed_band=RiskBand.BLOCKED
    ) is False


def test_validate_consent_token_rejects_unbound_counterparty():
    """
    Lines 452–453: Token's counterparty_id must match the observed one (BINDING).
    """
    # Token for BLOCKED_CP, but observing ELEVATED_CP:
    assert validate_consent_token(
        consent_for(BLOCKED_CP, RiskBand.BLOCKED),
        counterparty_id=ELEVATED_CP,
        observed_band=RiskBand.BLOCKED
    ) is False


def test_validate_consent_token_rejects_band_mismatch():
    """
    Lines 455–456: Token's claimed band must match observed (CONSISTENCY).
    """
    # Token claims "elevated" but observed is "blocked":
    assert validate_consent_token(
        consent_for(BLOCKED_CP, RiskBand.ELEVATED),
        counterparty_id=BLOCKED_CP,
        observed_band=RiskBand.BLOCKED
    ) is False


def test_validate_consent_token_rejects_empty_nonce():
    """
    Lines 458–459: Nonce field must be non-empty (a grant marker).
    """
    assert validate_consent_token(
        f"consent:{BLOCKED_CP}:{RiskBand.BLOCKED}:",  # empty nonce
        counterparty_id=BLOCKED_CP,
        observed_band=RiskBand.BLOCKED
    ) is False
    assert validate_consent_token(
        f"consent:{BLOCKED_CP}:{RiskBand.BLOCKED}:   ",  # whitespace nonce
        counterparty_id=BLOCKED_CP,
        observed_band=RiskBand.BLOCKED
    ) is False


def test_validate_consent_token_accepts_valid_token():
    """
    Lines 460: A token passing all checks returns True.
    """
    assert validate_consent_token(
        consent_for(BLOCKED_CP, RiskBand.BLOCKED),
        counterparty_id=BLOCKED_CP,
        observed_band=RiskBand.BLOCKED
    ) is True


def test_validate_consent_token_case_insensitive_band():
    """
    Lines 455: Band check is case-insensitive (tok_band.strip().lower()).
    """
    # Uppercase band in token, lowercase observed:
    assert validate_consent_token(
        f"consent:{BLOCKED_CP}:{RiskBand.BLOCKED.upper()}:n1",
        counterparty_id=BLOCKED_CP,
        observed_band=RiskBand.BLOCKED
    ) is True  # case-insensitive comparison


# ===========================================================================
# Lines 619–632: validate_rationale() — approval/rejection paths
# ===========================================================================

def test_validate_rationale_rejects_empty_text():
    """Lines 619–620: Empty or non-string text returns False."""
    result = {"rollup": {"blocked_ids": [], "all_committable": True}}
    assert validate_rationale("", result) is False
    assert validate_rationale(None, result) is False
    assert validate_rationale(123, result) is False


def test_validate_rationale_accepts_text_when_all_committable():
    """
    Lines 623–632: When all counterparties are committable (no block),
    any non-empty text is accepted (no softening check needed).
    """
    result = {"rollup": {"blocked_ids": [], "all_committable": True}}
    assert validate_rationale("This looks good.", result) is True


def test_validate_rationale_rejects_softening_language_when_blocked():
    """
    Lines 623–631: When ANY counterparty is blocked, the text must NOT
    contain softening phrases like "safe to book", "all clear", "no risk", etc.
    """
    result = {"rollup": {"blocked_ids": [BLOCKED_CP], "all_committable": False}}

    # Each softening phrase should be rejected:
    soften_phrases = [
        "safe to book",
        "all clear",
        "no risk",
        "no concerns",
        "good to go",
        "perfectly fine",
        "nothing to worry",
        "risk-free",
        "go ahead and book",
        "solvent and safe",
    ]
    for phrase in soften_phrases:
        text = f"This carrier has {phrase}. Book it."
        assert validate_rationale(text, result) is False, (
            f"Softening phrase '{phrase}' should be rejected when blocked"
        )


def test_validate_rationale_accepts_honest_language_when_blocked():
    """
    Lines 623–632: When blocked, honest language that doesn't soften
    (mentions the block) is accepted.
    """
    result = {"rollup": {"blocked_ids": [BLOCKED_CP], "all_committable": False}}
    assert validate_rationale(
        f"The counterparty {BLOCKED_CP} is on a distress watchlist; do not book without consent.",
        result
    ) is True
    assert validate_rationale(
        "This counterparty requires explicit consent due to solvency concerns.",
        result
    ) is True


def test_validate_rationale_case_insensitive_softening_check():
    """
    Lines 624–631: Softening check is case-insensitive (text.lower()).
    """
    result = {"rollup": {"blocked_ids": [BLOCKED_CP], "all_committable": False}}
    assert validate_rationale("This is SAFE TO BOOK.", result) is False
    assert validate_rationale("It's ALL CLEAR to proceed.", result) is False


# ===========================================================================
# Lines 641–670: _llm_rationale() — LLM call + fallback paths
# ===========================================================================

def test_llm_rationale_returns_none_if_no_api_key():
    """
    Lines 641–643: If DASHSCOPE_API_KEY env var is missing,
    return None (deterministic fallback).
    """
    old = os.environ.pop("DASHSCOPE_API_KEY", None)
    try:
        result = {"rollup": {"all_committable": True, "blocked_ids": []}}
        rationale = _llm_rationale(result)
        assert rationale is None
    finally:
        if old:
            os.environ["DASHSCOPE_API_KEY"] = old


def test_llm_rationale_returns_none_on_http_error():
    """
    Lines 644–670: On any HTTP error or exception, return None (fail-safe).
    """
    os.environ["DASHSCOPE_API_KEY"] = "fake-key"
    try:
        result = {"rollup": {"all_committable": False, "blocked_ids": [BLOCKED_CP]}}
        with mock.patch("httpx.post") as mock_post:
            # Simulate HTTP error:
            mock_post.side_effect = Exception("Network error")
            rationale = _llm_rationale(result)
            assert rationale is None
    finally:
        os.environ.pop("DASHSCOPE_API_KEY", None)


def test_llm_rationale_returns_none_if_validation_fails():
    """
    Lines 668–670: If the LLM response fails validate_rationale,
    return None (deterministic fallback).
    """
    os.environ["DASHSCOPE_API_KEY"] = "fake-key"
    try:
        result = {"rollup": {"all_committable": False, "blocked_ids": [BLOCKED_CP]}}
        fake_response = mock.MagicMock()
        fake_response.json.return_value = {
            "choices": [{"message": {"content": "This is SAFE TO BOOK!"}}]  # softening!
        }
        with mock.patch("httpx.post", return_value=fake_response):
            rationale = _llm_rationale(result)
            assert rationale is None  # rejected by validate_rationale
    finally:
        os.environ.pop("DASHSCOPE_API_KEY", None)


def test_llm_rationale_returns_text_if_validation_passes():
    """
    Lines 668–670: If the LLM response passes validate_rationale,
    return the text.
    """
    os.environ["DASHSCOPE_API_KEY"] = "fake-key"
    try:
        result = {"rollup": {"all_committable": False, "blocked_ids": [BLOCKED_CP]}}
        honest_text = f"The counterparty {BLOCKED_CP} requires explicit consent."
        fake_response = mock.MagicMock()
        fake_response.json.return_value = {
            "choices": [{"message": {"content": honest_text}}]
        }
        with mock.patch("httpx.post", return_value=fake_response):
            rationale = _llm_rationale(result)
            assert rationale == honest_text
    finally:
        os.environ.pop("DASHSCOPE_API_KEY", None)


# ===========================================================================
# Lines 673–691: explain() — human-readable summary (deterministic)
# ===========================================================================

def test_explain_all_committable_headline():
    """
    Lines 680–684: When all_committable=True,
    headline says all counterparties are committable.
    """
    result = vet([{"counterparty_id": CLEAR_CP}])
    ex = explain(result)
    assert "committable" in ex["headline"].lower()
    assert ex["blocked_ids"] == []


def test_explain_blocked_headline_and_ids():
    """
    Lines 686–690: When blocked counterparties exist,
    headline reports the count and lists blocked_ids.
    """
    result = vet([
        {"counterparty_id": BLOCKED_CP},
        {"counterparty_id": UNKNOWN_CP},
    ])
    ex = explain(result)
    assert "NOT committable" in ex["headline"]
    assert len(ex["blocked_ids"]) == 2
    assert BLOCKED_CP in ex["blocked_ids"]


# ===========================================================================
# Lines 800–842: A2A handlers (_vet_handler, _explain_handler)
# ===========================================================================

def test_vet_handler_rejects_non_dict_payload():
    """
    Lines 802–806: fraud.vet requires data to be a JSON dict.
    """
    agent = FraudAgent()
    client = TestClient(agent.build_app())
    
    # Send a list instead of a dict:
    body = {
        "jsonrpc": "2.0", "id": str(uuid.uuid4()), "method": "message/send",
        "params": {"message": {
            "messageId": str(uuid.uuid4()), "role": "user",
            "parts": [{"kind": "data", "data": [1, 2, 3]}],  # list, not dict
            "metadata": {"skillId": "fraud.vet"},
        }},
    }
    resp = client.post("/", json=body)
    assert resp.status_code == 200
    result = resp.json()
    # The task should fail with the expected error message.
    assert "error" in result or "violation" in str(result).lower() or result.get("result", {}).get("status", {}).get("state") != "completed"


def test_vet_handler_rejects_missing_counterparties_list():
    """
    Lines 807–809: fraud.vet requires 'counterparties' as a non-empty list.
    """
    agent = FraudAgent()
    client = TestClient(agent.build_app())
    
    # Missing counterparties:
    body = {
        "jsonrpc": "2.0", "id": str(uuid.uuid4()), "method": "message/send",
        "params": {"message": {
            "messageId": str(uuid.uuid4()), "role": "user",
            "parts": [{"kind": "data", "data": {}}],  # empty dict
            "metadata": {"skillId": "fraud.vet"},
        }},
    }
    resp = client.post("/", json=body)
    assert resp.status_code == 200
    result = resp.json()
    assert result.get("result", {}).get("status", {}).get("state") != "completed" or "error" in str(result)


def test_vet_handler_rejects_empty_counterparties_list():
    """
    Lines 807–809: counterparties must be a non-empty list.
    """
    agent = FraudAgent()
    client = TestClient(agent.build_app())
    
    body = {
        "jsonrpc": "2.0", "id": str(uuid.uuid4()), "method": "message/send",
        "params": {"message": {
            "messageId": str(uuid.uuid4()), "role": "user",
            "parts": [{"kind": "data", "data": {"counterparties": []}}],
            "metadata": {"skillId": "fraud.vet"},
        }},
    }
    resp = client.post("/", json=body)
    assert resp.status_code == 200
    result = resp.json()
    assert result.get("result", {}).get("status", {}).get("state") != "completed" or "error" in str(result)


def test_vet_handler_success_path():
    """
    Lines 811–832: fraud.vet with valid counterparties returns a result artifact.
    """
    agent = FraudAgent()
    client = TestClient(agent.build_app())
    
    body = {
        "jsonrpc": "2.0", "id": str(uuid.uuid4()), "method": "message/send",
        "params": {"message": {
            "messageId": str(uuid.uuid4()), "role": "user",
            "parts": [{"kind": "data", "data": {
                "counterparties": [{"counterparty_id": BLOCKED_CP}]
            }}],
            "metadata": {"skillId": "fraud.vet"},
        }},
    }
    resp = client.post("/", json=body)
    assert resp.status_code == 200
    result = resp.json()
    assert result["result"]["status"]["state"] == "completed"
    data = result["result"]["artifacts"][0]["parts"][0]["data"]
    assert "rollup" in data
    assert BLOCKED_CP in data["rollup"]["blocked_ids"]
    assert data["rationale_source"] == "deterministic"


def test_vet_handler_use_llm_flag():
    """
    Lines 817–822: When use_llm=True, set rationale_source accordingly.
    """
    agent = FraudAgent()
    client = TestClient(agent.build_app())
    
    body = {
        "jsonrpc": "2.0", "id": str(uuid.uuid4()), "method": "message/send",
        "params": {"message": {
            "messageId": str(uuid.uuid4()), "role": "user",
            "parts": [{"kind": "data", "data": {
                "counterparties": [{"counterparty_id": CLEAR_CP}],
                "use_llm": False  # explicitly disable LLM
            }}],
            "metadata": {"skillId": "fraud.vet"},
        }},
    }
    resp = client.post("/", json=body)
    assert resp.status_code == 200
    result = resp.json()
    data = result["result"]["artifacts"][0]["parts"][0]["data"]
    assert data["rationale_source"] == "deterministic"


def test_explain_handler_requires_result():
    """
    Lines 836–837: fraud.explain requires a 'result' field in the payload.
    """
    agent = FraudAgent()
    client = TestClient(agent.build_app())
    
    body = {
        "jsonrpc": "2.0", "id": str(uuid.uuid4()), "method": "message/send",
        "params": {"message": {
            "messageId": str(uuid.uuid4()), "role": "user",
            "parts": [{"kind": "data", "data": {}}],  # missing 'result'
            "metadata": {"skillId": "fraud.explain"},
        }},
    }
    resp = client.post("/", json=body)
    assert resp.status_code == 200
    result = resp.json()
    assert result.get("result", {}).get("status", {}).get("state") != "completed" or "error" in str(result)


def test_explain_handler_success_path():
    """
    Lines 838–842: fraud.explain with a valid result returns an explanation artifact.
    """
    agent = FraudAgent()
    client = TestClient(agent.build_app())
    
    # First get a vet result:
    body1 = {
        "jsonrpc": "2.0", "id": str(uuid.uuid4()), "method": "message/send",
        "params": {"message": {
            "messageId": str(uuid.uuid4()), "role": "user",
            "parts": [{"kind": "data", "data": {
                "counterparties": [{"counterparty_id": BLOCKED_CP}]
            }}],
            "metadata": {"skillId": "fraud.vet"},
        }},
    }
    resp1 = client.post("/", json=body1)
    vet_result = resp1.json()["result"]["artifacts"][0]["parts"][0]["data"]
    
    # Now explain that result:
    body2 = {
        "jsonrpc": "2.0", "id": str(uuid.uuid4()), "method": "message/send",
        "params": {"message": {
            "messageId": str(uuid.uuid4()), "role": "user",
            "parts": [{"kind": "data", "data": {"result": vet_result}}],
            "metadata": {"skillId": "fraud.explain"},
        }},
    }
    resp2 = client.post("/", json=body2)
    assert resp2.status_code == 200
    result2 = resp2.json()
    assert result2["result"]["status"]["state"] == "completed"
    data = result2["result"]["artifacts"][0]["parts"][0]["data"]
    assert "headline" in data
    assert BLOCKED_CP in data["blocked_ids"]


# ===========================================================================
# Lines 845–865: _extract_payload() — various part kinds
# ===========================================================================

def test_extract_payload_from_data_dict_part():
    """
    Lines 848–851: Extract JSON from a 'data' part containing a dict.
    """
    agent = FraudAgent()
    message = {
        "parts": [
            {"kind": "data", "data": {"key": "value"}}
        ]
    }
    payload = agent._extract_payload(message)
    assert payload == {"key": "value"}


def test_extract_payload_from_data_list_part():
    """
    Lines 848–851: Extract JSON from a 'data' part containing a list.
    """
    agent = FraudAgent()
    message = {
        "parts": [
            {"kind": "data", "data": [1, 2, 3]}
        ]
    }
    payload = agent._extract_payload(message)
    assert payload == [1, 2, 3]


def test_extract_payload_from_data_json_string():
    """
    Lines 852–856: Extract JSON from a 'data' part containing a JSON string.
    """
    agent = FraudAgent()
    message = {
        "parts": [
            {"kind": "data", "data": '{"key": "value"}'}
        ]
    }
    payload = agent._extract_payload(message)
    assert payload == {"key": "value"}


def test_extract_payload_from_text_json_string():
    """
    Lines 857–864: Extract JSON from a 'text' part containing valid JSON.
    """
    agent = FraudAgent()
    message = {
        "parts": [
            {"kind": "text", "text": '{"key": "value"}'}
        ]
    }
    payload = agent._extract_payload(message)
    assert payload == {"key": "value"}


def test_extract_payload_prefers_data_over_text():
    """
    Lines 847–864: If both data and text parts exist, data is tried first.
    """
    agent = FraudAgent()
    message = {
        "parts": [
            {"kind": "data", "data": {"from": "data"}},
            {"kind": "text", "text": '{"from": "text"}'}
        ]
    }
    payload = agent._extract_payload(message)
    assert payload == {"from": "data"}


def test_extract_payload_returns_none_on_invalid_json():
    """
    Lines 855–856, 863–864: Invalid JSON strings return None.
    """
    agent = FraudAgent()
    message = {
        "parts": [
            {"kind": "text", "text": "not valid json at all"}
        ]
    }
    payload = agent._extract_payload(message)
    assert payload is None


def test_extract_payload_returns_none_if_no_parts():
    """
    Lines 865: If no valid parts are found, return None.
    """
    agent = FraudAgent()
    message = {"parts": []}
    payload = agent._extract_payload(message)
    assert payload is None


# ===========================================================================
# Integration: canonical_counterparty_id() edge cases
# ===========================================================================

def test_canonical_counterparty_id_normalizes_whitespace_and_case():
    """
    Lines 318–320: Normalizes to lowercase and strips whitespace.
    """
    assert canonical_counterparty_id("  CARRIER-SINGAPORE-AIRLINES  ") == CLEAR_CP
    assert canonical_counterparty_id(None) is None
    assert canonical_counterparty_id("") is None


# ===========================================================================
# Integration: fetch_solvency_profile() with simulate_source_unreachable
# ===========================================================================

def test_fetch_solvency_profile_simulate_source_unreachable():
    """
    Lines 326–345: When simulate_source_unreachable=True,
    freshness is "SEEDED_FALLBACK" (Tier-3 exhausted).
    """
    profile, freshness = fetch_solvency_profile(
        CLEAR_CP, simulate_source_unreachable=False
    )
    assert freshness == "SEEDED"
    
    profile2, freshness2 = fetch_solvency_profile(
        CLEAR_CP, simulate_source_unreachable=True
    )
    assert freshness2 == "SEEDED_FALLBACK"


# ===========================================================================
# Integration: vet_counterparty() with simulate_source_unreachable
# ===========================================================================

def test_vet_counterparty_simulate_source_unreachable_affects_tier():
    """
    Lines 530: When simulate_source_unreachable=True, provenance tier is EXHAUSTED.
    """
    from core.contracts import SourceTier
    
    v1 = vet_counterparty(CLEAR_CP, simulate_source_unreachable=False)
    assert v1["provenance"]["tier"] == SourceTier.SEEDED.value
    
    v2 = vet_counterparty(CLEAR_CP, simulate_source_unreachable=True)
    assert v2["provenance"]["tier"] == SourceTier.EXHAUSTED.value
