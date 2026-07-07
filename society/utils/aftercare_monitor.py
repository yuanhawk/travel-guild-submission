"""aftercare_monitor.py — #100 AFTERCARE: proactive risk-monitoring for BOOKED trips.

Builds on the firewalled emergency_feed seam and mirrors the query/alert pattern of
orchestrator._maybe_check_active_emergencies (L3241-3351) but reads a STORED BOOKED TRIP
instead of a live negotiate result.

CHANNEL-SECURITY BOUNDARY (non-negotiable):
  - READ-ONLY: calls ONLY store.get_trip + store.get_user. NEVER mark_booked /
    mark_cancelled / save_plan / complete_checkout / _wallet_debit_locked or any
    transactional symbol.
  - Telegram sender is SUGGEST-ONLY: URL buttons only, no callback_data (no inbound
    command path).

var-0 SACRED: this module is NEVER imported by negotiate()/_request_digest. `now` is
injected (off the digest). The deterministic core is byte-identical.

HONESTY:
  - NEVER fabricates a risk: feed fields copied verbatim; no feed item → no alert.
  - Feed unavailable → honest 'unavailable' status (NEVER a fabricated all-clear).
  - Translation failure → honest English fallback (translated:false).
  - Thin coverage → honest beta note.
"""
from __future__ import annotations

import html
import logging
from typing import Any

logger = logging.getLogger(__name__)

# Severity rank for dedup (high > medium > monitoring).
_SEV_RANK: dict[str, int] = {"high": 3, "medium": 2, "monitoring": 1}

_BETA_NOTE = (
    "Proactive monitoring is in beta: it currently covers tropical-cyclone / flood / wildfire "
    "emergencies (GDACS) for catalogued destinations. Thinly-covered places or other hazard "
    "types may not trigger an alert — always check local advisories."
)

_TRANSLATION_NOTE = (
    "Auto-translated by AI; original shown if translation unavailable."
)


def _choose_suggested_action(
    resp: dict,
    plan: dict,
    leg_index: int,
) -> tuple[str, dict | None]:
    """Return (suggested_action, action_target | None).

    Decision tree (suggest-only; all targets are WEBPAGE links):
    1. A fair_weather_attractions list exists on any day in this leg → switch_wet_weather_variant.
    2. Else active high/medium → reconsider_leg (whole-leg danger; be honest).
    For monitoring tier → 'monitor', no action_target.
    """
    severity = resp.get("severity", "")
    is_active = resp.get("active", False)

    if severity == "monitoring" and not is_active:
        return "monitor", None

    days: list[dict] = plan.get("days") or []
    leg_idx = leg_index  # day_plans list index for this plan
    for day_idx, day in enumerate(days):
        if isinstance(day.get("fair_weather_attractions"), list):
            replan_op = {
                "op": "switch_variant",
                "leg_index": leg_idx,
                "day_index": day_idx,
                "variant": "fair",
            }
            return "switch_wet_weather_variant", {
                "kind": "replan_switch_variant",
                "webpage_path": "",  # filled in by run_aftercare_check with digest
                "replan_op": replan_op,
            }

    # No variant → whole-leg danger advisory (be honest, not auto-switch)
    return "reconsider_leg", {
        "kind": "webpage_review",
        "webpage_path": "",  # filled in by run_aftercare_check
    }


def _build_alert(
    resp: dict,
    plan: dict,
    leg_index: int,
    leg_id: str,
    leg_dates: dict,
    digest: str = "",
) -> dict:
    """Build an ALERT object from a feed response dict. Fields are copied VERBATIM
    from the feed — never fabricated."""
    city = plan.get("city") or ""
    iso2 = (plan.get("iso2") or "").strip().upper()
    checkin, checkout = leg_dates.get(leg_id, ("", ""))

    suggested_action, action_target = _choose_suggested_action(resp, plan, leg_index)

    # Fill in the webpage path from the digest
    if action_target and not action_target.get("webpage_path"):
        focus = f"leg-{leg_index}"
        action_target["webpage_path"] = (
            f"/trip/trip-{digest}?focus={focus}#replan" if digest else f"/trip?focus={focus}#replan"
        )

    summary = resp.get("headline") or ""
    alert: dict[str, Any] = {
        "leg_id": leg_id,
        "city": city,
        "iso2": iso2,
        "date_window": {"checkin": checkin, "checkout": checkout},
        "risk_type": resp.get("hazard") or "",       # verbatim from feed
        "severity_tier": resp.get("severity") or "", # verbatim from feed
        "summary": summary,                          # verbatim English headline from feed
        "summary_localized": summary,                # default = English (will be overwritten by translator)
        "lang": "en",
        "translated": False,
        "translation_note": _TRANSLATION_NOTE,
        "advice": resp.get("advice") or "",          # verbatim from feed
        "suggested_action": suggested_action,
        "source": resp.get("source") or "",          # verbatim from feed (provenance)
        "as_of": resp.get("as_of") or "",
        "beta": False,
    }
    if action_target is not None:
        alert["action_target"] = action_target
    return alert


def monitor_booked_trip(
    trip_row: dict,
    *,
    emergency_client,
    now: str = "",
) -> dict:
    """Pure-ish: given a stored trip row (store.get_trip) and a feed client, return
    {status, as_of, source, checked_legs, alerts:[ALERT...], beta_legs:[leg_id...]}.

    NEVER fabricates a risk; only surfaces real feed active/monitoring items.
    `now` is an injected ISO date (off var-0); unused for filtering beyond the feed's
    own window logic. Never raises.

    If emergency_client is None (feed not configured) → status:'unavailable', alerts:[].
    """
    if emergency_client is None:
        return {
            "status": "unavailable",
            "as_of": now,
            "source": "",
            "checked_legs": 0,
            "alerts": [],
            "beta_legs": [],
            "beta_note": _BETA_NOTE,
        }

    try:
        return _monitor_impl(trip_row, emergency_client=emergency_client, now=now)
    except Exception as exc:  # noqa: BLE001 — aftercare must never crash
        logger.warning("aftercare_monitor: unexpected error (%s) → unavailable", exc)
        return {
            "status": "unavailable",
            "as_of": now,
            "source": "",
            "checked_legs": 0,
            "alerts": [],
            "beta_legs": [],
            "beta_note": _BETA_NOTE,
        }


def _monitor_impl(trip_row: dict, *, emergency_client, now: str) -> dict:
    """Inner implementation (exceptions may bubble to the caller which catches all)."""
    envelope = trip_row.get("envelope") or {}
    digest = (trip_row.get("digest") or trip_row.get("idempotency_key") or "").replace("trip-", "")

    # Build leg_id → (checkin, checkout) from envelope["legs"] (mirrors orchestrator L3272-3276)
    leg_dates: dict[str, tuple] = {}
    for i, lg in enumerate(envelope.get("legs") or []):
        if isinstance(lg, dict):
            leg_id = lg.get("leg_id") or f"leg-{i}"
            leg_dates[leg_id] = (lg.get("checkin") or "", lg.get("checkout") or "")

    day_plans: list[dict] = [
        p for p in (envelope.get("day_plans") or []) if isinstance(p, dict)
    ]

    raw_alerts: list[dict] = []
    unavailable_legs: list[str] = []
    beta_legs: list[str] = []
    feed_source = ""
    feed_as_of = ""

    for leg_index, plan in enumerate(day_plans):
        leg_id = plan.get("leg_id") or f"leg-{leg_index}"
        city = plan.get("city") or ""
        iso2 = (plan.get("iso2") or "").strip().upper()
        ci, co = leg_dates.get(leg_id, ("", ""))

        # Thin-coverage detection (plan.iso2 empty or catalog_hit is False → beta)
        if not iso2 or plan.get("catalog_hit") is False:
            beta_legs.append(leg_id)

        query = {
            "city": city,
            "iso2": iso2,
            "region": plan.get("region"),
            "checkin": plan.get("checkin") or ci,
            "checkout": plan.get("checkout") or co,
        }

        try:
            resp = emergency_client(query)
        except Exception as exc:  # noqa: BLE001 — feed errors are honest unavailable
            logger.warning("aftercare_monitor: feed error for %s (%s) → unavailable", city, exc)
            resp = None

        if not isinstance(resp, dict):
            # Feed unavailable for this leg — honest note (NEVER a fabricated all-clear)
            unavailable_legs.append(leg_id)
            continue

        # Track feed provenance from any response
        if resp.get("source"):
            feed_source = resp["source"]
        if resp.get("as_of"):
            feed_as_of = resp["as_of"]

        if resp.get("active"):
            alert = _build_alert(resp, plan, leg_index, leg_id, leg_dates, digest)
            raw_alerts.append(alert)
        elif resp.get("monitoring") or resp.get("severity") == "monitoring":
            alert = _build_alert(resp, plan, leg_index, leg_id, leg_dates, digest)
            alert["suggested_action"] = "monitor"
            alert.pop("action_target", None)
            raw_alerts.append(alert)
        # clear → no alert (record clear status only)

    # Dedup by country (mirrors orchestrator L3337-3351): one alert per iso2, highest severity
    _rank = _SEV_RANK
    by_iso2: dict[str, dict] = {}
    order: list[str] = []
    for alert in raw_alerts:
        key = (alert.get("iso2") or "").strip().upper() or alert.get("leg_id", "")
        if not key:
            key = alert.get("leg_id", "")
        if key not in by_iso2:
            by_iso2[key] = alert
            order.append(key)
        else:
            existing_rank = _rank.get(by_iso2[key].get("severity_tier", ""), 0)
            new_rank = _rank.get(alert.get("severity_tier", ""), 0)
            if new_rank > existing_rank:
                by_iso2[key] = alert
    deduped_alerts = [by_iso2[k] for k in order]

    # Mark beta on deduped alerts whose leg_id is in beta_legs
    for alert in deduped_alerts:
        if alert.get("leg_id") in beta_legs:
            alert["beta"] = True

    status = "unavailable" if unavailable_legs else "ok"
    result: dict[str, Any] = {
        "status": status,
        "as_of": feed_as_of or now,
        "source": feed_source,
        "checked_legs": len(day_plans),
        "alerts": deduped_alerts,
        "beta_legs": beta_legs,
    }
    if beta_legs or status == "unavailable":
        result["beta_note"] = _BETA_NOTE
    return result


def run_aftercare_check(
    idempotency_key: str,
    user_id: str,
    *,
    store,
    emergency_client,
    translator=None,
    sender=None,
    webapp_base: str = "",
    now: str = "",
) -> dict:
    """Read trip (store.get_trip) → require status=='booked' → monitor_booked_trip →
    localize each alert (translator, deliverable 2) → if alerts & sender: send suggest-only
    Telegram (deliverable 3) → return the FE payload (deliverable 4 contract).

    SECURITY: `user_id` is the caller-supplied value from the request body and is
    NOT an authorization/identity input (the endpoint's own ownership check is on
    the trip, via session_token/owner_token — see server.py's
    _authorize_trip_action docstring; a caller can claim any user_id here). It is
    accepted for call-signature compatibility only and is intentionally IGNORED
    for language/Telegram-chat_id resolution, which is derived solely from the
    trip's stored owner (trip_row["user_id"]). Do not wire it back in without
    re-reading that docstring.

    READ-ONLY w.r.t. the store: calls ONLY store.get_trip + store.get_user. Never
    mark_booked/mark_cancelled/save_plan. Never raises.

    CRON SEAM: a real cron would iterate booked trips and call this function per trip on
    an interval — e.g. store.list_booked_trips() (not needed for MVP; the /aftercare/check
    endpoint triggers on-demand). No code path differs between on-demand and cron invocation.
    """
    try:
        return _run_impl(
            idempotency_key, user_id,
            store=store, emergency_client=emergency_client,
            translator=translator, sender=sender,
            webapp_base=webapp_base, now=now,
        )
    except Exception as exc:  # noqa: BLE001 — aftercare must never crash
        logger.warning("run_aftercare_check: unexpected error (%s)", exc)
        return {
            "outcome": "error",
            "idempotency_key": idempotency_key,
            "monitoring": {"status": "unavailable", "as_of": now, "source": "", "checked_legs": 0},
            "alerts": [],
            "beta_note": _BETA_NOTE,
            "telegram": {"attempted": False, "sent": False, "note": "internal error"},
        }


def _run_impl(
    idempotency_key: str,
    user_id: str,
    *,
    store,
    emergency_client,
    translator,
    sender,
    webapp_base: str,
    now: str,
) -> dict:
    """Inner implementation of run_aftercare_check."""
    # READ-ONLY store access only
    trip_row = store.get_trip(idempotency_key)
    if trip_row is None:
        return {
            "outcome": "unknown_trip",
            "idempotency_key": idempotency_key,
            "monitoring": {"status": "unavailable", "as_of": now, "source": "", "checked_legs": 0},
            "alerts": [],
            "beta_note": None,
            "telegram": {"attempted": False, "sent": False, "note": "trip not found"},
        }

    if trip_row.get("status") != "booked":
        return {
            "outcome": "not_booked",
            "idempotency_key": idempotency_key,
            "monitoring": {"status": "unavailable", "as_of": now, "source": "", "checked_legs": 0},
            "alerts": [],
            "beta_note": None,
            "telegram": {"attempted": False, "sent": False, "note": "trip is not booked"},
        }

    # Load user for language resolution AND Telegram chat_id resolution (READ-ONLY).
    #
    # SECURITY: this MUST be the trip's actual, stored owner (trip_row["user_id"]),
    # never the caller-supplied `user_id` argument. The caller (server.py's
    # /aftercare/check) authorizes access to the TRIP via a session_token/
    # owner_token bound to that trip (_authorize_trip_action) — its own docstring
    # states plainly that the request body's user_id "is no longer an
    # authorization input" and "a caller could simply CLAIM any user_id, no proof
    # required." If we honored that claim here, an attacker who is legitimately
    # authorized for THEIR OWN trip could set body.user_id to a victim's user_id
    # and this trip's alert (and its Telegram delivery) would be resolved against
    # the VICTIM's linked chat_id/prefs instead of the true trip owner's — a
    # cross-user delivery leak of the same class as the removed
    # TELEGRAM_ALLOWED_USERS[0] fallback. Guest trips (no stored owner) honestly
    # get no personalized user at all (English default, no telegram chat_id) —
    # there is no verified identity to resolve one, and CANNOT be spoofed by a
    # caller-supplied user_id.
    resolved_user_id = trip_row.get("user_id") or ""
    user = None
    if resolved_user_id:
        try:
            user = store.get_user(resolved_user_id)
        except Exception:  # noqa: BLE001 — missing user → English fallback
            user = None

    # Apply webapp_base to alerts action_target paths
    envelope = trip_row.get("envelope") or {}
    digest = (trip_row.get("digest") or trip_row.get("idempotency_key") or "").replace("trip-", "")

    monitoring_result = monitor_booked_trip(
        trip_row, emergency_client=emergency_client, now=now
    )

    alerts = monitoring_result.get("alerts") or []

    # Resolve user language for localization
    lang = _resolve_lang(user)

    # Localize each alert summary
    for alert in alerts:
        # Patch webpage_path with webapp_base (was built with relative path)
        if alert.get("action_target"):
            rel = alert["action_target"].get("webpage_path") or ""
            if rel and webapp_base:
                alert["action_target"]["webpage_path"] = webapp_base.rstrip("/") + rel

        # Translate summary
        original = alert.get("summary") or ""
        if lang == "en" or not original:
            alert["summary_localized"] = original
            alert["translated"] = False  # English original — NOT auto-translated (honesty)
            alert["lang"] = "en"
        else:
            alert["lang"] = lang
            if translator is not None:
                try:
                    localized = translator(original, lang)
                except Exception:  # noqa: BLE001 — translation failure → English fallback
                    localized = None
            else:
                localized = None

            if localized:
                alert["summary_localized"] = localized
                alert["translated"] = True
                alert["translation_note"] = _TRANSLATION_NOTE
            else:
                # Honest English fallback
                alert["summary_localized"] = original
                alert["translated"] = False
                alert["translation_note"] = _TRANSLATION_NOTE

    # Telegram (suggest-only, gated to booked trips with alerts)
    tg_result: dict = {"attempted": False, "sent": False, "note": "no alerts"}
    if alerts and sender is not None:
        import os
        token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
        allowed = os.environ.get("TELEGRAM_ALLOWED_USERS", "")
        if token:
            # Resolve chat_id: user.prefs.telegram_chat_id ONLY. There is deliberately no
            # "first TELEGRAM_ALLOWED_USERS entry" fallback here — this endpoint is
            # multi-user, and falling back to an unrelated allowed_users entry would
            # deliver User B's risk alert (e.g. "your flight was cancelled") into User
            # A's Telegram chat: a real privacy/correctness leak, not a cosmetic bug.
            # A user with no linked chat_id honestly gets "no chat_id resolved" below.
            chat_id = ""
            if user and isinstance(user.get("prefs"), dict):
                chat_id = str(user["prefs"].get("telegram_chat_id") or "").strip()

            # DEMO/TEST CONVENIENCE ONLY: lets a developer see a live Telegram send
            # fire without linking a real chat_id, during manual testing. Double-gated
            # so it can NEVER silently apply to a real user's alert:
            #   1. explicit opt-in env var (unset by default in every real deployment)
            #   2. the env var's value must equal the requesting user_id AND that
            #      user_id must be one of the known pre-seeded demo users.
            if not chat_id and allowed:
                demo_fallback_uid = os.environ.get("TELEGRAM_DEMO_FALLBACK_USER_ID", "").strip()
                if demo_fallback_uid and resolved_user_id == demo_fallback_uid:
                    from orchestration.demo_users import DEMO_USERS
                    if any(u.get("user_id") == resolved_user_id for u in DEMO_USERS):
                        chat_id = allowed.split(",")[0].strip()

            if chat_id:
                msg = _build_telegram_message(alerts, webapp_base)
                try:
                    tg_result = sender(
                        chat_id, msg,
                        token=token, allowed_users=allowed
                    )
                    tg_result["attempted"] = True
                except Exception as exc:  # noqa: BLE001 — Telegram failure must not crash aftercare
                    logger.warning("aftercare: telegram send failed (%s)", exc)
                    tg_result = {"attempted": True, "sent": False, "note": str(exc)}
            else:
                tg_result = {"attempted": False, "sent": False, "note": "no chat_id resolved"}
        else:
            tg_result = {"attempted": False, "sent": False, "note": "telegram disabled (no token configured)"}

    return {
        "outcome": "ok",
        "idempotency_key": idempotency_key,
        "monitoring": {
            "status": monitoring_result.get("status"),
            "as_of": monitoring_result.get("as_of"),
            "source": monitoring_result.get("source"),
            "checked_legs": monitoring_result.get("checked_legs", 0),
        },
        "alerts": alerts,
        "beta_note": monitoring_result.get("beta_note"),
        "telegram": tg_result,
    }


def _resolve_lang(user: dict | None) -> str:
    """prefs.lang override > NATIONALITY_LANG[nationality] > 'en'. Pure."""
    from utils.aftercare_lang import resolve_lang
    return resolve_lang(user)


def _build_telegram_message(alerts: list[dict], webapp_base: str) -> str:
    """Build a suggest-only Telegram message body (plain text/HTML). Never fabricates."""
    lines = ["<b>Travel Guild — Proactive Risk Alert</b>"]
    for alert in alerts:
        sev = (alert.get("severity_tier") or "").upper()
        city = alert.get("city") or ""
        summary = alert.get("summary_localized") or alert.get("summary") or ""
        advice = alert.get("advice") or ""
        action = alert.get("suggested_action") or ""
        lines.append(f"\n[{sev}] {html.escape(city)}: {html.escape(summary)}")
        if advice:
            lines.append(f"Advice: {html.escape(advice)}")
        action_labels = {
            "switch_wet_weather_variant": "Switch to wet-weather plan on the webpage",
            "reconsider_leg": "Review this leg on the webpage",
            "monitor": "Monitor official advisories",
        }
        lines.append(f"Suggested: {action_labels.get(action, html.escape(action))}")
        if alert.get("translated") is False:
            lines.append("(Translation unavailable — showing English original)")
    lines.append("\nAll changes require confirmation on the Travel Guild webpage.")
    return "\n".join(lines)
