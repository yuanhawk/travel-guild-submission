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

# PUBLIC-EXPORT NOTE: this is a simplified stand-in for the prompt actually used in
# production. The real one is iteratively tuned against a private evaluation corpus
# (the full honesty guardrail around dates/categories/unmet_activities, per-field
# style guidance, worked phrasing examples, etc.) — that tuning is the product's
# work, not something this showcase repo hands out verbatim. This version keeps
# the JSON contract the rest of the pipeline depends on (narrate()'s parsing above,
# and validate_narrative downstream, which both key off id/name and the
# overview/legs/days/highlights/dining shape) so the code still runs end-to-end,
# but the actual wording here is intentionally unrefined.
_NARRATOR_SYSTEM_PROMPT = """You are a travel writer for The Travel Guild. You are given a \
traveller's booked itinerary as structured JSON: legs (cities), each with days, each day listing \
attractions and meals — every place has an integer "id" and a "name".

Write a short, friendly narrative for the itinerary. Only mention places that appear in the input, \
using their exact "id" and "name" — never invent a place, and never state a fact (history, price, \
date, season, etc.) that isn't given to you.

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
