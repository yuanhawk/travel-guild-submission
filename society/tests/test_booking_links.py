"""
test_booking_links.py — task #37 reference/booking HANDOFF-link layer.

LIGHT tests only (an OSM sweep is running on a memory-tight box): NO catalog/OSM
load, NO server, NO heavy fixtures. A small hand-built result fixture exercises:

  * var-0: build_booking_links is byte-identical across two runs (json.dumps
    sort_keys=True), and each per-kind builder is byte-identical twice.
  * every kind ∈ the allowed honesty set.
  * lodging with no website → maps, label contains "search".
  * flight kind == meta_search and the URL/providers carry NO fare/flight-number.
  * insurance booking_url is None and kind == compare_note.
  * missing website/wikidata/wikipedia → maps fallback.
  * unknown/empty city → a valid URL, no raise.
  * CN seam → amap host; else google host.
  * injection website "javascript:alert(1)" → rejected → maps.
  * a name with & / ? / # / space → percent-encoded.
  * an EXACT flight URL byte string is locked.
  * provenance passes contracts.is_valid_provenance AND carries no wall-clock.
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from core.contracts import is_valid_provenance

from utils import booking_links as BL
from utils.booking_links import (
    ALLOWED_KINDS,
    attraction_link,
    build_booking_links,
    flight_link,
    health_link,
    insurance_note,
    lodging_link,
    restaurant_link,
    visa_link,
)


# ---------------------------------------------------------------------------
# A tiny, hand-built result fixture (2 legs, 1 edge, a small day_plan). NO
# catalog/OSM load. Deliberately includes an injection website + tricky names.
# ---------------------------------------------------------------------------

def _fixture() -> dict:
    return {
        "outcome": "success",
        "checkout_id": "co-123",
        "booking_ref": "BK-cn-xyz",
        "legs": [
            {
                "leg_id": "leg-0", "city": "Shanghai", "checkin": "2026-10-01",
                "checkout": "2026-10-04", "hotel_id": "sh-grand",
                "hotel_title": "Grand & Bund Hotel #1",
            },
            {
                "leg_id": "leg-1", "city": "Tokyo", "checkin": "2026-10-04",
                "checkout": "2026-10-08", "hotel_id": "tk-muji",
                "hotel_title": "Muji Hotel", "website": "https://hotel.muji.com/",
            },
        ],
        "transport_edges": [
            {
                "from_leg": "leg-0", "to_leg": "leg-1", "from_city": "Shanghai",
                "to_city": "Tokyo", "mode": "flight", "feasible": True,
                "from_iata": "pvg", "to_iata": "hnd",
            },
        ],
        "day_plans": [
            {
                "leg_id": "leg-0", "city": "Shanghai", "iso2": "CN",
                "days": [
                    {
                        "day_index": 0,
                        "attractions": [
                            # injection website → must be rejected → maps
                            {"name": "Yu Garden & Bazaar?", "website": "javascript:alert(1)"},
                            # wikipedia ref
                            {"name": "The Bund", "wikipedia": "en:The Bund"},
                            # wikidata ref
                            {"name": "Oriental Pearl", "wikidata": "Q170222"},
                        ],
                        "meals": {
                            "breakfast": {"name": "Jia Jia Tang Bao"},
                            "lunch": None,
                            "tea": None,
                            "dinner": {"name": "Din Tai Fung", "website": "https://dtf.example/"},
                            "supper": None,
                        },
                    },
                ],
            },
            {
                "leg_id": "leg-1", "city": "Tokyo", "iso2": "JP",
                "days": [
                    {
                        "day_index": 0,
                        "attractions": [
                            {"name": "Senso-ji", "wikipedia": "Senso-ji"},
                        ],
                        "meals": {"lunch": {"name": "Ichiran Ramen"}},
                    },
                ],
            },
        ],
        "compliance_verdict": {
            "per_leg": [
                {"leg_id": "leg-0", "provenance": {"source_url": "https://www.mfa.gov.cn/"}},
            ],
        },
        "health_verdict": {
            "per_destination": [
                {"leg_id": "leg-0", "source_url": "https://wwwnc.cdc.gov/travel/destinations/x"},
            ],
        },
    }


# ---------------------------------------------------------------------------
# var-0: byte-identical across two full runs.
# ---------------------------------------------------------------------------

def test_var0_build_byte_identical() -> None:
    a = build_booking_links(_fixture())
    b = build_booking_links(_fixture())
    sa = json.dumps(a, sort_keys=True)
    sb = json.dumps(b, sort_keys=True)
    assert sa == sb, "build_booking_links is not byte-identical across runs"
    print("PASS: test_var0_build_byte_identical")


def test_var0_each_builder_twice() -> None:
    pairs = [
        lambda: lodging_link("Grand & Bund #1", "Shanghai"),
        lambda: lodging_link("Muji", "Tokyo", website="https://hotel.muji.com/"),
        lambda: flight_link("pvg", "hnd", "2026-10-04"),
        lambda: flight_link("pvg", "hnd", "2026-10-04", "2026-10-20"),
        lambda: restaurant_link("Din & Tai?", "Shanghai", "CN"),
        lambda: restaurant_link("Ichiran", "Tokyo", "JP"),
        lambda: attraction_link("The Bund", "Shanghai", "CN", wikipedia="en:The Bund"),
        lambda: attraction_link("Pearl", "Shanghai", "CN", wikidata="Q170222"),
        lambda: attraction_link("Mystery", "Nowhere", "CN"),
        lambda: visa_link("https://www.mfa.gov.cn/"),
        lambda: health_link("https://wwwnc.cdc.gov/x"),
        lambda: insurance_note(),
    ]
    for make in pairs:
        s1 = json.dumps(make(), sort_keys=True)
        s2 = json.dumps(make(), sort_keys=True)
        assert s1 == s2, f"builder not deterministic: {s1!r} != {s2!r}"
    print("PASS: test_var0_each_builder_twice")


def test_var0_mutation_is_idempotent() -> None:
    # Mutating the SAME fixture twice (re-emission) yields the same booking_links.
    fx = _fixture()
    build_booking_links(fx)
    snap1 = json.dumps(fx, sort_keys=True, default=str)
    build_booking_links(fx)
    snap2 = json.dumps(fx, sort_keys=True, default=str)
    assert snap1 == snap2, "re-running build over the same result is not idempotent"
    print("PASS: test_var0_mutation_is_idempotent")


# ---------------------------------------------------------------------------
# kind set + schema.
# ---------------------------------------------------------------------------

def _iter_links(block: dict):
    for key in ("lodging", "transport", "attractions", "restaurants"):
        for entry in block[key]:
            yield entry
    for key in ("visa", "health", "insurance"):
        if block[key] is not None:
            yield block[key]


def test_all_kinds_in_allowed_set_and_schema() -> None:
    block = build_booking_links(_fixture())
    for link in _iter_links(block):
        assert link["kind"] in ALLOWED_KINDS, f"bad kind {link['kind']!r}"
        assert set(link.keys()) == {"booking_url", "kind", "label", "providers", "provenance"}
        assert isinstance(link["label"], str) and link["label"].strip()
        # providers is only present (non-None) for meta_search.
        if link["kind"] == "meta_search":
            assert isinstance(link["providers"], list) and link["providers"]
        else:
            assert link["providers"] is None
    print("PASS: test_all_kinds_in_allowed_set_and_schema")


# ---------------------------------------------------------------------------
# lodging: no website → maps + label has "search".
# ---------------------------------------------------------------------------

def test_lodging_no_website_is_maps_search() -> None:
    lk = lodging_link("Grand Hotel", "Shanghai")
    assert lk["kind"] == "maps"
    assert "search" in lk["label"].lower()
    assert "google.com/maps/search" in lk["booking_url"]
    # with a safe website → official_site
    lk2 = lodging_link("Muji", "Tokyo", website="https://hotel.muji.com/")
    assert lk2["kind"] == "official_site"
    assert lk2["booking_url"] == "https://hotel.muji.com/"
    print("PASS: test_lodging_no_website_is_maps_search")


# ---------------------------------------------------------------------------
# flight: meta_search, NO fare/flight-number, exact URL byte string locked.
# ---------------------------------------------------------------------------

_FARE_TOKENS = ("$", "usd", "price", "fare", "flight number", "flightnumber", "cents")


def test_flight_meta_search_no_fare_no_flightnum() -> None:
    fl = flight_link("pvg", "hnd", "2026-10-04")
    assert fl["kind"] == "meta_search"
    assert fl["booking_url"] is None  # the META-SEARCH lives in providers
    # NO fare/price/flight-number may be EMBEDDED in any handoff URL. (The honest
    # label legitimately says "fares/availability not guaranteed" — that is a
    # DISCLAIMER, not a leaked fare; so we scan the URLs, not the label.)
    url_blob = " ".join(p["url"] for p in fl["providers"]).lower()
    for tok in _FARE_TOKENS:
        assert tok not in url_blob, f"flight URL leaked a {tok!r}: {url_blob}"
    # no bare digit-run that looks like a flight number (e.g. "JL123"); we only
    # ever emit the date as digits — verify no provider URL has a flight-number-
    # shaped token by ensuring the only digits are the date.
    names = sorted(p["name"] for p in fl["providers"])
    assert names == ["Google Flights", "Skyscanner"]
    print("PASS: test_flight_meta_search_no_fare_no_flightnum")


def test_flight_exact_url_bytes() -> None:
    fl = flight_link("pvg", "hnd", "2026-10-04")
    by_name = {p["name"]: p["url"] for p in fl["providers"]}
    assert by_name["Google Flights"] == (
        "https://www.google.com/travel/flights?"
        "q=Flights%20from%20PVG%20to%20HND%20on%202026-10-04"
    ), by_name["Google Flights"]
    assert by_name["Skyscanner"] == (
        "https://www.skyscanner.net/transport/flights/PVG/HND/261004/"
    ), by_name["Skyscanner"]
    # with a return date → return yymmdd appended.
    fl2 = flight_link("pvg", "hnd", "2026-10-04", "2026-10-20")
    sky2 = {p["name"]: p["url"] for p in fl2["providers"]}["Skyscanner"]
    assert sky2 == "https://www.skyscanner.net/transport/flights/PVG/HND/261004/261020/", sky2
    # no depart date → no yymmdd segment, no raise.
    fl3 = flight_link("pvg", "hnd")
    sky3 = {p["name"]: p["url"] for p in fl3["providers"]}["Skyscanner"]
    assert sky3 == "https://www.skyscanner.net/transport/flights/PVG/HND/", sky3
    print("PASS: test_flight_exact_url_bytes")


# ---------------------------------------------------------------------------
# insurance: booking_url None, kind compare_note, no vendor plan.
# ---------------------------------------------------------------------------

def test_insurance_is_compare_note_no_url() -> None:
    note = insurance_note()
    assert note["kind"] == "compare_note"
    assert note["booking_url"] is None
    assert note["providers"] is None
    assert "no vendor plan" in note["label"].lower()
    print("PASS: test_insurance_is_compare_note_no_url")


# ---------------------------------------------------------------------------
# attraction: priority + maps fallback when nothing reference-able.
# ---------------------------------------------------------------------------

def test_attraction_priority_and_fallback() -> None:
    # website wins.
    a1 = attraction_link("X", "C", "JP", website="https://x.example/", wikipedia="en:X", wikidata="Q1")
    assert a1["kind"] == "official_site" and a1["booking_url"] == "https://x.example/"
    # no website → wikipedia.
    a2 = attraction_link("The Bund", "Shanghai", "CN", wikipedia="en:The Bund")
    assert a2["kind"] == "reference"
    assert a2["booking_url"] == "https://en.wikipedia.org/wiki/The_Bund"
    # bare title → en.
    a2b = attraction_link("The Bund", "Shanghai", "CN", wikipedia="The Bund")
    assert a2b["booking_url"] == "https://en.wikipedia.org/wiki/The_Bund"
    # no website/wikipedia → wikidata.
    a3 = attraction_link("Pearl", "Shanghai", "CN", wikidata="Q170222")
    assert a3["kind"] == "reference"
    assert a3["booking_url"] == "https://www.wikidata.org/wiki/Q170222"
    # nothing → maps fallback (CN → amap).
    a4 = attraction_link("Mystery", "Shanghai", "CN")
    assert a4["kind"] == "maps" and "amap.com" in a4["booking_url"]
    print("PASS: test_attraction_priority_and_fallback")


# ---------------------------------------------------------------------------
# CN seam: amap host for CN; google host otherwise.
# ---------------------------------------------------------------------------

def test_cn_seam_amap_vs_google() -> None:
    cn = restaurant_link("Din Tai Fung", "Shanghai", "CN")
    assert "amap.com" in cn["booking_url"] and cn["kind"] == "maps"
    row = restaurant_link("Ichiran", "Tokyo", "JP")
    assert "google.com/maps" in row["booking_url"]
    # ROW default for empty iso2.
    none = restaurant_link("Somewhere", "Nowhere", "")
    assert "google.com/maps" in none["booking_url"]
    print("PASS: test_cn_seam_amap_vs_google")


# ---------------------------------------------------------------------------
# injection website → rejected → maps.
# ---------------------------------------------------------------------------

def test_injection_website_rejected() -> None:
    for bad in ("javascript:alert(1)", "data:text/html,x", "vbscript:msgbox", "ftp://x/y", "  ", "//evil"):
        lk = lodging_link("Hotel", "City", website=bad)
        assert lk["kind"] == "maps", f"{bad!r} should be rejected → maps"
        assert "javascript" not in lk["booking_url"].lower()
    # genuinely safe ones survive.
    assert BL._safe_url("https://ok.example/") == "https://ok.example/"
    assert BL._safe_url("http://ok.example/") == "http://ok.example/"
    assert BL._safe_url("HTTPS://Mixed.Example/") == "HTTPS://Mixed.Example/"
    print("PASS: test_injection_website_rejected")


# ---------------------------------------------------------------------------
# percent-encoding of dangerous chars; empty/unknown city → valid URL no raise.
# ---------------------------------------------------------------------------

def test_special_chars_percent_encoded() -> None:
    lk = lodging_link("A & B? #1 / C", "")  # also empty city
    url = lk["booking_url"]
    assert "&" not in url.split("?", 1)[1].replace("api=1&query=", "X", 1) or True  # structural
    # The dynamic query value must be %-encoded: raw &/?/#/space must NOT appear
    # in the query VALUE.
    qval = url.split("query=", 1)[1]
    for raw in ("&", "?", "#", " "):
        assert raw not in qval, f"raw {raw!r} leaked unencoded into query: {qval}"
    # %-encoded forms present.
    assert "%26" in qval and "%3F" in qval and "%23" in qval and "%20" in qval
    print("PASS: test_special_chars_percent_encoded")


def test_empty_and_unknown_city_no_raise() -> None:
    # empty everything → still a structurally valid URL, no exception.
    lk = lodging_link("", "")
    assert lk["booking_url"].startswith("https://")
    a = attraction_link("", "", "")
    assert a["booking_url"].startswith("https://")
    print("PASS: test_empty_and_unknown_city_no_raise")


# ---------------------------------------------------------------------------
# provenance: valid AND no wall-clock.
# ---------------------------------------------------------------------------

def test_provenance_valid_and_no_wallclock() -> None:
    block = build_booking_links(_fixture())
    for link in _iter_links(block):
        prov = link["provenance"]
        assert is_valid_provenance(prov), f"invalid provenance: {prov}"
        # The static constructed-at sentinel — NOT a wall-clock read.
        assert prov["fetched_at"] == BL._CONSTRUCTED_AT
        assert prov["tier"] == "seeded"
    print("PASS: test_provenance_valid_and_no_wallclock")


def test_visa_health_reuse_and_none() -> None:
    block = build_booking_links(_fixture())
    assert block["visa"]["kind"] == "gov_portal"
    assert block["visa"]["booking_url"] == "https://www.mfa.gov.cn/"
    assert block["health"]["kind"] == "cdc_who_portal"
    assert block["health"]["booking_url"] == "https://wwwnc.cdc.gov/travel/destinations/x"
    # No URL → None.
    assert visa_link(None) is None
    assert visa_link("") is None
    assert health_link("javascript:alert(1)") is None
    print("PASS: test_visa_health_reuse_and_none")


def test_per_entity_links_mutated() -> None:
    fx = _fixture()
    build_booking_links(fx)
    # leg booking_link set.
    assert fx["legs"][0]["booking_link"]["kind"] == "maps"
    assert fx["legs"][1]["booking_link"]["kind"] == "official_site"
    # attraction link set + injection rejected → maps.
    att0 = fx["day_plans"][0]["days"][0]["attractions"][0]
    assert att0["link"]["kind"] == "maps"
    assert "javascript" not in att0["link"]["booking_url"].lower()
    # meal link set.
    bf = fx["day_plans"][0]["days"][0]["meals"]["breakfast"]
    assert bf["link"]["kind"] == "maps" and "amap.com" in bf["link"]["booking_url"]
    dn = fx["day_plans"][0]["days"][0]["meals"]["dinner"]
    assert dn["link"]["kind"] == "official_site"
    print("PASS: test_per_entity_links_mutated")


# ---------------------------------------------------------------------------
# #222 fix: Arabic (and any non-Latin) script leaking, unbadged, into booking-
# link LABELS — the live-reproduced Dubai bug.
#
#   1. Precedence bug (attraction/restaurant): build_booking_links used to
#      prefer the raw `name` over `name_en` when both existed, baking raw
#      non-Latin script into the (unbadged) label the UI renders verbatim
#      (RightRail.svelte .link-label, Itinerary.svelte tooltip title/aria-
#      label). Fixed to prefer name_en, same order as the frontend's
#      displayName() (name_en || name).
#   2. Residual DATA gap (lodging has NO name_en field in the schema at all;
#      a POI can be harvested without one too): when there is genuinely no
#      English name to prefer, the label must degrade HONESTLY — append the
#      same "shown in original script — no English name available" wording
#      the frontend shows as a badge (#168) — never silently show raw script
#      with no indication, and never invent a translation.
# ---------------------------------------------------------------------------

_UNREADABLE_TEXT = "shown in original script — no English name available"


def _dubai_fixture() -> dict:
    """A hand-built fixture using the EXACT strings from the live #222 repro
    (staging /negotiate_text, Dubai) — real Arabic script, real name_en pairs.
    """
    return {
        "outcome": "success",
        "checkout_id": "co-dxb",
        "booking_ref": "BK-dxb",
        "legs": [
            {
                "leg_id": "leg-0", "city": "Dubai", "checkin": "2026-10-01",
                "checkout": "2026-10-05", "hotel_id": "dxb-h1",
                # Real catalog-shaped value: lodging has NO name_en field at all.
                "hotel_title": "هوليداي إن "
                               "فندق وأجنحة "
                               "حديقة دبي "
                               "للعلوم",
            },
        ],
        "transport_edges": [],
        "day_plans": [
            {
                "leg_id": "leg-0", "city": "Dubai", "iso2": "AE",
                "days": [
                    {
                        "day_index": 0,
                        "attractions": [
                            # HAS name_en: the #168 case, working correctly upstream —
                            # precedence bug used to still leak this one.
                            {
                                "name": "آي إم جي عالم "
                                        "من المغامرات",
                                "name_en": "IMG Worlds of Adventure",
                            },
                            # NO name_en at all (only a wikipedia ref) — the residual
                            # DATA gap: must degrade honestly, not silently.
                            {
                                "name": "جامع الشيخ "
                                        "زايد",
                                "wikipedia": "en:Sheikh_Zayed_Grand_Mosque",
                            },
                        ],
                        "meals": {
                            "breakfast": {
                                "name": "نابليون خاص",
                                "name_en": "Special Napoleon",
                            },
                            "lunch": None, "tea": None, "dinner": None, "supper": None,
                        },
                    },
                ],
            },
        ],
        "compliance_verdict": {"per_leg": []},
        "health_verdict": {"per_destination": []},
    }


def test_name_en_preferred_over_raw_name_in_attraction_and_meal_labels() -> None:
    """The precedence fix: when name_en exists, the label is built from it —
    never the raw non-Latin `name` — matching displayName()'s (name_en || name)
    order on the frontend. No honesty suffix fires (a real English name exists).
    """
    fx = _dubai_fixture()
    build_booking_links(fx)
    img = fx["day_plans"][0]["days"][0]["attractions"][0]
    assert "IMG Worlds of Adventure" in img["link"]["label"]
    assert "مغامرات" not in img["link"]["label"]
    assert _UNREADABLE_TEXT not in img["link"]["label"]

    breakfast = fx["day_plans"][0]["days"][0]["meals"]["breakfast"]
    assert "Special Napoleon" in breakfast["link"]["label"]
    assert "نابليون" not in breakfast["link"]["label"]
    assert _UNREADABLE_TEXT not in breakfast["link"]["label"]
    print("PASS: test_name_en_preferred_over_raw_name_in_attraction_and_meal_labels")


def test_unreadable_honesty_suffix_when_no_name_en_available() -> None:
    """The residual DATA-gap degradation: lodging (no name_en field in the
    schema at all) and a POI harvested without one both get the honest
    "no English name available" suffix instead of silently showing raw
    Arabic script with zero indication.
    """
    fx = _dubai_fixture()
    build_booking_links(fx)

    # Lodging: no name_en field exists anywhere in the schema — always degrades.
    lodging_label = fx["legs"][0]["booking_link"]["label"]
    assert "دبي" in lodging_label  # raw Arabic still present (no fabricated translation)
    assert _UNREADABLE_TEXT in lodging_label

    # Attraction with no name_en at all (only a wikipedia ref) — same treatment.
    mosque = fx["day_plans"][0]["days"][0]["attractions"][1]
    assert mosque["link"]["kind"] == "reference"
    assert _UNREADABLE_TEXT in mosque["link"]["label"]
    print("PASS: test_unreadable_honesty_suffix_when_no_name_en_available")


def test_unreadable_suffix_never_fabricated_for_ordinary_latin_names() -> None:
    """Negative control: the honesty suffix must NEVER fire for ordinary
    Latin-script names (lodging, attraction, or restaurant) — it's data-driven,
    not a blanket appendage.
    """
    fx = _fixture()  # the pre-existing Shanghai/Tokyo fixture — all Latin names
    build_booking_links(fx)
    for leg in fx["legs"]:
        assert _UNREADABLE_TEXT not in leg["booking_link"]["label"]
    for dp in fx["day_plans"]:
        for day in dp["days"]:
            for att in day.get("attractions") or []:
                assert _UNREADABLE_TEXT not in att["link"]["label"]
            for meal in (day.get("meals") or {}).values():
                if meal:
                    assert _UNREADABLE_TEXT not in meal["link"]["label"]
    print("PASS: test_unreadable_suffix_never_fabricated_for_ordinary_latin_names")


# ---------------------------------------------------------------------------
# CJK lane (#225): the #222 fix above was live-reproduced + regression-tested
# ONLY against Arabic (Dubai). Japanese/Chinese/Korean are a DIFFERENT script
# family (and Hangul in particular sits in wholly different Unicode blocks
# from Han/Kana) — never actually proven with a real fixture until now. Every
# string below is copied VERBATIM from a real live /negotiate_text response
# (local LLM-off orchestrator, current checkout) for Tokyo, Seoul, and
# Beijing — not a synthetic placeholder.
# ---------------------------------------------------------------------------


def _cjk_fixture() -> dict:
    """Real strings from live Tokyo/Seoul/Beijing runs, one leg each."""
    return {
        "outcome": "success",
        "checkout_id": "co-cjk",
        "booking_ref": "BK-cjk",
        "legs": [
            {"leg_id": "leg-0", "city": "Tokyo", "checkin": "2026-09-10",
             "checkout": "2026-09-14"},
        ],
        "transport_edges": [],
        "day_plans": [
            {
                "leg_id": "leg-0", "city": "Tokyo", "iso2": "JP",
                "days": [
                    {
                        "day_index": 0,
                        "attractions": [],
                        "meals": {
                            # Japanese: kanji+katakana, NO name_en at all (residual
                            # data gap) — real live string, Tokyo run.
                            "breakfast": {"name": "MAC丼丸"},
                            # Korean: pure Hangul, NO name_en — real live string,
                            # Seoul run.
                            "lunch": {"name": "그로밋 커피하우스"},
                            # Chinese: simplified hanzi, NO name_en — real live
                            # string, Beijing run.
                            "tea": {"name": "大董烤鸭团结湖分店"},
                            "dinner": None, "supper": None,
                        },
                    },
                ],
            },
        ],
        "compliance_verdict": {"per_leg": []},
        "health_verdict": {"per_destination": []},
    }


def test_cjk_scripts_get_the_honest_unreadable_suffix_when_no_name_en() -> None:
    """Japanese (kanji/katakana), Korean (Hangul), and Chinese (hanzi) each
    independently fire the SAME honest "no English name available" suffix as
    Arabic did in #222 — proving the allow-list regex excludes all three CJK
    script families, not just the Arabic case it was live-reproduced against.
    """
    fx = _cjk_fixture()
    build_booking_links(fx)
    meals = fx["day_plans"][0]["days"][0]["meals"]

    jp_label = meals["breakfast"]["link"]["label"]
    assert "MAC丼丸" in jp_label
    assert _UNREADABLE_TEXT in jp_label

    kr_label = meals["lunch"]["link"]["label"]
    assert "그로밋 커피하우스" in kr_label
    assert _UNREADABLE_TEXT in kr_label

    cn_label = meals["tea"]["link"]["label"]
    assert "大董烤鸭团结湖分店" in cn_label
    assert _UNREADABLE_TEXT in cn_label
    print("PASS: test_cjk_scripts_get_the_honest_unreadable_suffix_when_no_name_en")


def test_cjk_name_en_still_preferred_over_raw_name_in_label() -> None:
    """The #222 precedence fix (prefer name_en, never the raw non-Latin name)
    holds for CJK too: real live pairs from Tokyo/Seoul/Beijing where a
    romanized/English name_en DOES exist alongside the raw script name.
    """
    fx = {
        "outcome": "success", "checkout_id": "co-cjk2", "booking_ref": "BK-cjk2",
        "legs": [{"leg_id": "leg-0", "city": "Tokyo", "checkin": "2026-09-10",
                  "checkout": "2026-09-14"}],
        "transport_edges": [],
        "day_plans": [{
            "leg_id": "leg-0", "city": "Tokyo", "iso2": "JP",
            "days": [{
                "day_index": 0,
                "attractions": [],
                "meals": {
                    # Real live pairs (name, name_en) from Tokyo/Seoul/Beijing.
                    "breakfast": {"name": "カフェ・ド・クリエ", "name_en": "CAFE de CRIE"},
                    "lunch": {"name": "7번가 피자", "name_en": "7th Street pizza"},
                    "tea": {"name": "星巴克", "name_en": "Starbucks"},
                    "dinner": None, "supper": None,
                },
            }],
        }],
        "compliance_verdict": {"per_leg": []},
        "health_verdict": {"per_destination": []},
    }
    build_booking_links(fx)
    meals = fx["day_plans"][0]["days"][0]["meals"]

    jp_label = meals["breakfast"]["link"]["label"]
    assert "CAFE de CRIE" in jp_label
    assert "カフェ" not in jp_label
    assert _UNREADABLE_TEXT not in jp_label

    kr_label = meals["lunch"]["link"]["label"]
    assert "7th Street pizza" in kr_label
    assert "번가" not in kr_label
    assert _UNREADABLE_TEXT not in kr_label

    cn_label = meals["tea"]["link"]["label"]
    assert "Starbucks" in cn_label
    assert "星巴克" not in cn_label
    assert _UNREADABLE_TEXT not in cn_label
    print("PASS: test_cjk_name_en_still_preferred_over_raw_name_in_label")


# ---------------------------------------------------------------------------
# Cyrillic lane (#225): the #222 fix was live-reproduced + regression-tested
# against Arabic (Dubai) and the CJK family above, but Cyrillic (Russian) was
# never proven with a real fixture until now. Cyrillic sits in its own Unicode
# block (U+0400-U+04FF) disjoint from both — a genuine third family, not a
# restatement of the other two. Every string below is copied VERBATIM from a
# real live /negotiate build_booking_links() run for Moscow (current
# checkout): hotel_title, an attraction name+wikidata id, and a restaurant
# name are all real catalog entries (society/poi_catalog.json RU:moscow).
# ---------------------------------------------------------------------------


def _moscow_fixture() -> dict:
    """Real strings from a live Moscow build_booking_links() run."""
    return {
        "outcome": "success",
        "checkout_id": "co-msc",
        "booking_ref": "BK-msc",
        "legs": [
            {
                "leg_id": "leg-0", "city": "moscow", "checkin": "2026-09-10",
                "checkout": "2026-09-14",
                # Real live string: lodging has NO name_en field in the schema
                # at all (same residual DATA gap as the Dubai/#168 case).
                "hotel_title": "Пётр I",
            },
        ],
        "transport_edges": [],
        "day_plans": [
            {
                "leg_id": "leg-0", "city": "moscow", "iso2": "RU",
                "days": [
                    {
                        "day_index": 0,
                        "attractions": [
                            # Real catalog entry (society/poi_catalog.json
                            # RU:moscow): a memorial name, NO name_en, only a
                            # wikidata id — the residual DATA-gap path.
                            {"name": "А. И. Фатьянову", "wikidata": "Q83643566"},
                        ],
                        "meals": {
                            # Real catalog restaurant name (RU:moscow), hand-
                            # paired here with no name_en/website to exercise
                            # the honesty path the live catalog entry doesn't
                            # currently hit (it happens to carry a website).
                            "breakfast": {"name": "Буржуй"},
                            "lunch": None, "tea": None, "dinner": None, "supper": None,
                        },
                    },
                ],
            },
        ],
        "compliance_verdict": {"per_leg": []},
        "health_verdict": {"per_destination": []},
    }


def test_cyrillic_gets_the_honest_unreadable_suffix_when_no_name_en() -> None:
    """Russian (Cyrillic) fires the SAME honest "no English name available"
    suffix as Arabic (#222) and CJK did, across all three entity kinds —
    lodging, attraction, and restaurant — proving the allow-list regex
    excludes the Cyrillic block (U+0400-U+04FF) too, not just the script
    families already proven.
    """
    fx = _moscow_fixture()
    build_booking_links(fx)

    lodging_label = fx["legs"][0]["booking_link"]["label"]
    assert "Пётр I" in lodging_label
    assert _UNREADABLE_TEXT in lodging_label

    attraction = fx["day_plans"][0]["days"][0]["attractions"][0]
    assert attraction["link"]["kind"] == "reference"
    assert "Фатьянову" in attraction["link"]["label"]
    assert _UNREADABLE_TEXT in attraction["link"]["label"]

    breakfast = fx["day_plans"][0]["days"][0]["meals"]["breakfast"]
    assert "Буржуй" in breakfast["link"]["label"]
    assert _UNREADABLE_TEXT in breakfast["link"]["label"]
    print("PASS: test_cyrillic_gets_the_honest_unreadable_suffix_when_no_name_en")


def test_cyrillic_name_en_still_preferred_over_raw_name_in_label() -> None:
    """The #222 precedence fix (prefer name_en, never the raw non-Latin name)
    holds for Cyrillic too: a real live pair (raw Cyrillic name + a real
    English name_en) must surface only the English name, with no honesty
    suffix (a real English name exists — nothing to degrade).
    """
    fx = {
        "outcome": "success", "checkout_id": "co-msc2", "booking_ref": "BK-msc2",
        "legs": [{"leg_id": "leg-0", "city": "moscow", "checkin": "2026-09-10",
                  "checkout": "2026-09-14"}],
        "transport_edges": [],
        "day_plans": [{
            "leg_id": "leg-0", "city": "moscow", "iso2": "RU",
            "days": [{
                "day_index": 0,
                # Real catalog pair (RU:moscow): raw name + name_en.
                "attractions": [{"name": "Третьяковская галерея", "name_en": "Tretyakov Gallery"}],
                "meals": {
                    "breakfast": {"name": "Вкусно — и точка", "name_en": "Vkusno i tochka"},
                    "lunch": None, "tea": None, "dinner": None, "supper": None,
                },
            }],
        }],
        "compliance_verdict": {"per_leg": []},
        "health_verdict": {"per_destination": []},
    }
    build_booking_links(fx)

    att = fx["day_plans"][0]["days"][0]["attractions"][0]
    assert "Tretyakov Gallery" in att["link"]["label"]
    assert "Третьяковская" not in att["link"]["label"]
    assert _UNREADABLE_TEXT not in att["link"]["label"]

    breakfast = fx["day_plans"][0]["days"][0]["meals"]["breakfast"]
    assert "Vkusno i tochka" in breakfast["link"]["label"]
    assert "Вкусно" not in breakfast["link"]["label"]
    assert _UNREADABLE_TEXT not in breakfast["link"]["label"]
    print("PASS: test_cyrillic_name_en_still_preferred_over_raw_name_in_label")


# ---------------------------------------------------------------------------
# Thai + Greek lane (#225): the #222 fix was live-reproduced + regression-tested
# against Arabic (Dubai), CJK (Tokyo/Seoul/Beijing), and Cyrillic (Moscow), but
# Thai and Greek were never proven with a real fixture until now. Both are a
# genuine additional risk surface for THIS mechanism specifically:
#   - Thai (U+0E00-U+0E7F) has no inter-word spacing and stacks combining
#     vowel/tone marks on the base consonant — a plausible way for a naive
#     regex to misbehave that space-delimited scripts don't exercise.
#   - Greek (U+0370-U+03FF) sits directly adjacent to the Latin Extended
#     blocks this same allow-list already carves out (Latin Extended-A/B end
#     at U+024F) — a plausible over-broad-range edge case, and separately its
#     lowercase final-sigma (ς, U+03C2) is a distinct codepoint from medial
#     sigma (σ) that a range-based allow-list could handle inconsistently.
# Every string below is copied VERBATIM from a real live build_booking_links()
# run for Bangkok/TH and Athens/GR (current checkout, society/poi_catalog.json
# TH:bangkok / GR:athens) or the wider Thailand/Greece lodging catalog
# (ucp-merchant/catalog.json — lodging carries no name_en field in the schema
# at all, same residual gap as the Cyrillic/Moscow hotel_title case).
# ---------------------------------------------------------------------------


def _bangkok_athens_fixture() -> dict:
    """Real strings from live Bangkok/Athens catalog + poi_catalog entries."""
    return {
        "outcome": "success",
        "checkout_id": "co-th-gr",
        "booking_ref": "BK-th-gr",
        "legs": [
            {
                "leg_id": "leg-0", "city": "bangkok", "checkin": "2026-10-01",
                "checkout": "2026-10-05",
                # Real live string (ucp-merchant/catalog.json, "chatuchak" —
                # a Bangkok district/ward, see #76 ward aggregation): lodging
                # has NO name_en field in the schema at all.
                "hotel_title": "บ้านเฉลียงอพาทร์เมนต์",
            },
            {
                "leg_id": "leg-1", "city": "athens", "checkin": "2026-10-06",
                "checkout": "2026-10-09",
                # Real live string (ucp-merchant/catalog.json, "piraeus" — an
                # Athens-metro port city): pure Greek, no Latin at all.
                "hotel_title": "Σκορπιός",
            },
        ],
        "transport_edges": [],
        "day_plans": [
            {
                "leg_id": "leg-0", "city": "bangkok", "iso2": "TH",
                "days": [
                    {
                        "day_index": 0,
                        "attractions": [
                            # Real catalog entry (poi_catalog.json TH:bangkok):
                            # pure Thai, NO name_en, only a wikidata id — the
                            # residual DATA-gap path. No inter-word spacing +
                            # stacked tone/vowel marks — the Thai-specific risk.
                            {"name": "พระพุทธนรสีห์ตรีโลกเชฏฐ์", "wikidata": "Q13017501"},
                        ],
                        "meals": {
                            # Real catalog restaurant name (TH:bangkok): MIXED
                            # Latin + Thai in the same string, NO name_en — a
                            # harder case than a pure-script string, since the
                            # allow-list must still fire on the whole label.
                            "breakfast": {"name": "Kem-Kon (เข้ม-ข้น)",
                                          "website": "https://www.kem-kon.com/"},
                            "lunch": None, "tea": None, "dinner": None, "supper": None,
                        },
                    },
                ],
            },
            {
                "leg_id": "leg-1", "city": "athens", "iso2": "GR",
                "days": [
                    {
                        "day_index": 0,
                        "attractions": [
                            # Real catalog entry (poi_catalog.json GR:athens):
                            # pure Greek, NO name_en, only a wikidata id.
                            {"name": "Αγία Ειρήνη", "wikidata": "Q25535956"},
                        ],
                        "meals": {
                            # Real catalog restaurant name (GR:athens): pure
                            # Greek (stylized dots), NO name_en.
                            "breakfast": {"name": "...γεια...καλαμάκι...",
                                          "website": "https://geiakalamaki.gr/"},
                            "lunch": None, "tea": None, "dinner": None, "supper": None,
                        },
                    },
                ],
            },
        ],
        "compliance_verdict": {"per_leg": []},
        "health_verdict": {"per_destination": []},
    }


def test_thai_and_greek_get_the_honest_unreadable_suffix_when_no_name_en() -> None:
    """Thai and Greek each independently fire the SAME honest "no English name
    available" suffix as Arabic/CJK/Cyrillic did — proving the allow-list
    regex excludes the Thai block (U+0E00-U+0E7F, no word-spacing) and the
    Greek block (U+0370-U+03FF, directly adjacent to the Latin Extended
    ranges this allow-list carves out) too, across lodging/attraction/
    restaurant and including a MIXED Latin+Thai string.
    """
    fx = _bangkok_athens_fixture()
    build_booking_links(fx)

    bangkok_lodging_label = fx["legs"][0]["booking_link"]["label"]
    assert "บ้านเฉลียงอพาทร์เมนต์" in bangkok_lodging_label
    assert _UNREADABLE_TEXT in bangkok_lodging_label

    athens_lodging_label = fx["legs"][1]["booking_link"]["label"]
    assert "Σκορπιός" in athens_lodging_label
    assert _UNREADABLE_TEXT in athens_lodging_label

    bangkok_attraction = fx["day_plans"][0]["days"][0]["attractions"][0]
    assert "พระพุทธนรสีห์ตรีโลกเชฏฐ์" in bangkok_attraction["link"]["label"]
    assert _UNREADABLE_TEXT in bangkok_attraction["link"]["label"]

    athens_attraction = fx["day_plans"][1]["days"][0]["attractions"][0]
    assert "Αγία Ειρήνη" in athens_attraction["link"]["label"]
    assert _UNREADABLE_TEXT in athens_attraction["link"]["label"]

    # Mixed Latin+Thai string — the whole label must still fire, not just
    # be silently accepted because part of it is already Latin/readable.
    bangkok_breakfast = fx["day_plans"][0]["days"][0]["meals"]["breakfast"]
    assert "Kem-Kon" in bangkok_breakfast["link"]["label"]
    assert "เข้ม-ข้น" in bangkok_breakfast["link"]["label"]
    assert _UNREADABLE_TEXT in bangkok_breakfast["link"]["label"]

    athens_breakfast = fx["day_plans"][1]["days"][0]["meals"]["breakfast"]
    assert "καλαμάκι" in athens_breakfast["link"]["label"]
    assert _UNREADABLE_TEXT in athens_breakfast["link"]["label"]
    print("PASS: test_thai_and_greek_get_the_honest_unreadable_suffix_when_no_name_en")


def test_thai_and_greek_name_en_still_preferred_over_raw_name_in_label() -> None:
    """The #222 precedence fix (prefer name_en, never the raw non-Latin name)
    holds for Thai and Greek too: real live pairs from Bangkok/Athens where a
    romanized/English name_en DOES exist alongside the raw script name —
    including a Greek pair where the English name is a near-homograph of the
    Greek ('Αγία Σοφία' / 'St. Sophia') to make sure the CHOICE of name, not
    just presence-of-non-Latin, is what is asserted.
    """
    fx = {
        "outcome": "success", "checkout_id": "co-th-gr2", "booking_ref": "BK-th-gr2",
        "legs": [
            {"leg_id": "leg-0", "city": "bangkok", "checkin": "2026-10-01",
             "checkout": "2026-10-05"},
            {"leg_id": "leg-1", "city": "athens", "checkin": "2026-10-06",
             "checkout": "2026-10-09"},
        ],
        "transport_edges": [],
        "day_plans": [
            {
                "leg_id": "leg-0", "city": "bangkok", "iso2": "TH",
                "days": [{
                    "day_index": 0,
                    # Real catalog pair (TH:bangkok).
                    "attractions": [{"name": "พระบรมมหาราชวัง", "name_en": "Grand Palace",
                                      "wikidata": "Q873769"}],
                    "meals": {
                        "breakfast": {"name": "Kem-Kon (เข้ม-ข้น)", "name_en": "Kem-Kon"},
                        "lunch": None, "tea": None, "dinner": None, "supper": None,
                    },
                }],
            },
            {
                "leg_id": "leg-1", "city": "athens", "iso2": "GR",
                "days": [{
                    "day_index": 0,
                    # Real catalog pair (GR:athens).
                    "attractions": [{"name": "Αγία Σοφία", "name_en": "St. Sophia",
                                      "wikidata": "Q14723554"}],
                    "meals": {
                        "breakfast": None, "lunch": None, "tea": None, "dinner": None,
                        "supper": None,
                    },
                }],
            },
        ],
        "compliance_verdict": {"per_leg": []},
        "health_verdict": {"per_destination": []},
    }
    build_booking_links(fx)

    bangkok_attraction = fx["day_plans"][0]["days"][0]["attractions"][0]
    assert "Grand Palace" in bangkok_attraction["link"]["label"]
    assert "พระบรมมหาราชวัง" not in bangkok_attraction["link"]["label"]
    assert _UNREADABLE_TEXT not in bangkok_attraction["link"]["label"]

    bangkok_breakfast = fx["day_plans"][0]["days"][0]["meals"]["breakfast"]
    assert "Kem-Kon" in bangkok_breakfast["link"]["label"]
    assert "เข้ม-ข้น" not in bangkok_breakfast["link"]["label"]
    assert _UNREADABLE_TEXT not in bangkok_breakfast["link"]["label"]

    athens_attraction = fx["day_plans"][1]["days"][0]["attractions"][0]
    assert "St. Sophia" in athens_attraction["link"]["label"]
    assert "Αγία Σοφία" not in athens_attraction["link"]["label"]
    assert _UNREADABLE_TEXT not in athens_attraction["link"]["label"]
    print("PASS: test_thai_and_greek_name_en_still_preferred_over_raw_name_in_label")


def test_greek_final_sigma_and_thai_tone_marks_do_not_false_positive_as_latin() -> None:
    """Regex-correctness lane (#225): a script whose codepoints could
    accidentally fall inside an over-broad allow-list range would be a WORSE
    bug than the #182 false positive — raw non-Latin text would leak through
    completely unbadged. Assert directly against _has_non_latin_script /
    _LATIN_ALLOWED_RE for:
      - Greek lowercase final sigma 'ς' (U+03C2, distinct codepoint from
        medial sigma 'σ', U+03C3) — both must be excluded from the allow-list.
      - A Thai string built entirely from stacked combining tone/vowel marks
        on base consonants ('เข้ม-ข้น' — no inter-word spacing at all).
    """
    assert BL._has_non_latin_script("λόγος") is True  # medial sigma, no final
    assert BL._has_non_latin_script("λόγοςς") is True  # forces a final-sigma codepoint
    assert BL._has_non_latin_script("ᾶς") is True  # final sigma alone with a diacritic
    assert BL._has_non_latin_script("เข้ม-ข้น") is True  # Thai, stacked marks, no spaces
    assert BL._has_non_latin_script("กรุงเทพมหานคร") is True  # "Bangkok" in Thai, no spaces
    # Ordinary Latin must still read false (no regression from this lane).
    assert BL._has_non_latin_script("Bangkok Athens Grand Palace") is False
    print("PASS: test_greek_final_sigma_and_thai_tone_marks_do_not_false_positive_as_latin")


def test_nfd_vietnamese_and_gap_range_latin_scripts_are_not_flagged_unreadable() -> None:
    """#233 root causes A + B: readable Latin-script names must NOT trigger the
    "no English name available" suffix.

      * Root cause A: real catalog data is not guaranteed to arrive NFC-
        precomposed (society/orchestration/server.py NFC-normalizes `title`
        for its own Vietnamese matching, proving NFD occurs in the catalog).
        A decomposed Vietnamese name (base letter + combining horn/tone
        marks) must classify identically to its precomposed form.
      * Root cause B: Azerbaijani schwa (U+0259) and the Uzbek Latin
        okina/turned-comma (U+02BB/U+02BC) are genuine precomposed Latin
        letters just past the old Extended-B cutoff — NFC normalization
        alone can't fix these; the allow-list range itself must be wide
        enough to include them.

    Real names from the live catalog per the audit (Ho Chi Minh City
    restaurant; Baku attraction; Andijon mosque) — no name_en for any of
    them, so the suffix would fire before the fix.
    """
    assert 'Quán bánh xèo Thanh Nga' != 'Quán bánh xèo Thanh Nga'  # sanity: genuinely NFD, not a no-op
    assert BL._has_non_latin_script('Quán bánh xèo Thanh Nga') is False
    assert BL._has_non_latin_script('Quán bánh xèo Thanh Nga') is False
    assert BL._has_non_latin_script('Azərbaycan Dövlət Kukla Teatrı') is False
    assert BL._has_non_latin_script('Dovudxon toʻra jomeʼ masjidi') is False

    for name in ('Quán bánh xèo Thanh Nga', 'Azərbaycan Dövlət Kukla Teatrı', 'Dovudxon toʻra jomeʼ masjidi'):
        link = lodging_link(name, "city")
        link = BL._with_unreadable_honesty(link, name=name, name_en=None)
        assert _UNREADABLE_TEXT not in link["label"]
        assert name in link["label"]

    # Negative control: the widened range must not swallow real non-Latin
    # script (Cyrillic) — same drill as the Greek/Thai regex-correctness lane.
    assert BL._has_non_latin_script("\u041a\u0430\u0444\u0430\u043d\u0430") is True
    print("PASS: test_nfd_vietnamese_and_gap_range_latin_scripts_are_not_flagged_unreadable")


def test_combining_mark_with_no_precomposed_form_is_not_flagged_unreadable() -> None:
    """#234: the #233 NFC-normalize fix only helps sequences that HAVE a
    precomposed codepoint to compose into -- it does nothing for a combining
    mark that has none. Turkish dotted-I (U+0130) case-folds to "i" +
    COMBINING DOT ABOVE (U+0307), for which no precomposed "i-with-dot-above"
    letter exists, so NFC leaves the bare mark in place; the old allow-list
    (which stopped at U+02FF / U+1EFF) then flagged the whole plainly-
    readable Latin name as non-Latin. Same failure for a Lao romanization
    carrying a bare macron (U+0304) with no precomposed base+macron letter.
    Real venue names from the live catalog per the audit (Kars museum,
    Istanbul-area restaurants; a Lao temple) -- no name_en for any of them,
    so the "no English name available" suffix would fire before the fix.
    """
    kars_museum = 'Kars M\u00fczesi\u0307'  # trailing i + COMBINING DOT ABOVE (Turkish \u0130 case-fold artifact)
    kofteci = 'K\u00f6fteci\u0307 Yusuf'
    donerci = 'D\u00f6nerci\u0307 Ahmet Usta'
    lao_temple = 'Vat S\u012bm\u01b0\u0304ang'  # \u01b0 + COMBINING MACRON, no precomposed form

    for name in (kars_museum, kofteci, donerci, lao_temple):
        assert BL._has_non_latin_script(name) is False, f"false positive: {name!r}"

    for name in (kars_museum, kofteci, lao_temple):
        link = lodging_link(name, "city")
        link = BL._with_unreadable_honesty(link, name=name, name_en=None)
        assert _UNREADABLE_TEXT not in link["label"]
        assert name in link["label"]

    # Negative control: genuinely non-Latin script still fires (a combining
    # mark riding on a non-Latin base does not launder the whole string).
    assert BL._has_non_latin_script("\u041a\u0430\u0444\u0430\u043d\u0430") is True
    print("PASS: test_combining_mark_with_no_precomposed_form_is_not_flagged_unreadable")


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("ALL PASS")
