"""itinerary_narration.py — #3 Phase 1: the GROUNDED data contract + anti-fabrication validator
for the cosmetic Google-style itinerary narrative.

Pure + deterministic. NO LLM, NO network, NO UI here. These helpers:
  1. build_narration_payload(day_plans, legs) — compact, fully-grounded payload (real attractions +
     restaurants the deterministic day-planner already selected), each item stamped a stable int id.
  2. narration_corpus(payload) — id -> real name, the ONLY places a narrative may surface.
  3. validate_narrative(narrative, corpus) — drops any highlight/dining item whose id isn't in the
     corpus or whose name grossly mismatches (anti-fabrication). Canonicalises names to the real ones.

The narrative itself (built in Phase 2/3 by an LLM) is COSMETIC and var-0-firewalled: it is generated
AFTER the deterministic booking and never feeds back. This module is the honesty firewall — the UI
only ever renders ids that round-trip against the booked plan, so a hallucinated place cannot surface.

KNOWN, ACCEPTED RESIDUALS (reviewed SAFE — neither is a fabrication-reaches-a-chip hole):
  - Prose fields (narrative/summary/title/blurb) are NOT scanned: a fabricated place named ONLY in
    prose (never as a chip) is not caught here. The user chose free-text prose + an on-panel
    AI-provenance disclosure over a fixed template. Chip TEXT is always the deterministic name.
  - Non-Latin completeness: an ASCII real name vs a CJK-only emitted name shares no tokens and is
    DROPPED — a missed REAL item (completeness loss), never a surfaced fake. Note for China/SEA.
"""

from __future__ import annotations

import copy
import re
from typing import Any


def _name(item: dict) -> str | None:
    return item.get("name_en") or item.get("name")


def _as_int(v: Any) -> int | None:
    """Coerce an id to int — tolerant of a JSON-roundtripped string id ('5'), None for anything not a
    clean integer. bool is excluded (it is an int subclass but is never a real id). Used so a real
    item isn't dropped just because the LLM returned its id as a string; junk still fails closed."""
    if isinstance(v, bool):
        return None
    if isinstance(v, int):
        return v
    if isinstance(v, str) and v.strip().lstrip("-").isdigit():
        return int(v)
    return None


def build_narration_payload(day_plans: list[dict] | None, legs: list[dict] | None) -> dict:
    """Compact, grounded payload from the deterministic day_plans + legs. Each attraction/meal gets a
    stable integer id (assigned in deterministic traversal order). Legs with no catalog hit / no days
    are skipped so the narrator is never asked to invent content for an empty city. Pure."""
    leg_by_id = {l.get("leg_id"): l for l in (legs or []) if isinstance(l, dict)}
    counter = {"n": 0}

    def _nid() -> int:
        counter["n"] += 1
        return counter["n"]

    out_legs: list[dict] = []
    for dp in (day_plans or []):
        if not isinstance(dp, dict):
            continue
        # NB: do NOT gate on catalog_hit — a city can miss the ATTRACTION catalog yet still have real
        # restaurants from the dining layer (e.g. Bali, whose POIs sit under sub-areas). Narrate
        # whatever REAL content exists; the empty-day / empty-leg checks below skip a truly empty leg.
        leg = leg_by_id.get(dp.get("leg_id"), {})
        days_out: list[dict] = []
        for day in (dp.get("days") or []):
            if not isinstance(day, dict):
                continue
            atts: list[dict] = []
            for a in (day.get("attractions") or []):
                nm = _name(a) if isinstance(a, dict) else None
                if not nm:
                    continue
                atts.append({"id": _nid(), "name": nm, "category": a.get("category"),
                             "heritage": a.get("heritage"), "opening_hours": a.get("opening_hours"),
                             "website": a.get("website")})
            meals: list[dict] = []
            meals_src = day.get("meals")
            if not isinstance(meals_src, dict):
                meals_src = {}  # defensive: a non-dict meals container can't .items() — self-contained
            for slot, m in meals_src.items():
                if not isinstance(m, dict):
                    continue
                nm = _name(m)
                if not nm:
                    continue
                meals.append({"id": _nid(), "slot": slot, "name": nm,
                              "cuisine": m.get("cuisine"), "website": m.get("website")})
            if atts or meals:
                days_out.append({"day_number": (_as_int(day.get("day_index")) or 0) + 1,
                                 "attractions": atts, "meals": meals})
        if days_out:
            # Phase 4: carry the traveller's stated activity interests + any honest "not in
            # verified catalog" notes so the narrator can speak to intent and honestly acknowledge
            # gaps. CONTEXT only — the grounding corpus (narration_corpus) still restricts every
            # surfaced place to a real POI, so the narrator can never invent one to fit an interest.
            _interests = leg.get("interests") if isinstance(leg.get("interests"), list) else None
            _unmet = [n for n in (dp.get("notes") or [])
                      if isinstance(n, str) and "not present in verified POI catalog" in n]
            _leg_out = {
                "leg_id": dp.get("leg_id"), "city": dp.get("city"), "country": dp.get("country"),
                "checkin": leg.get("checkin"), "checkout": leg.get("checkout"),
                "hotel_title": leg.get("hotel_title") or leg.get("title"),
                "days": days_out,
            }
            if _interests:
                _leg_out["interests"] = _interests
            if _unmet:
                _leg_out["unmet_activities"] = _unmet
            out_legs.append(_leg_out)
    return {"legs": out_legs}


def narration_corpus(payload: dict) -> dict[int, str]:
    """id -> real name, across every attraction + meal in the payload. The grounding set: a narrative
    may surface ONLY these ids/names."""
    corpus: dict[int, str] = {}
    for leg in payload.get("legs", []):
        for day in leg.get("days", []):
            for item in list(day.get("attractions", [])) + list(day.get("meals", [])):
                iid = item.get("id")
                nm = item.get("name")
                if isinstance(iid, int) and nm:
                    corpus[iid] = nm
    return corpus


def _norm_tokens(s: str) -> set[str]:
    return {t for t in re.sub(r"[^a-z0-9 ]", " ", (s or "").lower()).split() if len(t) >= 3}


def _name_matches(emitted: str, real: str) -> bool:
    """True if `emitted` plausibly refers to `real` (anti id-swap guard): at least one shared
    significant token. Real names with no significant token accept on the id alone."""
    r = _norm_tokens(real)
    if not r:
        return True
    e = _norm_tokens(emitted)
    return bool(e & r)


def validate_narrative(narrative: dict | None, corpus: dict[int, str]) -> dict:
    """Return a cleaned COPY of the LLM narrative with every un-grounded highlight/dining item dropped
    (id not in corpus, or name grossly mismatches the real name). Surviving items are canonicalised to
    the real name. Adds `_validation` stats. Never raises on a malformed narrative."""
    out = copy.deepcopy(narrative) if isinstance(narrative, dict) else {}
    dropped, kept_n = 0, 0
    for leg in (out.get("legs") or []):
        if not isinstance(leg, dict):
            continue
        for day in (leg.get("days") or []):
            if not isinstance(day, dict):
                continue
            for key in ("highlights", "dining"):
                kept: list[dict] = []
                for item in (day.get(key) or []):
                    if not isinstance(item, dict):
                        dropped += 1
                        continue
                    # id-coerced lookup (tolerant of a string id); the token-overlap gate only
                    # governs SURVIVAL — the surfaced name is ALWAYS overwritten to the real
                    # corpus name below, so a wrong-name-on-real-id item can never SHOW a fake name.
                    real = corpus.get(_as_int(item.get("id")))
                    if real is not None and _name_matches(str(item.get("name", "")), real):
                        item["name"] = real  # canonicalise to the deterministic name
                        kept.append(item)
                        kept_n += 1
                    else:
                        dropped += 1
                day[key] = kept
    out["_validation"] = {"dropped": dropped, "kept": kept_n, "corpus_size": len(corpus)}
    return out
