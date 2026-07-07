"""itinerary_narrator.py — #3 Phase 3: the LLM narrator seam (DashScope qwen3.x).

`narrate(payload)` takes the GROUNDED day-by-day payload (utils.itinerary_narration.build_narration_
payload — real attractions/restaurants only, each with a stable id) and returns a structured day-by-
day narrative, or None on ANY failure (missing key / network / bad JSON) so the orchestrator hook
degrades to no-narrative. The output is ALWAYS re-validated against the deterministic corpus by
validate_narrative before anything is surfaced — so even if the model disobeys and invents a place,
the honesty firewall drops it at the chip level. This module only asks; it never decides what shows.

Cost/latency: one extra call per booked trip, fuzzy-mode + opt-in only. enable_thinking:false (no
reasoning tokens) + a compact grounded payload keep it cheap; 30s timeout, degrade to None.
"""

from __future__ import annotations

import json
import logging
import os
import re
from typing import Any

import httpx

logger = logging.getLogger(__name__)

DASHSCOPE_API_KEY = os.environ.get("DASHSCOPE_API_KEY", "")
DASHSCOPE_BASE_URL = "https://dashscope-intl.aliyuncs.com/compatible-mode/v1"
MODEL = os.environ.get("SOCIETY_NARRATOR_MODEL", "qwen-turbo")  # fast model for the COSMETIC layer:
# qwen-turbo ~14s for the richer narrative vs qwen3-max ~60s (which blew the timeout); quality is
# ample for warm grounded travel prose, and the honesty firewall + audited prompt do the heavy lifting.

_NARRATOR_SYSTEM_PROMPT = """You are a warm, evocative travel writer for The Travel Guild. You are \
given a traveller's ALREADY-BOOKED itinerary as structured JSON: legs (cities), each with days, each \
day listing attractions and meals — every place has a stable integer "id" and an exact "name". A leg \
may also carry "interests" (what the traveller asked for) and "unmet_activities" (things we could not \
find verified places for).

Write a polished, inviting itinerary the traveller will love to read — rich and atmospheric, yet \
scrupulously honest.

HONESTY — NON-NEGOTIABLE. Your prose is read by a human, NOT auto-filtered, so YOU are the guardrail:
- You may ONLY name places that appear in the input, each by its EXACT "id" and "name". NEVER invent \
or add any attraction, restaurant, hotel, neighbourhood, or place not in the input.
- Each attraction carries a "category" and each meal a "cuisine" (e.g. "tourism=museum", "french"). \
You MAY name what KIND of place it is from those fields — a museum, a cinema, a French patisserie — \
and that is the ONLY hard fact you may state about a place.
- Beyond category/cuisine, NEVER assert a fact that is not in the input — no history, founding dates, \
"famous for", "oldest/largest", UNESCO, michelin stars, opening hours, prices, distances, crowd \
levels, or weather. You have NO knowledge of these places beyond what the input gives you. Write the \
EXPERIENCE and the FLOW, never facts you cannot see.
- NEVER state a specific calendar month, date, day-of-week, or season anywhere in your prose — not \
even one derived from "checkin"/"checkout". Those fields exist for ordering only. You are not a \
reliable calendar and a wrong guess here is exactly the kind of invented fact this firewall cannot \
catch (it only validates place names, never dates). Speak only in relative/relational terms: "day \
two", "your first morning", "as your journey unfolds" — never "in April" or "this autumn".
- Evoke mood, rhythm and the shape of the day (the arc from morning to evening, how the chosen places \
string together, how they serve the traveller's stated interests) — warmth and anticipation, grounded \
only in the given names. If a day has few places, write less; never pad.
- Weave in the leg's "interests" where the listed places naturally serve them. If "unmet_activities" \
is present, acknowledge it once, gracefully and honestly (e.g. "we couldn't verify a dedicated \
ice-cave outing here") — NEVER invent a place to cover it.

STYLE:
- "overview": 2-3 warm sentences introducing the WHOLE journey — its arc and feeling, naming the \
cities, inviting the traveller in.
- each leg "summary": 2-3 sentences setting the scene for that city and how its days unfold.
- each day "title": a short, evocative label drawn only from that day's places/interests \
(e.g. "Temple Mornings & Riverside Evenings").
- each day "narrative": 2-4 flowing sentences guiding the traveller through the day's places in order, \
connecting them with feeling and pace — no invented facts.
- each highlight/dining "blurb": one vivid but grounded line — the place's role in the day (a slow \
morning, an evening table), never an invented fact about it.

Output ONLY this JSON object (nothing else):
{"overview":"...",
 "legs":[{"leg_id":"...","city":"...","summary":"...",
   "days":[{"day_number":1,"title":"...","narrative":"...",
     "highlights":[{"id":0,"name":"...","blurb":"..."}],
     "dining":[{"id":0,"name":"...","blurb":"..."}]}]}]}
Every highlight/dining item MUST use the exact id+name from the input."""


def narrate(payload: dict[str, Any]) -> dict | None:
    """Call the LLM to write a grounded day-by-day narrative for `payload`. Returns the parsed
    narrative dict, or None on any failure (caller degrades to no-narrative). NEVER raises."""
    if not DASHSCOPE_API_KEY:
        return None

    # Denial-of-wallet breaker (default OFF; utils/cost_breaker.py). The narrative is
    # a SECOND paid LLM call — count it against the same daily LLM cap. When tripped
    # (cap reached or kill-switch set), degrade to no-narrative — the exact existing
    # graceful fallback (returning None), never a paid call. Disabled → no-op.
    try:
        from utils.cost_breaker import get_breaker
    except ImportError:
        from cost_breaker import get_breaker  # type: ignore[no-redef]
    if not get_breaker().allow("llm"):
        return None

    try:
        from utils.model_router import dashscope_chat
    except ImportError:
        from model_router import dashscope_chat  # type: ignore[no-redef]

    # SCALE the cap to the trip size: each day contributes a title + narrative + ~10 chips-with-blurbs,
    # so a FIXED cap truncates big itineraries (a 5-6 leg RTW ≈ 18 days overran 4000 → finish_reason=
    # length → broken JSON → silent None). max_tokens is a CAP not a target — small trips still stop
    # early (finish_reason=stop), so scaling only HELPS big trips. SOCIETY_NARRATOR_MAX_TOKENS overrides.
    _total_days = sum(len(leg.get("days") or []) for leg in (payload.get("legs") or [])
                      if isinstance(leg, dict))
    _scaled = max(2500, min(9000, 1200 + 400 * _total_days))
    max_tokens = int(os.environ.get("SOCIETY_NARRATOR_MAX_TOKENS") or _scaled)
    body = {
        "enable_thinking": False,  # qwen3.x reasoning models: skip thinking → fast, cheap narrative
        "messages": [
            {"role": "system", "content": _NARRATOR_SYSTEM_PROMPT},
            {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
        ],
        "response_format": {"type": "json_object"},
        "max_tokens": max_tokens,
    }
    logger.info("narrator: max_tokens=%s total_days=%s", max_tokens, _total_days)
    try:
        data = dashscope_chat("narrator", body, timeout=90.0)
        msg = (data.get("choices") or [{}])[0].get("message") or {}
        content = msg.get("content")
        if not isinstance(content, str) or not content.strip():
            finish = (data.get("choices") or [{}])[0].get("finish_reason")
            logger.warning(
                "narrator: empty content (finish_reason=%s, had_reasoning=%s)",
                finish, bool(msg.get("reasoning_content")),
            )
            return None  # honest no-narrative — never surface a stub
        # Strip markdown fences if present (mirrors accommodation_agent._llm_rank_hotels)
        text = content.strip()
        text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.MULTILINE)
        text = re.sub(r"\s*```\s*$", "", text, flags=re.MULTILINE)
        text = text.strip()
        out = json.loads(text)
        return out if isinstance(out, dict) else None
    except Exception:
        return None  # missing key / network / bad JSON → no narrative (honest, never fabricated)
