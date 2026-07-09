"""followup_parser.py — B3: parse conversational follow-ups into structured deltas + apply them.

`parse_followup(message, trip_request, turns)` calls the LLM to map NL → a bounded
delta object (closed op-set); all hard numbers from the LLM are clamped / validated
before use — this is the FUZZY FRONT only, exactly like intent_parser._llm_call and
the narrator. No fabrication can reach the deterministic core.

`apply_delta(trip_request, delta)` is a PURE FUNCTION — deepcopy, mutate, return
(new_request, changed_list). The result is then passed to orch.negotiate(commit=False)
which is the unchanged byte-identical deterministic core.

`compute_refine_diff(old_request, new_request, ops)` (B3) is a PURE FUNCTION over
the two structured trip_requests — a machine-readable diff (legs added/removed,
per-leg + total nights delta, budget delta) with a `side_effect` flag per field
marking changes NOT directly named by an op in `ops`. Returned to the frontend
under /refine's "diff" response key so it can flag unrequested side effects (e.g.
a city swap that also silently shrinks total trip length) without parsing prose.

`build_domain_answer(domain, envelope)` (B5) is a THIRD /refine response mode —
"answer" — for a follow-up that is a QUESTION about health/fraud/insurance/
compliance rather than a request to change the trip (e.g. "is this covered if I
get sick", "is my payment safe", "what's my insurance situation"). Before B5
those questions had ZERO path to any op in the closed set above and dead-ended in
refine_unsupported, even though the underlying domain agent already ran (and
produced a real verdict) during the trip's INITIAL planning pass. parse_followup's
LLM classification step now ALSO detects this case (the SAME LLM call already
made for op-classification — no extra latency) and returns "question_domain".
build_domain_answer is a PURE FORMATTER over the verdict ALREADY STORED on the
held plan's envelope (health_verdict / compliance_verdict / fraud_verdict /
insurance — the exact dicts orchestrator.py's negotiate() attached during the
original planning pass) — it makes NO new agent call and NEVER re-enters
orch.negotiate(), which is what makes this genuinely fast (a dict lookup + a
pure-Python formatter) instead of the 100s+ full-replan anti-pattern a
question previously had to go through to reach ANY domain-agent answer at all.
If the relevant domain never fired for this trip (e.g. no counterparties → no
fraud verdict), it says so honestly instead of fabricating a verdict.

var-0 argument: parse_followup is off the digest path (it's called only by /refine);
apply_delta never touches negotiate/_request_digest; compute_refine_diff runs AFTER
negotiate and never feeds back into it; build_domain_answer never touches
negotiate/_request_digest either (pure read of an already-persisted envelope, no
apply_delta, no re-plan); the conversation table is a side-effect written after
negotiate completes. Anonymous /negotiate_text path is entirely untouched.
"""
from __future__ import annotations

import copy
import json
import logging
import os
import re
from datetime import date, timedelta
from typing import Any

import httpx

from utils.intent_parser import MAX_LEGS, _MAX_DATE_RANGE_NIGHTS

logger = logging.getLogger(__name__)

DASHSCOPE_API_KEY = os.environ.get("DASHSCOPE_API_KEY", "")
DASHSCOPE_BASE_URL = "https://dashscope-intl.aliyuncs.com/compatible-mode/v1"
# Use the same fast model as the intent parser, defaulting to qwen-turbo for refine.
_MODEL = os.environ.get("SOCIETY_REFINE_MODEL", os.environ.get("SOCIETY_LLM_MODEL", "qwen-turbo"))

# How much to adjust budget when the user says "cheaper/cheaper" without a percentage.
REFINE_STEP_PCT = 0.15

# #116: prefix apply_delta uses for a `changed` entry that reports an op it could
# NOT recognise at all (unknown op kind, or a malformed non-dict entry) — as
# opposed to every other `changed` entry, which reports an op it DID recognise
# (whether applied or declined for a validation reason, e.g. "not found —
# skipped"). server.py's total-failure safety net filters these out to tell
# "recognised nothing in this delta" (decline the whole turn, unchanged) apart
# from "recognised at least one op, one of the others was garbage" (apply what
# validated, report the rest as skipped) — see apply_delta's docstring.
UNSUPPORTED_OP_PREFIX = "unsupported_op:"

# PUBLIC-EXPORT NOTE: this is a simplified stand-in for the prompt actually used in
# production. The real one is iteratively tuned against a private evaluation corpus
# (disambiguation heuristics for direction/currency/nationality inference, the exact
# QUESTION MODE topic-boundary wording, etc.) — that tuning is the product's work,
# not something this showcase repo hands out verbatim. This version keeps the JSON
# contract the rest of the pipeline depends on (apply_delta's dispatch below, and
# server.py's question_domain routing) so the code still runs end-to-end, but the
# actual wording here is intentionally unrefined.
_FOLLOWUP_SYSTEM_PROMPT = """You are the Travel Guild's trip-edit assistant.
The user has an EXISTING trip plan and wants to change something, or is asking a
question about it. Respond with ONLY a JSON object, one of two shapes:

1. A change request — {"ops": [...]} using ops from this closed set:
  {"op":"budget_set",        "amount_usd": <number>}
  {"op":"budget_adjust",     "direction":"cheaper"|"higher", "pct": <0.0-1.0>|null}
  {"op":"add_leg",           "city":"<city>", "vibe":"city"|"beach"|"nature"|null, "position":"end"|"after:<city>"}
  {"op":"remove_leg",        "city":"<city>"}
  {"op":"set_nights",        "total_nights": <integer>}
  {"op":"adjust_nights",     "delta_nights": <integer (positive=add, negative=remove)>}
  {"op":"set_start_date",    "date":"YYYY-MM-DD"}
  {"op":"add_interest",      "interest":"<token>"}
  {"op":"remove_interest",   "interest":"<token>"}
  {"op":"swap_item",         "leg_city":"<city>", "remove_name":"<place name>", "kind":"dining"|"attraction"}
  {"op":"home_currency_set", "currency":"<3-letter ISO code>"}
  {"op":"nationality_set",   "iso2":"<2-letter country ISO code>"}
  If nothing maps, set "unsupported":true with a brief "reason".

2. A question about the trip's health/fraud/insurance/compliance situation — set
  "question_domain" to one of "health"|"fraud"|"insurance"|"compliance", leave
  "ops" empty. If it doesn't clearly fit one of those, fall back to unsupported.

Output format:
{"ops":[...], "unsupported":false|true, "reason":null|"<string>",
 "question_domain":null|"health"|"fraud"|"insurance"|"compliance"}
"""

# Closed set of question_domain values the LLM may emit — anything else clamps to
# None (fail-closed, mirrors the "ops" closed-set guard in apply_delta below).
_QUESTION_DOMAINS = frozenset({"health", "fraud", "insurance", "compliance"})


def _clamp_question_domain(raw: Any) -> str | None:
    """Closed-set clamp for the LLM-emitted question_domain (var-0: never trust
    free LLM text past this set — an unrecognised value degrades to None, i.e.
    'not a domain question', never a guessed/fabricated domain)."""
    if isinstance(raw, str) and raw.strip().lower() in _QUESTION_DOMAINS:
        return raw.strip().lower()
    return None

# Patterns that are pure informational queries — answer from context without re-planning.
_INFO_QUERY_RE = re.compile(
    r"^\s*(?:what'?s?|what is|how much|show me|give me)\s+(?:the\s+)?"
    r"(?:budget|cost|price|total|itinerary|plan|nights?|days?)\b",
    re.I,
)


def _info_query_reply(message: str, trip_request: dict) -> str | None:
    """If the message is a pure informational query, return a direct answer. Else None."""
    if not _INFO_QUERY_RE.match(message.strip()):
        return None
    legs = trip_request.get("legs") or []
    budget_usd = (trip_request.get("total_budget_cents") or 0) / 100
    cities = [leg.get("city", "?") for leg in legs if isinstance(leg, dict)]
    total_nights = sum(
        (leg.get("nights") or _nights_from_dates(leg.get("checkin"), leg.get("checkout")) or 0)
        for leg in legs if isinstance(leg, dict)
    )
    currency = trip_request.get("home_currency") or "USD"
    return (
        f"Current plan: {', '.join(cities) or '(unknown)'}, {int(total_nights)} nights, "
        f"${budget_usd:,.0f} budget (displayed in {currency}). "
        f"To change the budget, say e.g. 'make it cheaper' or 'set budget to $2000'."
    )


def parse_followup(
    message: str,
    trip_request: dict,
    prior_turns: list[dict],
) -> dict:
    """Call the LLM to parse a follow-up NL message → bounded delta ops.

    Never raises. Returns a dict with keys: ops (list), unsupported (bool), reason (str|None),
    question_domain (str|None — B5, one of health/fraud/insurance/compliance when the
    message is a QUESTION about that topic rather than a change request).
    On LLM failure → returns unsupported=True with an honest reason.
    On info-query → returns unsupported=False, ops=[], query_answer=<string> (no LLM call).
    """
    # #116: fast-path for informational queries — answer from context, no LLM, no re-plan.
    info_reply = _info_query_reply(message, trip_request)
    if info_reply is not None:
        return {"ops": [], "unsupported": False, "reason": None, "query_answer": info_reply,
                "question_domain": None}

    _empty = {"ops": [], "unsupported": True, "reason": "LLM unavailable", "question_domain": None}
    if not DASHSCOPE_API_KEY:
        return _empty

    # Build a compact context for the LLM: current cities + budget.
    legs = trip_request.get("legs") or []
    cities = [leg.get("city", "?") for leg in legs if isinstance(leg, dict)]
    budget_usd = (trip_request.get("total_budget_cents") or 0) / 100
    total_nights = sum(
        (leg.get("nights") or
         _nights_from_dates(leg.get("checkin"), leg.get("checkout")) or 0)
        for leg in legs if isinstance(leg, dict)
    )
    context = (
        f"Current plan: {', '.join(cities) or '(unknown)'}; "
        f"{int(total_nights)} nights; ${budget_usd:.0f} budget."
    )

    # Include the last 4 assistant+user turns as context (enough without ballooning the prompt).
    recent = []
    for t in (prior_turns or [])[-4:]:
        role = t.get("role", "user")
        content = t.get("content", "")
        if role in ("user", "assistant") and content:
            recent.append({"role": role, "content": content})

    messages = [
        {"role": "system", "content": _FOLLOWUP_SYSTEM_PROMPT},
        {"role": "user", "content": f"{context}\n\nUser request: {message}"},
    ]
    # Inject recent turns between system and user if present.
    if recent:
        messages = [messages[0]] + recent + [messages[1]]

    try:
        from utils.model_router import dashscope_chat
    except ImportError:
        from model_router import dashscope_chat  # type: ignore[no-redef]

    body = {
        "enable_thinking": False,
        "messages": messages,
        "response_format": {"type": "json_object"},
        "max_tokens": 512,
    }
    try:
        data = dashscope_chat("refine", body, timeout=30.0)
        msg = (data.get("choices") or [{}])[0].get("message") or {}
        content = msg.get("content")
        if not isinstance(content, str) or not content.strip():
            logger.warning("followup_parser: empty LLM content")
            return {**_empty, "reason": "LLM returned empty response"}
        text = content.strip()
        text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.MULTILINE)
        text = re.sub(r"\s*```\s*$", "", text, flags=re.MULTILINE)
        parsed = json.loads(text.strip())
        if not isinstance(parsed, dict):
            return {**_empty, "reason": "LLM returned non-object"}
        # Normalise: ensure expected fields are present.
        ops = parsed.get("ops") or []
        # B5: closed-set clamp on question_domain (var-0 — never trust free LLM
        # text past this set). Mutual-exclusion is ENFORCED here, not just
        # requested in the prompt: a genuine question carries no ops, so if the
        # LLM (against instructions) emits both, non-empty ops win — a request
        # to CHANGE the trip must never be silently swallowed into a
        # question-answer that ignores it.
        question_domain = _clamp_question_domain(parsed.get("question_domain")) if not ops else None
        return {
            "ops": ops,
            "unsupported": bool(parsed.get("unsupported", False)),
            "reason": parsed.get("reason"),
            "question_domain": question_domain,
        }
    except Exception as exc:  # noqa: BLE001
        logger.warning("followup_parser: LLM call failed: %s", exc)
        return {**_empty, "reason": f"parse error: {exc}"}


def _nights_from_dates(checkin: str | None, checkout: str | None) -> int:
    """Compute nights between two ISO-date strings. Returns 0 on any failure."""
    try:
        ci = date.fromisoformat(checkin or "")
        co = date.fromisoformat(checkout or "")
        return max(0, (co - ci).days)
    except Exception:
        return 0


def _total_nights_req(trip_request: dict) -> int:
    """Sum per-leg nights from a trip_request."""
    legs = trip_request.get("legs") or []
    total = 0
    for leg in legs:
        if not isinstance(leg, dict):
            continue
        n = leg.get("nights") or _nights_from_dates(leg.get("checkin"), leg.get("checkout"))
        total += n or 0
    return total


def _redistribute_dates(legs: list[dict], start_date: str | None = None) -> bool:
    """Recompute contiguous checkin/checkout for all legs in-place, preserving leg nights.

    Returns True when the DEFAULT_START_DATE fallback anchor had to be used because
    NEITHER an explicit `start_date` was supplied NOR did `legs[0]` already carry a
    checkin (#honesty-fix GAP 4 companion: callers must stamp `assumed_start_date` on the
    trip_request when this fires — mirrors intent_parser._clamp_and_validate's same-named
    flag, so the disclosure survives a /refine edit that hits this fallback).
    """
    from utils.intent_parser import DEFAULT_START_DATE
    if not legs:
        return False
    # Prefer explicit `nights` field; fall back to date diff.
    nights_list = []
    for leg in legs:
        n = leg.get("nights") or _nights_from_dates(leg.get("checkin"), leg.get("checkout"))
        nights_list.append(max(1, n or 1))

    # Get or derive start_date from the first leg; fall back to the SAME sentinel
    # DEFAULT_START_DATE intent_parser.py uses elsewhere (a distinct hardcoded date here
    # would be a second, inconsistent fabricated anchor — #honesty-fix GAP 4). Treat any
    # FALSY start_date (None OR "") as "derive it" — a caller passing along a leg's
    # already-empty checkin string (e.g. a brand-new leg with no dates yet) must hit the
    # same fallback as passing None, not crash on `date.fromisoformat("")`.
    used_fallback = False
    if not start_date:
        start_date = (legs[0].get("checkin") if legs else None) or ""
        if not start_date:
            start_date = DEFAULT_START_DATE
            used_fallback = True

    cursor = date.fromisoformat(start_date)
    for leg, nights in zip(legs, nights_list):
        leg["nights"] = nights
        leg["checkin"] = cursor.isoformat()
        cursor += timedelta(days=nights)
        leg["checkout"] = cursor.isoformat()
    return used_fallback


def _stamp_assumed_start_date(req: dict) -> None:
    """Stamp the `assumed_start_date` honesty flag on `req` (#honesty-fix GAP 4
    companion) after a `_redistribute_dates` call reports it had to fall back to
    DEFAULT_START_DATE. Mirrors intent_parser._clamp_and_validate's same-named flag so
    /refine's `attach_assumption_notes` picks it up on the next re-plan."""
    from utils.intent_parser import DEFAULT_START_DATE
    req["assumed_start_date"] = DEFAULT_START_DATE


# LAUNCH BLOCKER fix (live QA, 2026-07-08): the closed op-set the LLM is
# instructed to emit is {"op":"remove_leg","city":"lisbon"}, {"op":"add_leg",
# "city":"Porto","position":"end"}, etc — but qwen-turbo (the fast model used
# for /refine parses) occasionally collapses that into SHORTHAND where the
# op-name IS the dict key instead of an "op" field, e.g. {"remove_leg":
# "lisbon"} / {"add_leg":{"city":"Porto","position":"end"}}. apply_delta's
# dispatch below keys off `op.get("op")`, which is None for shorthand — so
# EVERY op in a shorthand delta fell through to the "unknown op" branch
# (silently dropped, nothing appended to `changed`, no error). A live "swap
# Lisbon for Porto" on a single-leg trip reproduced this exactly: delta_applied
# echoed the shorthand ops, changed==[], and plan.legs stayed Lisbon-only —
# while /refine's success gate (server.py) has no notion of "recognised the
# ops but they did nothing", so it still returned outcome=plan_ready with a
# confident-sounding assistant_reply. This is a GENERAL failure mode across
# the whole closed op-set, not specific to remove_leg/add_leg.
#
# _OP_SHORTHAND_FIELD maps each closed-set op kind to the canonical field its
# shorthand value should be assigned to when the value is a bare scalar
# (str/number). None means the value is itself expected to be the fields dict
# (add_leg/budget_adjust/swap_item all pass extra keys), so it's splatted in
# directly instead of assigned to a single field.
_OP_SHORTHAND_FIELD: dict[str, str | None] = {
    "budget_set": "amount_usd",
    "budget_adjust": None,
    "add_leg": None,
    "remove_leg": "city",
    "set_nights": "total_nights",
    "adjust_nights": "delta_nights",
    "set_start_date": "date",
    "add_interest": "interest",
    "remove_interest": "interest",
    "swap_item": None,
    "home_currency_set": "currency",
    "nationality_set": "iso2",
}


def _normalize_op(op: Any) -> Any:
    """Canonicalise a single delta op into {"op": <kind>, ...fields} shape.

    Already-canonical ops (carrying an "op" key) pass through untouched. A
    recognised SHORTHAND op — a single-key dict whose key is one of the closed
    op kinds — is rewritten to canonical shape (see module comment above for
    why this exists). Anything else (not a dict, multi-key with no "op", or a
    key outside the closed set) is returned unchanged so the normal
    "unknown op -> dropped, logged" path still applies — this function only
    RESCUES a known shape, it never invents new tolerance for garbage.
    """
    if not isinstance(op, dict) or "op" in op or len(op) != 1:
        return op
    (key, value), = op.items()
    if key not in _OP_SHORTHAND_FIELD:
        return op
    field = _OP_SHORTHAND_FIELD[key]
    if field is None:
        # Value must itself be the fields dict (add_leg/budget_adjust/swap_item).
        return {"op": key, **value} if isinstance(value, dict) else op
    return {"op": key, field: value}


def normalize_ops(ops: Any) -> list:
    """Canonicalise every op in a delta's `ops` list (see `_normalize_op`).

    Public entry point used by server.py's /refine handler so that the SAME
    canonical ops shape is seen by apply_delta, compute_refine_diff, the
    has_only_unsupported gate, AND the `delta_applied` value echoed back to
    the caller — a single normalisation point, not four places independently
    guessing at op shape.
    """
    if not isinstance(ops, list):
        return []
    return [_normalize_op(op) for op in ops]


def apply_delta(
    trip_request: dict,
    delta: dict,
) -> tuple[dict, list[str]]:
    """Apply a parsed delta to a trip_request (deepcopy — pure function).

    Returns (new_request, changed) where changed is a list of human-readable
    strings describing what was actually modified (truthful, used in assistant_reply).

    NOTE: swap_item ops are silently skipped (returned in changed as 'swap_item:unsupported').
    Unknown cities in add_leg are dropped and noted in changed.

    #116 (LOW, design-robustness follow-up to the 2026-07-08 LAUNCH BLOCKER fix
    below): EVERY branch in this dispatch loop appends something to `changed`
    for every op it recognises — including pure no-op skips (e.g. "legs.remove:
    berlin (not found — skipped)") — specifically so server.py's `ops and not
    changed` safety net can tell "nothing in this delta was recognised" apart
    from "at least one op was recognised". That invariant used to have one
    hole: a genuinely UNRECOGNISED op kind (a typo'd op name, an op type
    outside the closed set, or a malformed non-dict entry) fell through to a
    `logger.debug`-only drop with NOTHING appended to `changed`. For a delta
    that mixed one valid op with one garbage op, the valid op's `changed`
    entry made the safety net see "something happened" and wave the whole
    turn through as a clean success — silently eating the part of the user's
    request tied to the garbage op with zero signal, in `changed`,
    `assistant_reply`, or `delta_applied`. Fixed by making the unrecognised-op
    and malformed-op paths append a `changed` entry using the SAME
    "kind:detail (reason — skipped)" convention every other skip in this
    function already uses, rather than inventing new all-or-nothing decline
    semantics: it keeps applying the ops it CAN validate (least astonishment —
    the valid add_leg still happens) while making the drop just as visible as
    every other skip already is.
    """
    import copy as _copy
    req = _copy.deepcopy(trip_request)
    changed: list[str] = []
    legs: list[dict] = req.get("legs") or []

    for op in (delta.get("ops") or []):
        raw_op = op
        op = _normalize_op(op)
        if not isinstance(op, dict):
            # Malformed op (not a dict, or a shape _normalize_op couldn't
            # rescue) — report it instead of silently dropping the user's
            # request for it (#116; see docstring above).
            changed.append(f"{UNSUPPORTED_OP_PREFIX}{raw_op!r} (malformed — skipped)")
            logger.debug("followup_parser: malformed op=%r — skipped", raw_op)
            continue
        kind = op.get("op")

        if kind == "budget_set":
            amount_usd = op.get("amount_usd")
            if isinstance(amount_usd, (int, float)) and amount_usd > 0:
                old_cents = req.get("total_budget_cents") or 0
                req["total_budget_cents"] = int(amount_usd * 100)
                changed.append(f"budget.set:${amount_usd:.0f} (was ${old_cents/100:.0f})")

        elif kind == "budget_adjust":
            direction = op.get("direction")
            pct = op.get("pct")
            if pct is None or not (0 < pct <= 1.0):
                pct = REFINE_STEP_PCT
            pct = min(float(pct), 0.5)  # clamp to 50%
            old_cents = req.get("total_budget_cents") or 0
            if direction == "cheaper":
                req["total_budget_cents"] = int(old_cents * (1 - pct))
                changed.append(f"budget.adjust:-{pct*100:.0f}%")
            elif direction == "higher":
                req["total_budget_cents"] = int(old_cents * (1 + pct))
                changed.append(f"budget.adjust:+{pct*100:.0f}%")

        elif kind == "add_leg":
            raw_city = op.get("city")
            city = _resolve_city(raw_city)
            if not city:
                changed.append(f"add_leg:{raw_city} (not bookable — skipped)")
                continue
            # UX-audit 2026-07 (H2): the ORIGINAL parse path honestly declines when a
            # request names more than MAX_LEGS cities (intent_parser.py) -- this delta-
            # application path had no equivalent check, so a /refine conversation could
            # grow trip.legs past that deliberate cap with zero validation (a guardrail
            # bypass, not just a missed edge case: the same trip is unplannable/
            # unbookable either way, refine just let it happen silently instead of
            # honestly declining like turn 1 would have).
            if len(legs) >= MAX_LEGS:
                changed.append(
                    f"add_leg:{city} (skipped — already at the {MAX_LEGS}-city limit; "
                    f"split into a separate trip)"
                )
                continue
            position = op.get("position") or "end"
            vibe = op.get("vibe")
            # Capture the ORIGINAL first leg's checkin BEFORE the append/insert below —
            # once the new (dateless) leg is spliced in, `legs[0]` may BE that new leg
            # (when `legs` started empty), so re-deriving `legs[0].get("checkin")` AFTER
            # mutation would read "" instead of a real anchor and crash
            # `_redistribute_dates` (a pre-existing latent bug, surfaced while fixing
            # GAP 4's assumed_start_date propagation here).
            _orig_first_checkin = legs[0].get("checkin") if legs else None
            # Compute default nights: round(total/new_count); borrow from longest if needed.
            total_n = _total_nights_req(req) or 7
            new_count = len(legs) + 1
            default_nights = max(1, round(total_n / new_count))
            # #88/#115: the 180-night cap (_MAX_DATE_RANGE_NIGHTS) is enforced at initial
            # plan() time (intent_parser._scan_nights_over_cap honestly declines an
            # over-cap free-text duration) but was NEVER re-checked here — each add_leg
            # only ever grew total nights (existing legs keep their nights unchanged;
            # the new leg ADDS default_nights on top, it doesn't redistribute), so a
            # /refine conversation could add legs one at a time and walk the cumulative
            # total straight past 180 nights with zero validation, exactly the same
            # guardrail-bypass shape as the MAX_LEGS check above (task #63). Re-validate
            # the CUMULATIVE total across ALL legs (existing + this one) before splicing
            # the new leg in, and honestly decline — matching the MAX_LEGS decline
            # pattern — instead of silently exceeding the cap one refine turn at a time.
            _prospective_total = total_n + default_nights
            if _prospective_total > _MAX_DATE_RANGE_NIGHTS:
                changed.append(
                    f"add_leg:{city} (skipped — would bring the trip to "
                    f"{_prospective_total} total nights, over the "
                    f"{_MAX_DATE_RANGE_NIGHTS}-night cap; split into a separate trip)"
                )
                continue
            # #honesty-fix (GAP 4 companion): inherit adults from the first leg if one
            # exists, else from the top-level trip_request's own adults field, else fall
            # back to 1 — the SAME conservative default the rest of this codebase uses
            # (intent_parser._scan_adults / _clamp_and_validate), instead of a bespoke,
            # undocumented "2" that silently priced an extra traveler with no signal.
            new_leg: dict[str, Any] = {
                "city": city,
                "place_key": city,
                "adults": (legs[0].get("adults") if legs else None) or req.get("adults") or 1,
                "nights": default_nights,
                "checkin": "",   # will be set by _redistribute_dates
                "checkout": "",
            }
            if vibe:
                new_leg["vibe"] = vibe
            # Copy interests from first leg (or empty).
            if legs and legs[0].get("interests"):
                new_leg["interests"] = list(legs[0]["interests"])

            if position == "end" or not position.startswith("after:"):
                legs.append(new_leg)
            else:
                ref_city = position[len("after:"):]
                idx = next(
                    (i for i, l in enumerate(legs) if isinstance(l, dict) and
                     l.get("city", "").lower() == ref_city.lower()),
                    len(legs) - 1,
                )
                legs.insert(idx + 1, new_leg)

            req["legs"] = legs
            if _redistribute_dates(legs, start_date=_orig_first_checkin):
                _stamp_assumed_start_date(req)
            changed.append(f"legs.add:{city}")

        elif kind == "remove_leg":
            raw_city = op.get("city") or ""
            before = len(legs)
            legs = [l for l in legs if not (isinstance(l, dict) and
                    l.get("city", "").lower() == raw_city.lower())]
            if len(legs) < before:
                req["legs"] = legs
                if _redistribute_dates(legs, start_date=legs[0].get("checkin") if legs else None):
                    _stamp_assumed_start_date(req)
                changed.append(f"legs.remove:{raw_city}")
            else:
                changed.append(f"legs.remove:{raw_city} (not found — skipped)")

        elif kind == "set_nights":
            total_nights = op.get("total_nights")
            if isinstance(total_nights, int) and 1 <= total_nights <= 60:
                from utils.intent_parser import _split_nights_evenly
                leg_count = len(legs)
                if leg_count > 0:
                    split = _split_nights_evenly(total_nights, leg_count)
                    for leg, n in zip(legs, split):
                        if isinstance(leg, dict):
                            leg["nights"] = n
                    req["legs"] = legs
                    if _redistribute_dates(legs, start_date=legs[0].get("checkin") if legs else None):
                        _stamp_assumed_start_date(req)
                    changed.append(f"nights.set:{total_nights}")

        elif kind == "adjust_nights":
            delta_n = op.get("delta_nights")
            if isinstance(delta_n, int) and delta_n != 0:
                current = _total_nights_req(req)
                new_total = max(1, min(60, current + delta_n))
                from utils.intent_parser import _split_nights_evenly
                leg_count = len(legs)
                if leg_count > 0:
                    split = _split_nights_evenly(new_total, leg_count)
                    for leg, n in zip(legs, split):
                        if isinstance(leg, dict):
                            leg["nights"] = n
                    req["legs"] = legs
                    if _redistribute_dates(legs, start_date=legs[0].get("checkin") if legs else None):
                        _stamp_assumed_start_date(req)
                    sign = "+" if delta_n > 0 else ""
                    changed.append(f"nights.adjust:{sign}{delta_n} (total {new_total})")

        elif kind == "set_start_date":
            raw_date = op.get("date")
            try:
                d = date.fromisoformat(raw_date or "")
                _redistribute_dates(legs, start_date=d.isoformat())
                req["legs"] = legs
                # #honesty-fix (GAP 4 companion): the user just supplied a REAL date via
                # this follow-up — clear any assumed-date provenance flags left over from
                # the original parse (else the honesty notes would keep firing on data
                # the user has since corrected). None is the same "not assumed" sentinel
                # intent_parser._clamp_and_validate uses.
                req["assumed_start_date"] = None
                req["assumed_date_year"] = None
                changed.append(f"start_date.set:{d.isoformat()}")
            except Exception:
                changed.append(f"set_start_date:{raw_date} (invalid — skipped)")

        elif kind == "add_interest":
            interest = (op.get("interest") or "").strip().lower()
            if interest:
                for leg in legs:
                    if isinstance(leg, dict):
                        existing = list(leg.get("interests") or [])
                        if interest not in existing:
                            existing.append(interest)
                            leg["interests"] = sorted(existing)
                req["legs"] = legs
                changed.append(f"interest.add:{interest}")

        elif kind == "remove_interest":
            interest = (op.get("interest") or "").strip().lower()
            if interest:
                for leg in legs:
                    if isinstance(leg, dict):
                        existing = list(leg.get("interests") or [])
                        leg["interests"] = [i for i in existing if i != interest]
                req["legs"] = legs
                changed.append(f"interest.remove:{interest}")

        elif kind == "home_currency_set":
            # #116: display-currency change (var-0-safe — home_currency is display-only,
            # already part of trip_request, off the deterministic digest).
            _VALID_CURRENCIES = {
                "USD","EUR","GBP","SGD","AUD","JPY","CAD","CHF","CNY","HKD",
                "THB","IDR","MYR","INR","BRL","MXN","KRW","TWD","AED","SAR",
                "NZD","ZAR","SEK","NOK","DKK","PLN","CZK","HUF","QAR","KWD",
                "OMR","JOD","BHD","ILS","TRY","VND","PHP","BDT","PKR","LKR",
            }
            currency = (op.get("currency") or "").upper().strip()
            if currency and len(currency) == 3 and currency in _VALID_CURRENCIES:
                old = req.get("home_currency")
                req["home_currency"] = currency
                changed.append(f"home_currency.set:{currency}" + (f" (was {old})" if old else ""))
            else:
                changed.append(f"home_currency.set:{currency} (unrecognised — skipped)")

        elif kind == "nationality_set":
            # #116: traveler nationality change — affects compliance/visa re-check.
            iso2 = (op.get("iso2") or "").upper().strip()
            if iso2 and len(iso2) == 2 and iso2.isalpha():
                old = req.get("nationality")
                req["nationality"] = iso2
                changed.append(f"nationality.set:{iso2}" + (f" (was {old})" if old else ""))
            else:
                changed.append(f"nationality.set:{iso2} (invalid ISO-2 — skipped)")

        elif kind == "swap_item":
            # swap_item is parsed but NOT applied in MVP.
            changed.append(f"swap_item:unsupported (leg={op.get('leg_city')}, "
                           f"place={op.get('remove_name')})")

        else:
            # Unknown op kind — outside the closed set. Report it (#116)
            # instead of dropping it silently: this is the case that made a
            # MIXED delta (one valid op + one garbage op) look like a clean
            # full success upstream, because `changed` was non-empty (from
            # the valid op) so server.py's total-failure safety net never
            # fired. Kept as a partial-apply: the ops we COULD validate still
            # take effect, and this one is now honestly flagged as skipped.
            changed.append(f"{UNSUPPORTED_OP_PREFIX}{kind!r} (not recognised — skipped)")
            logger.debug("followup_parser: unknown op=%s — skipped", kind)

    # Keep legs in sync on req.
    req["legs"] = legs
    return req, changed


def _resolve_city(raw: Any) -> str | None:
    """Attempt to normalise a city name via intent_parser. Returns None if not resolvable."""
    try:
        from utils.intent_parser import _normalise_city
        return _normalise_city(raw)
    except Exception:
        return str(raw).strip().lower().replace(" ", "-") if raw else None


def compute_refine_diff(
    old_request: dict,
    new_request: dict,
    ops: list[dict] | None,
) -> dict:
    """B3: structured, machine-readable diff between the trip_request BEFORE and
    AFTER a /refine delta was applied — a pure function over the two structured
    requests (var-0: never re-derives anything from LLM text or prose). Returned
    to the frontend under /refine's "diff" response key, ALONGSIDE the existing
    "changed" list of human-readable string labels (kept verbatim for backward
    compatibility — this does not replace it).

    Every changed field also carries a "side_effect" bool: True when that exact
    field was NOT directly named by an op in `ops` (the SAME ops list already
    returned to the caller as "delta_applied") — i.e. it moved as a knock-on
    consequence of a DIFFERENT requested change, not because the user asked for
    it. Concretely: `add_leg`/`remove_leg` only "directly name" the leg-added/
    leg-removed rows for that city; they do NOT directly name a total-nights or
    per-leg-nights change, so a city swap (remove_leg + add_leg) that also
    shrinks total trip length via the default-nights redistribution in
    apply_delta's add_leg branch gets total nights flagged side_effect=True —
    this is the exact silent-shrink case a live prod trace surfaced. Only
    `set_nights`/`adjust_nights` directly name a nights change; only
    `budget_set`/`budget_adjust` directly name a budget change.

    RESPONSE SHAPE (frontend contract — keep this comment in sync with the code):
        {
          "legs_added": [
              {"city": str, "nights": int, "side_effect": bool}, ...
          ],
          "legs_removed": [
              {"city": str, "nights": int, "side_effect": bool}, ...
          ],
          "leg_nights_changed": [
              {"city": str, "old_nights": int, "new_nights": int, "delta": int,
               "side_effect": bool}, ...
          ],
          "total_nights": {
              "old": int, "new": int, "delta": int, "side_effect": bool
          },
          "total_budget_cents": {
              "old": int, "new": int, "delta": int, "side_effect": bool
          },
        }

    `legs_added`/`legs_removed`/`leg_nights_changed` are empty lists (never
    omitted) when nothing of that kind changed. `total_nights` and
    `total_budget_cents` are ALWAYS present with delta=0/side_effect=False when
    unchanged — callers never need to guard on key existence, only on
    `delta != 0` (or check `side_effect` directly to decide whether to warn).

    Cities are matched by their NORMALISED `leg["city"]` key (the same value
    apply_delta stores/matches on) — a leg present in both old and new with an
    unchanged city counts as "matched", not add+remove, even if its nights
    changed (that lands in `leg_nights_changed` instead).
    """
    ops = ops or []
    op_kinds = {o.get("op") for o in ops if isinstance(o, dict)}

    # Mirrors apply_delta's OWN matching predicates exactly, so a city landing in
    # legs_added/legs_removed as a byproduct of a given op is guaranteed to be
    # found "directly named" here — add_leg normalises via _resolve_city before
    # storing; remove_leg matches raw_city.lower() against the stored leg's city
    # verbatim (see apply_delta above). Reusing the identical predicates (rather
    # than re-deriving a "similar" one) is what makes side_effect trustworthy.
    named_add_cities = {
        _resolve_city(o.get("city"))
        for o in ops if isinstance(o, dict) and o.get("op") == "add_leg"
    }
    named_remove_cities = {
        str(o.get("city") or "").strip().lower()
        for o in ops if isinstance(o, dict) and o.get("op") == "remove_leg"
    }
    nights_directly_named = bool({"set_nights", "adjust_nights"} & op_kinds)
    budget_directly_named = bool({"budget_set", "budget_adjust"} & op_kinds)

    def _legs_by_city(req: dict) -> dict[str, int]:
        out: dict[str, int] = {}
        for leg in (req.get("legs") or []):
            if not isinstance(leg, dict):
                continue
            city = leg.get("city")
            if not city:
                continue
            out[city] = leg.get("nights") or _nights_from_dates(leg.get("checkin"), leg.get("checkout")) or 0
        return out

    old_legs = _legs_by_city(old_request)
    new_legs = _legs_by_city(new_request)

    legs_added = [
        {"city": city, "nights": new_legs[city], "side_effect": city not in named_add_cities}
        for city in new_legs if city not in old_legs
    ]
    legs_removed = [
        {"city": city, "nights": old_legs[city], "side_effect": city.lower() not in named_remove_cities}
        for city in old_legs if city not in new_legs
    ]
    leg_nights_changed = [
        {
            "city": city,
            "old_nights": old_legs[city],
            "new_nights": new_legs[city],
            "delta": new_legs[city] - old_legs[city],
            "side_effect": not nights_directly_named,
        }
        for city in old_legs
        if city in new_legs and new_legs[city] != old_legs[city]
    ]

    # Total nights sum over ALL legs (via the canonical _total_nights_req helper),
    # NOT over the city-collapsed `_legs_by_city` dict above: a /refine add_leg of a
    # city already in the trip (e.g. "add a few more days in Tokyo") yields two legs
    # with the same city, which the dict collapses — summing it would UNDERCOUNT the
    # headline duration. This mirrors build_assistant_reply's own _total_nights_from_legs
    # (nights totalled per-leg; cities matched by membership) so the two honest-diff
    # surfaces agree. For the normal distinct-city case both are byte-identical.
    old_total_nights = _total_nights_req(old_request)
    new_total_nights = _total_nights_req(new_request)
    nights_delta = new_total_nights - old_total_nights

    old_budget = int(old_request.get("total_budget_cents") or 0)
    new_budget = int(new_request.get("total_budget_cents") or 0)
    budget_delta = new_budget - old_budget

    return {
        "legs_added": legs_added,
        "legs_removed": legs_removed,
        "leg_nights_changed": leg_nights_changed,
        "total_nights": {
            "old": old_total_nights,
            "new": new_total_nights,
            "delta": nights_delta,
            "side_effect": nights_delta != 0 and not nights_directly_named,
        },
        "total_budget_cents": {
            "old": old_budget,
            "new": new_budget,
            "delta": budget_delta,
            "side_effect": budget_delta != 0 and not budget_directly_named,
        },
    }


# ===========================================================================
# B5 — DOMAIN-QUESTION ANSWER MODE (health / fraud / insurance / compliance).
# ===========================================================================
# A third /refine response mode, alongside "applied a structural change" and
# "unsupported/declined": a QUESTION about the trip's safety/money situation
# gets routed to the SAME domain-agent verdict already computed for THIS trip
# during its initial planning pass (orchestrator.py's negotiate() attaches
# health_verdict / compliance_verdict / fraud_verdict / insurance onto the
# result dict, which server.py's _persist_and_sanitize_plan stores verbatim as
# the held plan's `envelope` — see that function's `"envelope": dict(result)`
# line). build_domain_answer is a PURE FORMATTER over that already-persisted
# dict: no LLM, no new agent call, no negotiate() re-entry — genuinely fast (a
# dict lookup + a deterministic formatter), unlike the pre-B5 anti-pattern of
# forcing a lookup-shaped question through the full apply_delta + re-plan
# machinery. When the relevant domain never fired for this trip (its
# CONTEXTUAL trigger condition — see orchestrator.py's _run_health_gate /
# _run_compliance_gate / _run_fraud_gate docstrings — wasn't present, e.g. no
# counterparties → no fraud verdict), it says so honestly rather than
# fabricate a verdict that was never computed (the same never-silently-drop
# bar the rest of this codebase holds).

# The four domain envelope keys this reads, and why: health_verdict /
# compliance_verdict / fraud_verdict are attached VERBATIM by orchestrator.py
# (see its `result["health_verdict"] = health_verdict` etc.) using the exact
# shape each domain agent's own `assess`/`check_eligibility`/`vet` returns, so
# each domain's own pure-formatter (`health_agent.explain`,
# `compliance_agent.explain_block`, `fraud_agent.explain`) can be called on it
# directly — reusing the SAME honest-summary code the initial plan review
# already trusts, not a second parallel formatter. `insurance` is the ONE
# exception: orchestrator.py stores a REDUCED view (premium_cents,
# excluded_perils_summary, undetermined_perils, peril_set, line_item — see
# its `result["insurance"] = {...}` block) rather than the full
# CoverageAssessment `assess_coverage()` returns, so insurance_agent's own
# `explain_exclusion` (which needs the full `per_peril` list AND a specific
# peril_class) does not fit this "general status" question shape — B5 builds
# its own small pure formatter for it below (_format_insurance_answer),
# grounded in the SAME already-computed, already-persisted fields.
_DOMAIN_ENVELOPE_KEYS: dict[str, str] = {
    "health": "health_verdict",
    "compliance": "compliance_verdict",
    "fraud": "fraud_verdict",
    "insurance": "insurance",
}

# Honest "this domain has no data for this trip" messages — one per domain,
# each naming the REAL reason that gate is contextual/didn't fire (mirrors the
# docstrings on orchestrator.py's _run_health_gate / _run_compliance_gate /
# _run_fraud_gate / _apply_insurance), never a generic "no answer available".
_DOMAIN_NOT_COMPUTED_MESSAGES: dict[str, str] = {
    "health": (
        "I don't have a health/vaccination assessment on file for this trip — "
        "that check only runs when a leg resolves to a specific destination "
        "country, and none of this trip's legs triggered it. For medical "
        "concerns, please consult official CDC/WHO travel-health guidance for "
        "your destination directly."
    ),
    "compliance": (
        "I don't have a visa/entry-eligibility check on file for this trip — "
        "that check needs your nationality, which this trip doesn't have on "
        "record. Tell me your nationality (e.g. 'I'm from Singapore') and I "
        "can check visa/entry requirements for your destinations."
    ),
    "fraud": (
        "There's no counterparty/vendor solvency check on file for this trip — "
        "that check only runs against named vendors attached to a leg, and "
        "this trip has none. I have no fraud-specific signal to report either "
        "way from what's on file."
    ),
    "insurance": (
        "There's no insurance/coverage assessment on file for this trip — that "
        "check only runs when a hazard risk (e.g. a storm or unrest advisory) "
        "was flagged for your destinations, and none was for this trip. For "
        "general travel-insurance guidance, please consult a licensed provider."
    ),
}


def _format_insurance_answer(insurance: dict[str, Any]) -> str:
    """Pure formatter over orchestrator.py's REDUCED `result["insurance"]` view
    (see the module-level comment above for why this isn't insurance_agent's
    own explain_exclusion). NO-LLM-NUMBERS: every figure below is read
    straight from the already-persisted dict, nothing is computed or guessed
    here."""
    premium_cents = insurance.get("premium_cents") or 0
    excluded = insurance.get("excluded_perils_summary") or []
    undetermined = insurance.get("undetermined_perils") or []
    excluded_names = [e.get("peril_class") for e in excluded if isinstance(e, dict) and e.get("peril_class")]
    parts = [f"This trip's insurance premium is ${premium_cents/100:,.2f}."]
    if excluded_names:
        parts.append(f"Excluded (NOT covered): {', '.join(excluded_names)}.")
    if undetermined:
        parts.append(f"Coverage status undetermined for: {', '.join(undetermined)}.")
    if not excluded_names and not undetermined:
        parts.append("No exclusions or undetermined perils are flagged for this trip's coverage.")
    return " ".join(parts)


def build_domain_answer(domain: str, envelope: dict) -> dict:
    """B5: build the "answer" mode response for a health/fraud/insurance/
    compliance QUESTION (see the module-level comment block above for the
    full design — this is a PURE FORMATTER, no LLM, no agent call, no
    negotiate() re-entry).

    `domain` MUST already be one of the closed set in `_DOMAIN_ENVELOPE_KEYS`
    (parse_followup's `_clamp_question_domain` is the only legitimate source —
    an unrecognised domain here is a caller bug, not a data problem, so this
    raises rather than silently degrading).

    `envelope` is the held plan's stored envelope (server.py's
    `row.get("envelope")`) — the exact result dict orchestrator.py's
    negotiate() produced for THIS trip's current planning pass.

    RESPONSE SHAPE (frontend contract):
        {
          "domain": "health"|"fraud"|"insurance"|"compliance",
          "headline": "<honest prose answer>",
          "grounded": bool,   # True iff a real verdict for THIS trip backs the
                               # headline; False means the domain never fired
                               # for this trip and `headline` says so honestly.
        }
    Never omits a key; `headline` is never empty.
    """
    if domain not in _DOMAIN_ENVELOPE_KEYS:
        raise ValueError(f"build_domain_answer: unknown domain {domain!r}")
    envelope = envelope if isinstance(envelope, dict) else {}
    verdict = envelope.get(_DOMAIN_ENVELOPE_KEYS[domain])
    if not isinstance(verdict, dict):
        return {
            "domain": domain,
            "headline": _DOMAIN_NOT_COMPUTED_MESSAGES[domain],
            "grounded": False,
        }
    try:
        if domain == "health":
            from agents.health_agent import explain as _health_explain
            headline = _health_explain(verdict).get("headline") or _DOMAIN_NOT_COMPUTED_MESSAGES[domain]
        elif domain == "compliance":
            from agents.compliance_agent import explain_block as _compliance_explain
            headline = _compliance_explain(verdict).get("headline") or _DOMAIN_NOT_COMPUTED_MESSAGES[domain]
        elif domain == "fraud":
            from agents.fraud_agent import explain as _fraud_explain
            headline = _fraud_explain(verdict).get("headline") or _DOMAIN_NOT_COMPUTED_MESSAGES[domain]
        else:  # domain == "insurance"
            headline = _format_insurance_answer(verdict)
    except Exception as exc:  # noqa: BLE001 — never raise out of a /refine handler
        logger.warning("followup_parser: build_domain_answer(%s) formatter failed: %s", domain, exc)
        return {
            "domain": domain,
            "headline": _DOMAIN_NOT_COMPUTED_MESSAGES[domain],
            "grounded": False,
        }
    return {"domain": domain, "headline": headline, "grounded": True}


def build_assistant_reply(
    *,
    prev_envelope: dict,
    new_envelope: dict,
    changed: list[str],
) -> str:
    """Build a truthful assistant reply from the actual diff between envelopes.

    Computes deltas from the REAL new_envelope (not the LLM) — honest by construction.
    """
    parts: list[str] = []

    # Budget change.
    old_total = prev_envelope.get("package_total_with_fees_cents") or prev_envelope.get("package_total_cents") or 0
    new_total = new_envelope.get("package_total_with_fees_cents") or new_envelope.get("package_total_cents") or 0
    if old_total and new_total and old_total != new_total:
        diff_pct = (new_total - old_total) / old_total * 100
        sign = "+" if diff_pct > 0 else ""
        parts.append(
            f"New held total ${new_total/100:,.0f} (was ${old_total/100:,.0f}, {sign}{diff_pct:.0f}%)."
        )
    elif new_total:
        parts.append(f"New held total ${new_total/100:,.0f}.")

    # City / leg changes.
    old_cities = [l.get("city", "?") for l in (prev_envelope.get("legs") or [])
                  if isinstance(l, dict)]
    new_cities = [l.get("city", "?") for l in (new_envelope.get("legs") or [])
                  if isinstance(l, dict)]
    added = [c for c in new_cities if c not in old_cities]
    removed = [c for c in old_cities if c not in new_cities]
    if added:
        parts.append(f"Added: {', '.join(added)}.")
    if removed:
        parts.append(f"Removed: {', '.join(removed)}.")

    # Night count.
    old_nights = _total_nights_from_legs(prev_envelope.get("legs") or [])
    new_nights = _total_nights_from_legs(new_envelope.get("legs") or [])
    if new_nights and new_nights != old_nights:
        parts.append(f"Duration: {new_nights} nights (was {old_nights}).")

    # Summary line.
    n_cities = len(new_cities)
    parts.append(f"{n_cities} {'city' if n_cities == 1 else 'cities'} · {new_nights or '?'} nights total.")

    if not parts:
        parts.append("Your plan has been updated.")

    return " ".join(parts)


def _total_nights_from_legs(legs: list) -> int:
    total = 0
    for leg in legs:
        if not isinstance(leg, dict):
            continue
        n = leg.get("nights") or _nights_from_dates(leg.get("checkin"), leg.get("checkout"))
        total += n or 0
    return total
