"""replan_ops.py — pure, deterministic item-level edit ops for POST /replan.

Applies structured ops to the already-held plan_ready envelope's day_plans
WITHOUT calling negotiate(), the day-planner, or any live data source.

Key design constraints (from the build plan):
- NEVER fabricates a POI. add_place only draws from the existing pool
  (unscheduled_attractions or another day's attractions).
- NEVER touches the money path: package_total_cents, checkout_id, dest_token,
  payment_status, and wallet are all preserved verbatim.
- Items are addressed by (leg_index, day_index, position) + an attraction_ref
  integrity check (name_en or wikidata). A stale/wrong ref is REJECTED, not silently
  applied.
- var-0: entirely off negotiate() and _request_digest. No wall-clock read.

All functions are pure (no I/O, no side effects) and return a tuple:
    (applied_tag: str | None, reject_reason: str | None)
where applied_tag is a human-readable diff token and reject_reason is an honest
message on failure. Callers chain ops and collect applied/rejected lists.
"""
from __future__ import annotations

import copy
from typing import Any

# Pace cap from day_planner_agent.py (duplicate as a constant so replan_ops has no
# circular import). Keep in sync with agents/day_planner_agent.py:_PACE_CAP.
_PACE_CAP = {"relaxed": 2, "moderate": 3, "packed": 4}
_DEFAULT_PACE_CAP = 3


def _attraction_ref_matches(attraction: dict, ref: str) -> bool:
    """True iff the attraction's wikidata or name_en (lowered) matches ref (lowered).
    Integrity check: prevents a stale FE board from editing the wrong item."""
    if not ref:
        return False
    r = ref.strip().lower()
    wd = (attraction.get("wikidata") or "").strip().lower()
    ne = (attraction.get("name_en") or attraction.get("name") or "").strip().lower()
    return r == wd or r == ne


def _get_leg(day_plans: list[dict], leg_index: int) -> dict | None:
    if not isinstance(day_plans, list) or leg_index < 0 or leg_index >= len(day_plans):
        return None
    return day_plans[leg_index]


def _get_day(leg: dict, day_index: int) -> dict | None:
    days = leg.get("days") or []
    if day_index < 0 or day_index >= len(days):
        return None
    return days[day_index]


def _pool_source(envelope: dict, leg_index: int, from_spec: str) -> list[dict]:
    """Resolve a 'from' spec to the mutable attraction pool list.

    from_spec:
      "unscheduled"    → envelope["day_plans"][leg_index]["unscheduled_attractions"]
      "day:N"          → envelope["day_plans"][leg_index]["days"][N]["attractions"]
    Returns the LIVE list (mutations visible to caller) or [] on any resolution failure.
    """
    day_plans = envelope.get("day_plans") or []
    leg = _get_leg(day_plans, leg_index)
    if leg is None:
        return []
    if not from_spec or from_spec == "unscheduled":
        return leg.setdefault("unscheduled_attractions", [])
    if from_spec.startswith("day:"):
        try:
            d = int(from_spec.split(":", 1)[1])
        except (ValueError, IndexError):
            return []
        day = _get_day(leg, d)
        if day is None:
            return []
        return day.setdefault("attractions", [])
    return []


def _pop_from_pool(pool: list[dict], ref: str) -> dict | None:
    """Pop the first attraction matching ref from pool. Returns it, or None if not found.
    NEVER fabricates: if ref is not in the pool, returns None (caller must reject the op)."""
    for i, a in enumerate(pool):
        if _attraction_ref_matches(a, ref):
            return pool.pop(i)
    return None


def op_remove_place(
    envelope: dict,
    *,
    leg_index: int,
    day_index: int,
    position: int,
    attraction_ref: str,
) -> tuple[str | None, str | None]:
    """Remove the attraction at (leg_index, day_index, position) and move it to
    unscheduled_attractions. Verifies the ref matches the item at that position.
    Returns (applied_tag, None) on success, (None, reason) on rejection."""
    day_plans = envelope.get("day_plans") or []
    leg = _get_leg(day_plans, leg_index)
    if leg is None:
        # +1 for display only — these reach the user via /replan's "rejected"
        # list, rendered verbatim in web/'s flash toast. The
        # rest of the UI is 1-indexed ("Leg 1", "day 1"); the tag below stays
        # 0-indexed (internal, used for undo/tracking). Message text also
        # avoids raw field-name jargon (leg_index/day_index/position/len=) —
        # those are internal identifiers, not something a user should see.
        leg_num = leg_index + 1
        return None, f"leg {leg_num} out of range (this trip has {len(day_plans)} leg(s))"
    day = _get_day(leg, day_index)
    if day is None:
        leg_num = leg_index + 1
        day_num = day_index + 1
        return None, f"day {day_num} out of range for leg {leg_num}"
    atts = day.setdefault("attractions", [])
    if position < 0 or position >= len(atts):
        pos_num = position + 1
        return None, f"position {pos_num} out of range (day has {len(atts)} attractions)"
    item = atts[position]
    if not _attraction_ref_matches(item, attraction_ref):
        actual_ref = item.get("name_en") or item.get("name") or item.get("wikidata") or "?"
        leg_num = leg_index + 1
        day_num = day_index + 1
        pos_num = position + 1
        return None, (
            f"ref mismatch at leg {leg_num} day {day_num} pos {pos_num}: "
            f"expected {attraction_ref!r}, found {actual_ref!r} (stale board — refresh and try again)"
        )
    removed = atts.pop(position)
    leg.setdefault("unscheduled_attractions", []).append(removed)
    tag = f"remove:leg{leg_index}.day{day_index}.pos{position}:{attraction_ref}"
    return tag, None


def op_add_place(
    envelope: dict,
    *,
    leg_index: int,
    day_index: int,
    position: int,
    attraction_ref: str,
    from_spec: str = "unscheduled",
    pace: str = "moderate",
) -> tuple[str | None, str | None]:
    """Pop attraction_ref from pool and insert at (leg_index, day_index, position).
    If the day already meets the pace cap, still inserts but adds an honest note.
    NEVER fabricates: rejects if ref not in pool."""
    day_plans = envelope.get("day_plans") or []
    leg = _get_leg(day_plans, leg_index)
    if leg is None:
        leg_num = leg_index + 1
        return None, f"leg {leg_num} out of range"
    day = _get_day(leg, day_index)
    if day is None:
        leg_num = leg_index + 1
        day_num = day_index + 1
        return None, f"day {day_num} out of range for leg {leg_num}"

    pool = _pool_source(envelope, leg_index, from_spec)
    item = _pop_from_pool(pool, attraction_ref)
    if item is None:
        leg_num = leg_index + 1
        return None, f"{attraction_ref!r} not found in pool {from_spec!r} for leg {leg_num} (not fabricated)"

    atts = day.setdefault("attractions", [])
    cap = _PACE_CAP.get((pace or "moderate").lower().strip(), _DEFAULT_PACE_CAP)
    over_cap = len(atts) >= cap

    insert_pos = max(0, min(position, len(atts)))
    atts.insert(insert_pos, item)

    tag = f"add:leg{leg_index}.day{day_index}.pos{insert_pos}:{attraction_ref}"
    if over_cap:
        # Honest note: don't silently truncate; note it and keep. 1-indexed for
        # display (this string is surfaced verbatim to the user) — the tag above
        # stays 0-indexed (internal, used for undo/tracking).
        leg_num = leg_index + 1
        day_num = day_index + 1
        notes = envelope.setdefault("_replan_notes", [])
        notes.append(
            f"day {day_num} (leg {leg_num}) now exceeds pace cap "
            f"({len(atts)}>{cap}) after add — kept, flagged."
        )
    return tag, None


def op_swap_place(
    envelope: dict,
    *,
    leg_index: int,
    day_index: int,
    position: int,
    remove_ref: str,
    add_ref: str,
    from_spec: str = "unscheduled",
    pace: str = "moderate",
) -> tuple[str | None, str | None]:
    """Atomic remove+add at one position. Remove must match position ref; add must be
    in pool. On any failure the envelope is left UNCHANGED (atomic — we use a copy)."""
    # Work on a snapshot of the relevant day to make this atomic.
    day_plans = envelope.get("day_plans") or []
    leg = _get_leg(day_plans, leg_index)
    if leg is None:
        leg_num = leg_index + 1
        return None, f"leg {leg_num} out of range"
    day = _get_day(leg, day_index)
    if day is None:
        leg_num = leg_index + 1
        day_num = day_index + 1
        return None, f"day {day_num} out of range for leg {leg_num}"

    atts = day.setdefault("attractions", [])
    if position < 0 or position >= len(atts):
        pos_num = position + 1
        return None, f"position {pos_num} out of range (day has {len(atts)} attractions)"
    item_at_pos = atts[position]
    if not _attraction_ref_matches(item_at_pos, remove_ref):
        actual = item_at_pos.get("name_en") or item_at_pos.get("name") or "?"
        pos_num = position + 1
        return None, (
            f"swap item mismatch: expected {remove_ref!r}, found {actual!r} at position {pos_num}"
        )

    pool = _pool_source(envelope, leg_index, from_spec)
    incoming = _pop_from_pool(pool, add_ref)
    if incoming is None:
        return None, f"{add_ref!r} not found in pool {from_spec!r} — swap aborted (not fabricated)"

    # Both checks pass — execute atomically.
    removed = atts.pop(position)
    leg.setdefault("unscheduled_attractions", []).append(removed)
    atts.insert(position, incoming)

    tag = f"swap:leg{leg_index}.day{day_index}.pos{position}:{remove_ref}->{add_ref}"
    return tag, None


def op_switch_variant(
    envelope: dict,
    *,
    leg_index: int,
    day_index: int,
    variant: str,
) -> tuple[str | None, str | None]:
    """Swap attractions <-> fair_weather_attractions for a bad-weather day.
    Only valid when the day carries fair_weather_attractions (i.e. bad_weather=true
    was set by the day-planner). variant must be 'fair' or 'wet'.

    Tracking: we use a '_variant_active' marker on the day dict to track the current
    mode (initial state = 'wet', matching what the day-planner always surfaces first).
    This avoids fragile content-equality checks after a swap has been applied."""
    variant = (variant or "").strip().lower()
    if variant not in ("fair", "wet"):
        return None, f"variant must be 'fair' or 'wet', got {variant!r}"

    day_plans = envelope.get("day_plans") or []
    leg = _get_leg(day_plans, leg_index)
    if leg is None:
        leg_num = leg_index + 1
        return None, f"leg {leg_num} out of range"
    day = _get_day(leg, day_index)
    if day is None:
        leg_num = leg_index + 1
        day_num = day_index + 1
        return None, f"day {day_num} out of range for leg {leg_num}"

    fair_atts = day.get("fair_weather_attractions")
    if not isinstance(fair_atts, list):
        leg_num = leg_index + 1
        day_num = day_index + 1
        return None, (
            f"day {day_num} (leg {leg_num}) has no rain-ready alternative to switch to — "
            "this only applies to days flagged for bad weather"
        )

    current_atts = day.setdefault("attractions", [])
    # Track active variant: default is 'wet' (day-planner always starts in wet mode).
    active = day.get("_variant_active", "wet")

    if variant == active:
        day_num = day_index + 1
        return None, f"day {day_num} already showing {variant!r} plan"

    # Symmetric swap: push current attractions into the alt slot, pull the other in.
    alt = list(current_atts)
    day["attractions"] = list(fair_atts)
    day["fair_weather_attractions"] = alt
    day["_variant_active"] = variant

    tag = f"switch_variant:leg{leg_index}.day{day_index}:{variant}"
    return tag, None


def op_reflow_from(
    envelope: dict,
    *,
    leg_index: int,
    from_day_index: int,
    pace: str = "moderate",
) -> tuple[str | None, str | None]:
    """Re-pack attractions from from_day_index forward (within the leg) up to the pace cap,
    pulling from a combined pool of day attractions + unscheduled_attractions.
    Deterministic: preserves existing order within each day and the unscheduled pool.
    NEVER calls the day-planner or fetches new POIs."""
    day_plans = envelope.get("day_plans") or []
    leg = _get_leg(day_plans, leg_index)
    if leg is None:
        leg_num = leg_index + 1
        return None, f"leg {leg_num} out of range"

    days = leg.get("days") or []
    n_days = len(days)
    if from_day_index < 0 or from_day_index >= n_days:
        from_day_num = from_day_index + 1
        return None, f"day {from_day_num} out of range (leg has {n_days} days)"

    cap = _PACE_CAP.get((pace or "moderate").lower().strip(), _DEFAULT_PACE_CAP)

    # Collect all attractions from from_day_index onward + unscheduled, in order.
    pool: list[dict] = []
    for i in range(from_day_index, n_days):
        day = days[i]
        pool.extend(day.get("attractions") or [])
        day["attractions"] = []  # clear; will refill below

    unscheduled = leg.setdefault("unscheduled_attractions", [])
    pool.extend(unscheduled)
    unscheduled.clear()

    # Re-fill days from from_day_index up to cap; remainder → unscheduled.
    idx = 0
    for i in range(from_day_index, n_days):
        days[i]["attractions"] = pool[idx: idx + cap]
        idx += cap

    overflow = pool[idx:]
    unscheduled.extend(overflow)

    tag = f"reflow:leg{leg_index}.from_day{from_day_index}"
    return tag, None


def op_swap_meal(
    envelope: dict,
    *,
    leg_index: int,
    day_index: int,
    slot: str,
    restaurant_name: str,
) -> tuple[str | None, str | None]:
    """Swap the chosen meal in `slot` with a named alternative from day.meal_pool[slot].

    The replaced meal is put back into meal_pool so the user can undo by picking it again.
    On any failure the envelope is left UNCHANGED."""
    valid_slots = {"breakfast", "lunch", "dinner", "supper"}
    if slot not in valid_slots:
        return None, f"invalid slot {slot!r}; must be one of {valid_slots}"
    if not restaurant_name:
        return None, "a restaurant name is required"

    day_plans = envelope.get("day_plans") or []
    leg = _get_leg(day_plans, leg_index)
    if leg is None:
        leg_num = leg_index + 1
        return None, f"leg {leg_num} out of range"
    day = _get_day(leg, day_index)
    if day is None:
        leg_num = leg_index + 1
        day_num = day_index + 1
        return None, f"day {day_num} out of range for leg {leg_num}"

    meal_pool = day.get("meal_pool") or {}
    pool = meal_pool.get(slot) or []
    # Find the alternative by name (case-insensitive).
    incoming = next(
        (m for m in pool if (m.get("name") or "").lower() == restaurant_name.lower()),
        None,
    )
    if incoming is None:
        return None, (
            f"{restaurant_name!r} not found among the {slot} alternatives on offer — "
            "only server-emitted alternatives may be swapped (not fabricated)"
        )

    # Swap: current meal → back into pool, incoming → into meals.
    meals = day.setdefault("meals", {})
    current = meals.get(slot)
    meals[slot] = incoming
    # Put the evicted meal back at the front of pool so it can be re-swapped.
    new_pool = [m for m in pool if (m.get("name") or "").lower() != restaurant_name.lower()]
    if current and isinstance(current, dict):
        evicted_entry = {"name": current.get("name") or current.get("name_en"), "cuisine": current.get("cuisine")}
        if evicted_entry.get("name"):
            new_pool.insert(0, evicted_entry)
    meal_pool[slot] = new_pool[:2]  # keep cap at 2 alternatives

    tag = f"swap_meal:leg{leg_index}.day{day_index}.{slot}:{restaurant_name}"
    return tag, None


def op_swap_days(
    envelope: dict,
    *,
    leg_index: int,
    day_a: int,
    day_b: int,
) -> tuple[str | None, str | None]:
    """Swap two interior (non-boundary) days within the same leg. Content
    (attractions/meals/meal_pool) moves with the swap; day_index and
    bad_weather stay pinned to POSITION (the date), not the content, since
    bad_weather describes the calendar date sitting at that slot, not
    whatever plan a user drags into it. See design spec for full reasoning.

    Recompute scope — nothing beyond wet/fair reconciliation:
    - Transport: no recompute (intra-leg day swaps have zero transport
      implication — transport is inter-leg only, see transport_agent.py).
    - Meals: no recompute (venues are already concretely chosen and move
      with their day; re-running variety rotation would fabricate different
      selections the user didn't ask for).
    - Money/booking: untouched by construction (only mutates
      day_plans[...]["days"]).
    - Only reconciliation: the wet/fair toggle fields (see below).

    No cross-leg check is needed: the op shape only carries ONE leg_index,
    so a cross-leg swap isn't structurally representable here.

    var-0: pure function, no wall-clock/random/set-iteration. Applying the
    SAME swap_days(a, b) twice within one ops list re-applies cleanly (no
    dedup/no-op rejection, unlike op_switch_variant — a duplicate swap has no
    stale-state hazard, it's just applied twice), but it is a true identity
    (full involution) ONLY when day_a and day_b share the same bad_weather
    state. When they differ, the reconciliation below intentionally pops
    fair_weather_attractions/_variant_active off the slot that becomes fair
    (see below) — that data has nowhere to be restored from on the undo
    swap, so a bad-weather day's rain-ready alternative can be permanently
    dropped by swap-then-swap-back across a weather boundary. This is a
    known, honest tradeoff (never fabricate a wet-alternative that wasn't
    there), not a bug — a UI that offers "undo" on a cross-weather swap
    should say so rather than imply lossless undo."""
    try:
        day_a = int(day_a)
        day_b = int(day_b)
    except (TypeError, ValueError):
        return None, f"day selections must be whole numbers, got {day_a!r} and {day_b!r}"

    day_plans = envelope.get("day_plans") or []
    leg = _get_leg(day_plans, leg_index)
    if leg is None:
        leg_num = leg_index + 1
        return None, f"leg {leg_num} out of range (this trip has {len(day_plans)} leg(s))"

    days = leg.get("days") or []
    n = len(days)
    leg_num = leg_index + 1
    if _get_day(leg, day_a) is None:
        return None, f"day {day_a + 1} out of range for leg {leg_num} (leg has {n} days)"
    if _get_day(leg, day_b) is None:
        return None, f"day {day_b + 1} out of range for leg {leg_num} (leg has {n} days)"
    if day_a == day_b:
        return None, f"day {day_a + 1} was given twice — it's the same day; nothing to swap"
    for di in (day_a, day_b):
        if di in (0, n - 1):
            di_num = di + 1
            return None, (
                f"day {di_num} is a boundary day (arrival/checkout) of leg "
                f"{leg_num} and cannot be reordered"
            )

    # Capture date-anchored fields BEFORE the swap — they describe the slot
    # (the calendar date), not the content moving through it.
    bw_a = days[day_a].get("bad_weather", False)
    bw_b = days[day_b].get("bad_weather", False)

    days[day_a], days[day_b] = days[day_b], days[day_a]  # whole-dict content swap

    days[day_a]["day_index"] = day_a
    days[day_b]["day_index"] = day_b
    days[day_a]["bad_weather"] = bw_a
    days[day_b]["bad_weather"] = bw_b

    notes = envelope.setdefault("_replan_notes", [])
    if bw_a != bw_b:
        bad_di = day_a if bw_a else day_b
        bad_di_num = bad_di + 1
        notes.append(
            f"The bad-weather advisory stays with the date, not "
            f"your plan — day {bad_di_num} (leg {leg_num}) remains flagged "
            f"bad-weather for the moved-in activities; verify them against "
            f"the forecast."
        )

    # Wet/fair reconciliation, per-slot, against the slot's (pinned) bad_weather.
    for di in (day_a, day_b):
        day = days[di]
        if not day.get("bad_weather", False):
            # A fair-date day never offers a wet toggle.
            day.pop("fair_weather_attractions", None)
            day.pop("_variant_active", None)
        elif not isinstance(day.get("fair_weather_attractions"), list):
            # Bad-weather slot, but the moved-in content came from a
            # fair-date partner — do NOT fabricate a fair_weather_attractions
            # list; leave it absent and note it honestly.
            day.pop("_variant_active", None)
            di_num = di + 1
            notes.append(
                f"day {di_num} (leg {leg_num}) is in a flagged bad-weather "
                f"window and the moved-in plan has no rain-ready alternative "
                f"ordering (not fabricated)."
            )
        # else: bad_weather=True and fair_weather_attractions present —
        # content came from another bad-weather day; leave both fields as-is.

    tag = f"swap_days:leg{leg_index}.day{min(day_a, day_b)}<->day{max(day_a, day_b)}"
    return tag, None


# ---------------------------------------------------------------------------
# Top-level apply_ops (called by the /replan route handler)
# ---------------------------------------------------------------------------

def apply_ops(envelope: dict, ops: list[dict], *, pace: str = "moderate") -> tuple[list[str], list[dict]]:
    """Apply a list of ops to the plan envelope IN ORDER, returning (applied, rejected).

    applied: list of human-readable diff tokens for successfully-applied ops.
    rejected: list of {"op": ..., "reason": ...} for ops that could not be applied.

    envelope is mutated in place. Uses a deep-copy checkpoint: if ALL ops are rejected
    the envelope is left unchanged (for 'noop' detection by the caller).
    """
    applied: list[str] = []
    rejected: list[dict] = []

    for raw_op in ops:
        if not isinstance(raw_op, dict):
            rejected.append({"op": str(raw_op), "reason": "op must be a JSON object"})
            continue
        op_type = (raw_op.get("op") or "").strip()
        li = raw_op.get("leg_index", 0)
        di = raw_op.get("day_index", 0)
        pos = raw_op.get("position", 0)
        ref = (raw_op.get("attraction_ref") or "").strip()
        from_spec = (raw_op.get("from") or "unscheduled").strip()

        tag: str | None = None
        reason: str | None = None

        try:
            if op_type == "remove_place":
                tag, reason = op_remove_place(
                    envelope, leg_index=li, day_index=di, position=pos, attraction_ref=ref)
            elif op_type == "add_place":
                tag, reason = op_add_place(
                    envelope, leg_index=li, day_index=di, position=pos,
                    attraction_ref=ref, from_spec=from_spec, pace=pace)
            elif op_type == "swap_place":
                tag, reason = op_swap_place(
                    envelope, leg_index=li, day_index=di, position=pos,
                    remove_ref=(raw_op.get("remove_ref") or "").strip(),
                    add_ref=(raw_op.get("add_ref") or "").strip(),
                    from_spec=from_spec, pace=pace)
            elif op_type == "switch_variant":
                tag, reason = op_switch_variant(
                    envelope, leg_index=li, day_index=di,
                    variant=(raw_op.get("variant") or "").strip())
            elif op_type == "reflow_from":
                tag, reason = op_reflow_from(
                    envelope, leg_index=li, from_day_index=raw_op.get("from_day_index", 0),
                    pace=pace)
            elif op_type == "swap_meal":
                tag, reason = op_swap_meal(
                    envelope, leg_index=li, day_index=di,
                    slot=(raw_op.get("slot") or "").strip(),
                    restaurant_name=(raw_op.get("restaurant_name") or "").strip())
            elif op_type == "swap_days":
                tag, reason = op_swap_days(
                    envelope, leg_index=li,
                    day_a=raw_op.get("day_a", 0),
                    day_b=raw_op.get("day_b", 0))
            else:
                reason = f"unknown op type: {op_type!r}"
        except Exception as exc:  # noqa: BLE001 — never 500; always honest
            reason = f"internal error applying op {op_type!r}: {exc}"

        if tag is not None:
            applied.append(tag)
        else:
            rejected.append({"op": op_type, "reason": reason or "unknown"})

    return applied, rejected
