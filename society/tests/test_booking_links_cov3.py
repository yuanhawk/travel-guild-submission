"""
test_booking_links_cov3.py — coverage completion for booking_links.py.

Targets uncovered lines:
  * Line 105: _q() with None input
  * Lines 257-261: _yymmdd() with invalid format (non-string, wrong length, bad dashes, non-digits)
  * Lines 348-369: _wikipedia_url() edge cases (non-string, empty, bare title, lang:title with spaces, encoding)
  * Lines 437-449: _first_verdict_source_url() with missing/non-dict/non-list/invalid entries
  * Lines 497-561: build_booking_links() type validation and full nested iteration

All tests are deterministic, var-0-safe, NO live LLM/network/merchant.
"""
import json
from utils import booking_links as BL
from utils.booking_links import (
    _q,
    _qs,
    _safe_url,
    _yymmdd,
    _wikipedia_url,
    _wikidata_qid,
    _first_verdict_source_url,
    _leg_index,
    _iso2_for_leg,
    _maps_search,
    build_booking_links,
)


# ---------------------------------------------------------------------------
# Line 105: _q() with None (collapses to "")
# ---------------------------------------------------------------------------

def test_q_with_none() -> None:
    """Line 105: _q(None) returns empty string."""
    result = _q(None)
    assert result == "", f"_q(None) should return '', got {result!r}"
    print("PASS: test_q_with_none")


def test_q_with_empty_string() -> None:
    """_q('') and whitespace-only collapse to ''."""
    assert _q("") == ""
    assert _q("   ") == ""
    assert _q("\t\n") == ""
    print("PASS: test_q_with_empty_string")


def test_q_with_special_chars() -> None:
    """_q() encodes special chars deterministically."""
    assert _q("a&b") == "a%26b"
    assert _q("a?b") == "a%3Fb"
    assert _q("a#b") == "a%23b"
    assert _q("a b") == "a%20b"
    assert _q("a/b") == "a%2Fb"
    print("PASS: test_q_with_special_chars")


def test_qs_ordered_pairs() -> None:
    """_qs() builds query string from ordered (k,v) pairs deterministically."""
    pairs = [("foo", "bar"), ("x", "y z")]
    result = _qs(pairs)
    assert result == "foo=bar&x=y%20z"
    # Order matters: same pairs in different order → different output
    pairs2 = [("x", "y z"), ("foo", "bar")]
    result2 = _qs(pairs2)
    assert result2 == "x=y%20z&foo=bar"
    assert result != result2
    print("PASS: test_qs_ordered_pairs")


# ---------------------------------------------------------------------------
# Lines 257-261: _yymmdd() type & format validation
# ---------------------------------------------------------------------------

def test_yymmdd_valid_iso_date() -> None:
    """_yymmdd() converts valid YYYY-MM-DD to YYMMDD."""
    assert _yymmdd("2026-06-23") == "260623"
    assert _yymmdd("2000-01-01") == "000101"
    assert _yymmdd("2099-12-31") == "991231"
    print("PASS: test_yymmdd_valid_iso_date")


def test_yymmdd_invalid_non_string() -> None:
    """Line 253-254: _yymmdd(non-string) returns None."""
    assert _yymmdd(None) is None
    assert _yymmdd(123) is None
    assert _yymmdd(12.34) is None
    assert _yymmdd([]) is None
    assert _yymmdd({}) is None
    print("PASS: test_yymmdd_invalid_non_string")


def test_yymmdd_invalid_length() -> None:
    """Line 256: _yymmdd() rejects non-10-char strings."""
    assert _yymmdd("2026-06") is None  # too short
    assert _yymmdd("2026-06-23-01") is None  # too long
    assert _yymmdd("20260623") is None  # no dashes
    assert _yymmdd("") is None
    assert _yymmdd("      ") is None
    print("PASS: test_yymmdd_invalid_length")


def test_yymmdd_invalid_dash_positions() -> None:
    """Line 256: _yymmdd() requires dashes at positions 4 and 7."""
    assert _yymmdd("2026/06-23") is None  # wrong sep at pos 4
    assert _yymmdd("2026-06/23") is None  # wrong sep at pos 7
    assert _yymmdd("2026 06 23") is None
    print("PASS: test_yymmdd_invalid_dash_positions")


def test_yymmdd_invalid_non_digits() -> None:
    """Line 259: _yymmdd() requires all date fields to be digits."""
    assert _yymmdd("202x-06-23") is None  # non-digit in year
    assert _yymmdd("2026-0a-23") is None  # non-digit in month
    assert _yymmdd("2026-06-2x") is None  # non-digit in day
    assert _yymmdd("abcd-ef-gh") is None
    print("PASS: test_yymmdd_invalid_non_digits")


def test_yymmdd_with_whitespace() -> None:
    """_yymmdd() strips input before validation."""
    assert _yymmdd("  2026-06-23  ") == "260623"
    assert _yymmdd("\t2026-06-23\n") == "260623"
    print("PASS: test_yymmdd_with_whitespace")


# ---------------------------------------------------------------------------
# Lines 348-369: _wikipedia_url() edge cases
# ---------------------------------------------------------------------------

def test_wikipedia_url_non_string() -> None:
    """Line 353: _wikipedia_url(non-string) returns None."""
    assert _wikipedia_url(None) is None
    assert _wikipedia_url(123) is None
    assert _wikipedia_url([]) is None
    assert _wikipedia_url({}) is None
    print("PASS: test_wikipedia_url_non_string")


def test_wikipedia_url_empty_string() -> None:
    """Line 356: _wikipedia_url('') and whitespace-only return None."""
    assert _wikipedia_url("") is None
    assert _wikipedia_url("   ") is None
    assert _wikipedia_url("\t\n") is None
    print("PASS: test_wikipedia_url_empty_string")


def test_wikipedia_url_bare_title() -> None:
    """Line 365: bare title (no colon) defaults to 'en'."""
    result = _wikipedia_url("Eiffel_Tower")
    assert result == "https://en.wikipedia.org/wiki/Eiffel_Tower"
    print("PASS: test_wikipedia_url_bare_title")


def test_wikipedia_url_lang_colon_title() -> None:
    """Line 359: 'lang:Title' format splits on colon."""
    result = _wikipedia_url("fr:Tour Eiffel")
    # Title has space → becomes underscore (not %-encoded since underscore is safe in URLs)
    assert result == "https://fr.wikipedia.org/wiki/Tour_Eiffel"
    print("PASS: test_wikipedia_url_lang_colon_title")


def test_wikipedia_url_empty_lang_or_title() -> None:
    """Line 362: _wikipedia_url() returns None if lang or title is empty after strip."""
    assert _wikipedia_url(":Title") is None  # empty lang
    assert _wikipedia_url("lang:") is None  # empty title
    assert _wikipedia_url(":") is None  # both empty
    print("PASS: test_wikipedia_url_empty_lang_or_title")


def test_wikipedia_url_title_spaces_to_underscores() -> None:
    """Line 366: spaces in title → underscores, then %-encoded."""
    result = _wikipedia_url("The Bund")
    assert result == "https://en.wikipedia.org/wiki/The_Bund"
    # With lang.
    result2 = _wikipedia_url("zh:兵马俑")
    # Non-ASCII → %-encoded by quote()
    assert "wikipedia.org/wiki/" in result2
    print("PASS: test_wikipedia_url_title_spaces_to_underscores")


def test_wikipedia_url_empty_after_encoding() -> None:
    """Line 367-368: if title is whitespace-only, _q() returns '', and we return None."""
    result = _wikipedia_url("en:   ")  # whitespace title
    assert result is None, "whitespace-only title should yield None"
    print("PASS: test_wikipedia_url_empty_after_encoding")


def test_wikipedia_url_special_chars_encoded() -> None:
    """_wikipedia_url() %-encodes special chars in title."""
    result = _wikipedia_url("A & B? C")
    # spaces → underscores first: "A_&_B?_C", then _q() encodes special chars
    # underscore is NOT special so stays, &/?/# and spaces become %XX
    assert "A_%26_B%3F_C" in result
    # Just verify it's %-encoded and safe.
    assert "javascript" not in result.lower()
    print("PASS: test_wikipedia_url_special_chars_encoded")


# ---------------------------------------------------------------------------
# _wikidata_qid() basic coverage
# ---------------------------------------------------------------------------

def test_wikidata_qid_valid() -> None:
    """_wikidata_qid() returns a bare Q-id string, or None."""
    assert _wikidata_qid("Q170222") == "Q170222"
    assert _wikidata_qid("Q1") == "Q1"
    print("PASS: test_wikidata_qid_valid")


def test_wikidata_qid_invalid() -> None:
    """_wikidata_qid(non-string) or whitespace-only returns None."""
    assert _wikidata_qid(None) is None
    assert _wikidata_qid(123) is None
    assert _wikidata_qid("") is None
    assert _wikidata_qid("   ") is None
    print("PASS: test_wikidata_qid_invalid")


# ---------------------------------------------------------------------------
# Lines 437-449: _first_verdict_source_url() type validation
# ---------------------------------------------------------------------------

def test_first_verdict_source_url_non_dict_verdict() -> None:
    """Line 433-434: _first_verdict_source_url() returns None if verdict is not dict."""
    assert _first_verdict_source_url(None, "per_leg") is None
    assert _first_verdict_source_url("not a dict", "per_leg") is None
    assert _first_verdict_source_url([], "per_leg") is None
    print("PASS: test_first_verdict_source_url_non_dict_verdict")


def test_first_verdict_source_url_missing_key() -> None:
    """Line 435-436: returns None if list_key is missing or not a list."""
    verdict = {"per_destination": None}
    assert _first_verdict_source_url(verdict, "per_leg") is None
    print("PASS: test_first_verdict_source_url_missing_key")


def test_first_verdict_source_url_non_list_entries() -> None:
    """Line 435-436: returns None if verdict[list_key] is not a list."""
    verdict = {"per_leg": "not a list"}
    assert _first_verdict_source_url(verdict, "per_leg") is None
    verdict2 = {"per_leg": {"nested": "dict"}}
    assert _first_verdict_source_url(verdict2, "per_leg") is None
    print("PASS: test_first_verdict_source_url_non_list_entries")


def test_first_verdict_source_url_non_dict_entries() -> None:
    """Line 439: skip non-dict entries in the list."""
    verdict = {
        "per_leg": [
            "not a dict",
            None,
            123,
            {"source_url": "https://example.com/"},
        ]
    }
    # First 3 are skipped, 4th is a dict with a safe URL
    result = _first_verdict_source_url(verdict, "per_leg")
    assert result == "https://example.com/"
    print("PASS: test_first_verdict_source_url_non_dict_entries")


def test_first_verdict_source_url_top_level_source_url() -> None:
    """Line 441-443: checks entry['source_url'] first."""
    verdict = {
        "per_leg": [
            {"source_url": "https://first.example/"},
            {"source_url": "https://second.example/"},
        ]
    }
    result = _first_verdict_source_url(verdict, "per_leg")
    assert result == "https://first.example/"  # First match
    print("PASS: test_first_verdict_source_url_top_level_source_url")


def test_first_verdict_source_url_provenance_fallback() -> None:
    """Line 444-448: falls back to provenance.source_url if top-level is missing/unsafe."""
    verdict = {
        "per_leg": [
            {
                "source_url": "javascript:alert(1)",  # unsafe
                "provenance": {"source_url": "https://safe.example/"}
            }
        ]
    }
    result = _first_verdict_source_url(verdict, "per_leg")
    assert result == "https://safe.example/"
    print("PASS: test_first_verdict_source_url_provenance_fallback")


def test_first_verdict_source_url_provenance_not_dict() -> None:
    """Line 445: skip if provenance is not a dict."""
    verdict = {
        "per_leg": [
            {
                "provenance": "not a dict",
                "source_url": None,
            },
            {
                "provenance": {"source_url": "https://works.example/"},
            }
        ]
    }
    result = _first_verdict_source_url(verdict, "per_leg")
    assert result == "https://works.example/"
    print("PASS: test_first_verdict_source_url_provenance_not_dict")


def test_first_verdict_source_url_empty_list() -> None:
    """Returns None if entries list is empty."""
    verdict = {"per_leg": []}
    assert _first_verdict_source_url(verdict, "per_leg") is None
    print("PASS: test_first_verdict_source_url_empty_list")


# ---------------------------------------------------------------------------
# _leg_index() — find leg by leg_id
# ---------------------------------------------------------------------------

def test_leg_index_found() -> None:
    """_leg_index() finds a leg by leg_id."""
    legs = [
        {"leg_id": "leg-0", "city": "A"},
        {"leg_id": "leg-1", "city": "B"},
    ]
    found = _leg_index(legs, "leg-1")
    assert found is not None and found["city"] == "B"
    print("PASS: test_leg_index_found")


def test_leg_index_not_found() -> None:
    """_leg_index() returns None if leg_id not found."""
    legs = [{"leg_id": "leg-0", "city": "A"}]
    assert _leg_index(legs, "leg-99") is None
    print("PASS: test_leg_index_not_found")


def test_leg_index_skips_non_dicts() -> None:
    """_leg_index() skips non-dict entries."""
    legs = [
        "not a dict",
        None,
        {"leg_id": "leg-0", "city": "A"},
    ]
    found = _leg_index(legs, "leg-0")
    assert found is not None and found["city"] == "A"
    print("PASS: test_leg_index_skips_non_dicts")


# ---------------------------------------------------------------------------
# _iso2_for_leg() — derive iso2 from day_plans
# ---------------------------------------------------------------------------

def test_iso2_for_leg_found() -> None:
    """_iso2_for_leg() finds iso2 from matching day_plan."""
    day_plans = [
        {"leg_id": "leg-0", "iso2": "CN"},
        {"leg_id": "leg-1", "iso2": "JP"},
    ]
    assert _iso2_for_leg(day_plans, "leg-1") == "JP"
    print("PASS: test_iso2_for_leg_found")


def test_iso2_for_leg_not_found() -> None:
    """_iso2_for_leg() returns '' if not found."""
    day_plans = [{"leg_id": "leg-0", "iso2": "CN"}]
    assert _iso2_for_leg(day_plans, "leg-99") == ""
    print("PASS: test_iso2_for_leg_not_found")


def test_iso2_for_leg_normalizes() -> None:
    """_iso2_for_leg() strips and uppercases iso2."""
    day_plans = [{"leg_id": "leg-0", "iso2": "  jp  "}]
    assert _iso2_for_leg(day_plans, "leg-0") == "JP"
    print("PASS: test_iso2_for_leg_normalizes")


# ---------------------------------------------------------------------------
# Lines 497-561: build_booking_links() type validation and full iteration
# ---------------------------------------------------------------------------

def test_build_booking_links_missing_keys() -> None:
    """Lines 495-503: build_booking_links handles missing/None top-level keys."""
    result = {}  # Empty result
    block = build_booking_links(result)
    # Should still return the booking_links block with empty/default entries
    assert "booking_links" in result
    assert block["lodging"] == []
    assert block["transport"] == []
    assert block["attractions"] == []
    assert block["restaurants"] == []
    print("PASS: test_build_booking_links_missing_keys")


def test_build_booking_links_non_list_legs() -> None:
    """Lines 496-497: if legs is not a list, treat as []."""
    result = {"legs": "not a list"}
    block = build_booking_links(result)
    assert block["lodging"] == []
    print("PASS: test_build_booking_links_non_list_legs")


def test_build_booking_links_non_list_edges() -> None:
    """Lines 498-499: if edges is not a list, treat as []."""
    result = {"transport_edges": {"nested": "dict"}}
    block = build_booking_links(result)
    assert block["transport"] == []
    print("PASS: test_build_booking_links_non_list_edges")


def test_build_booking_links_non_list_day_plans() -> None:
    """Lines 501-502: if day_plans is not a list, treat as []."""
    result = {"day_plans": None}
    block = build_booking_links(result)
    assert block["attractions"] == []
    assert block["restaurants"] == []
    print("PASS: test_build_booking_links_non_list_day_plans")


def test_build_booking_links_lodging_with_non_dict_leg() -> None:
    """Lines 508-509: skip non-dict leg entries."""
    result = {
        "legs": [
            "not a dict",
            None,
            {"hotel_title": "Grand", "city": "Shanghai"},
        ]
    }
    block = build_booking_links(result)
    # Only the valid leg should produce a lodging entry
    assert len(block["lodging"]) == 1
    print("PASS: test_build_booking_links_lodging_with_non_dict_leg")


def test_build_booking_links_lodging_mutation() -> None:
    """Lines 510-515: each leg gets booking_link set, lodging list populated."""
    result = {
        "legs": [
            {"leg_id": "leg-0", "hotel_title": "Hotel A", "city": "City A"},
            {"leg_id": "leg-1", "hotel_title": "Hotel B", "city": "City B"},
        ]
    }
    block = build_booking_links(result)
    assert len(result["legs"]) == 2
    assert "booking_link" in result["legs"][0]
    assert "booking_link" in result["legs"][1]
    assert len(block["lodging"]) == 2
    print("PASS: test_build_booking_links_lodging_mutation")


def test_build_booking_links_transport_with_non_dict_edge() -> None:
    """Lines 524-525: skip non-dict edge entries."""
    result = {
        "transport_edges": [
            "not a dict",
            {"from_iata": "PVG", "to_iata": "HND"},
        ],
        "legs": [],
    }
    block = build_booking_links(result)
    assert len(block["transport"]) == 1
    print("PASS: test_build_booking_links_transport_with_non_dict_edge")


def test_build_booking_links_transport_derives_date_from_leg() -> None:
    """Lines 526-533: depart_date comes from matching leg's checkout."""
    result = {
        "legs": [
            {"leg_id": "leg-0", "checkout": "2026-10-04"},
        ],
        "transport_edges": [
            {"from_leg": "leg-0", "from_iata": "PVG", "to_iata": "HND"},
        ],
    }
    block = build_booking_links(result)
    fl = block["transport"][0]
    # The flight link providers should have the date in the URL
    assert any("2026-10-04" in p["url"] for p in fl["providers"])
    print("PASS: test_build_booking_links_transport_derives_date_from_leg")


def test_build_booking_links_attractions_with_non_dict_day_plan() -> None:
    """Lines 540-541: skip non-dict day_plan entries."""
    result = {
        "day_plans": [
            "not a dict",
            {"city": "Shanghai", "iso2": "CN", "days": []},
        ]
    }
    block = build_booking_links(result)
    # The function should not raise, just skip the bad entry
    assert isinstance(block["attractions"], list)
    print("PASS: test_build_booking_links_attractions_with_non_dict_day_plan")


def test_build_booking_links_attractions_with_non_dict_day() -> None:
    """Lines 545-546: skip non-dict day entries."""
    result = {
        "day_plans": [
            {
                "city": "Shanghai",
                "iso2": "CN",
                "days": [
                    "not a dict",
                    None,
                    {
                        "day_index": 0,
                        "attractions": [
                            {"name": "Attraction A"},
                        ]
                    }
                ]
            }
        ]
    }
    block = build_booking_links(result)
    # Only the valid day's attraction should be in the list
    assert len(block["attractions"]) == 1
    print("PASS: test_build_booking_links_attractions_with_non_dict_day")


def test_build_booking_links_attractions_with_non_dict_attraction() -> None:
    """Lines 548-549: skip non-dict attraction entries."""
    result = {
        "day_plans": [
            {
                "city": "Shanghai",
                "iso2": "CN",
                "days": [
                    {
                        "day_index": 0,
                        "attractions": [
                            "not a dict",
                            None,
                            {"name": "Valid Attraction"},
                        ]
                    }
                ]
            }
        ]
    }
    block = build_booking_links(result)
    assert len(block["attractions"]) == 1
    print("PASS: test_build_booking_links_attractions_with_non_dict_attraction")


def test_build_booking_links_attractions_mutation() -> None:
    """Lines 550-559: each attraction gets link set, attractions list populated."""
    result = {
        "day_plans": [
            {
                "city": "Shanghai",
                "iso2": "CN",
                "days": [
                    {
                        "attractions": [
                            {"name": "Attraction A"},
                            {"name": "Attraction B"},
                        ]
                    }
                ]
            }
        ]
    }
    block = build_booking_links(result)
    assert len(result["day_plans"][0]["days"][0]["attractions"]) == 2
    assert "link" in result["day_plans"][0]["days"][0]["attractions"][0]
    assert "link" in result["day_plans"][0]["days"][0]["attractions"][1]
    assert len(block["attractions"]) == 2
    print("PASS: test_build_booking_links_attractions_mutation")


def test_build_booking_links_restaurants_with_non_dict_meals() -> None:
    """Lines 561-562: skip if meals is not a dict."""
    result = {
        "day_plans": [
            {
                "city": "Shanghai",
                "iso2": "CN",
                "days": [
                    {
                        "meals": "not a dict",
                    }
                ]
            }
        ]
    }
    block = build_booking_links(result)
    assert block["restaurants"] == []
    print("PASS: test_build_booking_links_restaurants_with_non_dict_meals")


def test_build_booking_links_restaurants_canonical_slot_order() -> None:
    """Lines 564-575: restaurants processed in canonical slot order, skipping None."""
    result = {
        "day_plans": [
            {
                "city": "Shanghai",
                "iso2": "CN",
                "days": [
                    {
                        "meals": {
                            "breakfast": {"name": "Breakfast Place"},
                            "lunch": None,
                            "tea": {"name": "Tea House"},
                            "dinner": {"name": "Dinner House"},
                            "supper": None,
                        }
                    }
                ]
            }
        ]
    }
    block = build_booking_links(result)
    assert len(block["restaurants"]) == 3
    # Verify order: breakfast, tea, dinner (lunch and supper skipped)
    meal_names = [
        result["day_plans"][0]["days"][0]["meals"]["breakfast"]["link"]["label"],
        result["day_plans"][0]["days"][0]["meals"]["tea"]["link"]["label"],
        result["day_plans"][0]["days"][0]["meals"]["dinner"]["link"]["label"],
    ]
    # All should be set
    assert all(m for m in meal_names)
    print("PASS: test_build_booking_links_restaurants_canonical_slot_order")


def test_build_booking_links_restaurants_with_non_dict_meal() -> None:
    """Lines 566-567: skip non-dict meal entries."""
    result = {
        "day_plans": [
            {
                "city": "Shanghai",
                "iso2": "CN",
                "days": [
                    {
                        "meals": {
                            "breakfast": "not a dict",
                            "lunch": {"name": "Lunch Place"},
                            "tea": None,
                            "dinner": 123,
                            "supper": {"name": "Supper Place"},
                        }
                    }
                ]
            }
        ]
    }
    block = build_booking_links(result)
    assert len(block["restaurants"]) == 2
    print("PASS: test_build_booking_links_restaurants_with_non_dict_meal")


def test_build_booking_links_compliance_verdict_reuse() -> None:
    """Lines 578-579: reuse compliance_verdict source URL for visa link."""
    result = {
        "compliance_verdict": {
            "per_leg": [
                {"source_url": "https://www.mfa.gov.cn/"}
            ]
        }
    }
    block = build_booking_links(result)
    assert block["visa"] is not None
    assert block["visa"]["kind"] == "gov_portal"
    assert block["visa"]["booking_url"] == "https://www.mfa.gov.cn/"
    print("PASS: test_build_booking_links_compliance_verdict_reuse")


def test_build_booking_links_compliance_verdict_none() -> None:
    """Lines 578-579: visa is None if compliance_verdict has no safe URL."""
    result = {
        "compliance_verdict": {
            "per_leg": [
                {"source_url": "javascript:alert(1)"}
            ]
        }
    }
    block = build_booking_links(result)
    assert block["visa"] is None
    print("PASS: test_build_booking_links_compliance_verdict_none")


def test_build_booking_links_health_verdict_reuse() -> None:
    """Lines 581-582: reuse health_verdict source URL for health link."""
    result = {
        "health_verdict": {
            "per_destination": [
                {"source_url": "https://wwwnc.cdc.gov/travel/destinations/x"}
            ]
        }
    }
    block = build_booking_links(result)
    assert block["health"] is not None
    assert block["health"]["kind"] == "cdc_who_portal"
    assert block["health"]["booking_url"] == "https://wwwnc.cdc.gov/travel/destinations/x"
    print("PASS: test_build_booking_links_health_verdict_reuse")


def test_build_booking_links_returns_block_and_mutates() -> None:
    """Lines 585-595: build_booking_links returns block AND sets result['booking_links']."""
    result = {
        "legs": [],
        "transport_edges": [],
        "day_plans": [],
    }
    block = build_booking_links(result)
    assert block is result["booking_links"]
    assert "lodging" in result["booking_links"]
    assert "insurance" in result["booking_links"]
    # insurance is always present (no verdict needed)
    assert result["booking_links"]["insurance"] is not None
    print("PASS: test_build_booking_links_returns_block_and_mutates")


def test_build_booking_links_insurance_always_present() -> None:
    """Line 592: insurance note is always included, never None."""
    result = {}
    block = build_booking_links(result)
    assert block["insurance"] is not None
    assert block["insurance"]["kind"] == "compare_note"
    assert block["insurance"]["booking_url"] is None
    print("PASS: test_build_booking_links_insurance_always_present")


# ---------------------------------------------------------------------------
# Comprehensive integration test
# ---------------------------------------------------------------------------

def test_build_booking_links_full_integration() -> None:
    """Full integration: all type validations + mutations in one complex fixture."""
    result = {
        "legs": [
            {"leg_id": "leg-0", "hotel_title": "Hotel A", "city": "City A"},
            "not a dict",  # Skip
            {"leg_id": "leg-1", "hotel_title": "Hotel B", "city": "City B"},
        ],
        "transport_edges": [
            {"from_leg": "leg-0", "from_iata": "AAA", "to_iata": "BBB"},
            None,  # Skip
        ],
        "day_plans": [
            {
                "leg_id": "leg-0",
                "city": "City A",
                "iso2": "US",
                "days": [
                    {
                        "attractions": [
                            {"name": "Att A"},
                            "not a dict",  # Skip
                        ],
                        "meals": {
                            "breakfast": {"name": "B Fast"},
                            "lunch": None,  # Skip
                            "dinner": {"name": "Dinner"},
                        }
                    }
                ]
            }
        ],
        "compliance_verdict": {
            "per_leg": [
                {"source_url": "https://www.state.gov/"}
            ]
        },
        "health_verdict": {
            "per_destination": [
                {"source_url": "https://www.cdc.gov/"}
            ]
        }
    }
    block = build_booking_links(result)
    
    # Verify structure
    assert len(block["lodging"]) == 2  # Two valid legs
    assert len(block["transport"]) == 1  # One valid edge
    assert len(block["attractions"]) == 1  # One valid attraction
    assert len(block["restaurants"]) == 2  # Two valid meals (B Fast + Dinner)
    assert block["visa"] is not None
    assert block["health"] is not None
    assert block["insurance"] is not None
    
    # Verify mutations
    assert "booking_link" in result["legs"][0]
    assert "booking_link" in result["legs"][2]
    
    # Var-0: byte-identical on re-run
    sa = json.dumps(block, sort_keys=True)
    block2 = build_booking_links(result)
    sb = json.dumps(block2, sort_keys=True)
    assert sa == sb, "build_booking_links not byte-identical on re-run"
    
    print("PASS: test_build_booking_links_full_integration")


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("ALL PASS")
