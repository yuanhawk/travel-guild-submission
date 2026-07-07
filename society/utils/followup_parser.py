"""followup_parser.py — B3: parse conversational follow-ups into structured deltas + apply them.

`parse_followup(message, trip_request, turns)` calls the LLM to map NL → a bounded
delta object (closed op-set); all hard numbers from the LLM are clamped / validated
before use — this is the FUZZY FRONT only, exactly like intent_parser._llm_call and
the narrator. No fabrication can reach the deterministic core.

`apply_delta(trip_request, delta)` is a PURE FUNCTION — deepcopy, mutate, return
(new_request, changed_list). The result is then passed to orch.negotiate(commit=False)
which is the unchanged byte-identical deterministic core.

var-0 argument: parse_followup is off the digest path (it's called only by /refine);
apply_delta never touches negotiate/_request_digest; the conversation table is a
side-effect written after negotiate completes. Anonymous /negotiate_text path is
entirely untouched.
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

logger = logging.getLogger(__name__)

DASHSCOPE_API_KEY = os.environ.get("DASHSCOPE_API_KEY", "")
DASHSCOPE_BASE_URL = "https://dashscope-intl.aliyuncs.com/compatible-mode/v1"
# Use the same fast model as the intent parser, defaulting to qwen-turbo for refine.
_MODEL = os.environ.get("SOCIETY_REFINE_MODEL", os.environ.get("SOCIETY_LLM_MODEL", "qwen-turbo"))

# How much to adjust budget when the user says "cheaper/cheaper" without a percentage.
REFINE_STEP_PCT = 0.15

_FOLLOWUP_SYSTEM_PROMPT = """You are the Travel Guild's trip-edit assistant.
The user has an EXISTING trip plan and wants to change something.
Extract their INTENT as a JSON object with one field "ops": an array of operations
from the CLOSED set below. Do NOT invent operations outside this set.

CLOSED OPERATION SET:
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
  {"op":"home_currency_set", "currency":"<3-letter ISO code e.g. SGD EUR GBP JPY AUD CAD>"}
  {"op":"nationality_set",   "iso2":"<2-letter country ISO code e.g. SG GB US AU>"}

If you cannot map the request to ANY operation in this set, set "unsupported":true and
briefly explain in "reason". If you can map SOME but not all, emit the supported ops
and note the unsupported part in "reason".

RULES:
- "budget_adjust" with no pct: emit null (server uses 15% default).
- For "cheaper": direction="cheaper". For "more expensive"/"luxury": direction="higher".
- city names: use the plain city name as the user said it; the server normalises.
- "I'm located in X" / "I'm from X" / "I live in X" → nationality_set with the 2-letter ISO code for X.
- "show me in SGD" / "convert to EUR" / "prices in GBP" → home_currency_set.
- Output ONLY the JSON object, nothing else.

Output format:
{"ops":[...], "unsupported":false|true, "reason":null|"<string>"}
"""

# Patterns that are pure informational queries — answer from context without re-planning.
_INFO_QUERY_RE = re.compile(
    r"^\s*(?:what(?:'s|'s| is| are)?|how much|can you (?:tell|show) me|show me|give me)\s+"
    r"(?:the\s+)?(?:budget|cost|price|total|breakdown|estimate|summary|itinerary|plan|dates?|"
    r"nights?|days?|hotels?|lodging|insurance|visa|health|schedule)\b",
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

    Never raises. Returns a dict with keys: ops (list), unsupported (bool), reason (str|None).
    On LLM failure → returns unsupported=True with an honest reason.
    On info-query → returns unsupported=False, ops=[], query_answer=<string> (no LLM call).
    """
    # #116: fast-path for informational queries — answer from context, no LLM, no re-plan.
    info_reply = _info_query_reply(message, trip_request)
    if info_reply is not None:
        return {"ops": [], "unsupported": False, "reason": None, "query_answer": info_reply}

    _empty = {"ops": [], "unsupported": True, "reason": "LLM unavailable"}
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
        return {
            "ops": parsed.get("ops") or [],
            "unsupported": bool(parsed.get("unsupported", False)),
            "reason": parsed.get("reason"),
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


def _fmt_month_day(d: date) -> str:
    """'Jul 12' — %b + bare day number (avoids the non-portable %-d strftime flag)."""
    return f"{d.strftime('%b')} {d.day}"


def _format_added_leg_dates(checkin: str | None, checkout: str | None) -> str | None:
    """Human-readable date range for a newly-added leg, e.g. 'Jul 12' or 'Jul 13-14'.

    Returns None (never a fabricated/garbage string) when checkin/checkout are
    missing or unparseable — caller falls back to the bare city name.
    """
    try:
        ci = date.fromisoformat(checkin or "")
        co = date.fromisoformat(checkout or "")
    except Exception:
        return None
    if co <= ci:
        # 0-night / same-day leg — a single date, not a range.
        return _fmt_month_day(ci)
    # checkout is the morning-after departure date; display the last NIGHT
    # (checkout - 1 day), not the checkout date itself.
    display_end = co - timedelta(days=1)
    if display_end == ci:
        return _fmt_month_day(ci)
    if display_end.month == ci.month:
        return f"{_fmt_month_day(ci)}-{display_end.day}"
    return f"{_fmt_month_day(ci)}-{_fmt_month_day(display_end)}"


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


def apply_delta(
    trip_request: dict,
    delta: dict,
) -> tuple[dict, list[str]]:
    """Apply a parsed delta to a trip_request (deepcopy — pure function).

    Returns (new_request, changed) where changed is a list of human-readable
    strings describing what was actually modified (truthful, used in assistant_reply).

    NOTE: swap_item ops are silently skipped (returned in changed as 'swap_item:unsupported').
    Unknown cities in add_leg are dropped and noted in changed.
    """
    import copy as _copy
    req = _copy.deepcopy(trip_request)
    changed: list[str] = []
    legs: list[dict] = req.get("legs") or []

    for op in (delta.get("ops") or []):
        if not isinstance(op, dict):
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
            # Unknown op — drop silently (closed-set guard).
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
        # #199: disclose WHICH dates each newly-added city landed on — the leg's
        # checkin/checkout already exist on new_envelope["legs"]; surface them per
        # city instead of leaving the user to dig through the itinerary UI.
        legs_by_city = {
            l.get("city", "?"): l for l in (new_envelope.get("legs") or []) if isinstance(l, dict)
        }
        added_display = []
        for city in added:
            leg = legs_by_city.get(city) or {}
            checkin, checkout = leg.get("checkin"), leg.get("checkout")
            date_range = _format_added_leg_dates(checkin, checkout) if checkin and checkout else None
            added_display.append(f"{city} ({date_range})" if date_range else city)
        parts.append(f"Added: {', '.join(added_display)}.")
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
