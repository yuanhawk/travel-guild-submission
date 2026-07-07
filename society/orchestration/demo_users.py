"""demo_users.py — pre-seeded demo users + persona presets for the dashboard.

Slice 4: judges log in AS a pre-seeded demo user (NO register/signup — this is NOT
a full IAM). Each user carries a profile (persona, nationality, display currency,
SIMULATED wallet) that the server MERGES into a trip request at the I/O boundary,
BEFORE the deterministic core — so the core still receives explicit fields and
var-0 holds (the DB is never read inside negotiate()/_request_digest).

Persona is a PRESET BUNDLE that pre-fills EMPTY selections (interests/dietary/pace)
for ease of choice — it is NOT a scoring weight (this is the honest reframe of the
old #35). It is deliberately kept SEPARATE from the orchestrator's transport-only
`persona` ("comfort"/"default"): a demo persona expands to its preset effects, it
does not overwrite the transport persona (which stays text-driven).
"""
from __future__ import annotations

from typing import Any

# 5 demo users — varied nationality + display currency so the demo shows visa/
# compliance variance AND currency-display variance. $5,000 SIMULATED wallet each.
DEMO_USERS: list[dict[str, Any]] = [
    {"user_id": "demo-mei",   "display_name": "Mei Tan",      "persona": "foodie",
     "nationality": "SG", "home_currency": "SGD", "wallet_balance_cents": 500000},
    {"user_id": "demo-alex",  "display_name": "Alex Rivera",  "persona": "hiker",
     "nationality": "US", "home_currency": "USD", "wallet_balance_cents": 500000},
    {"user_id": "demo-priya", "display_name": "Priya Sharma", "persona": "culture",
     "nationality": "GB", "home_currency": "GBP", "wallet_balance_cents": 500000},
    {"user_id": "demo-lena",  "display_name": "Lena Fischer",  "persona": "family",
     "nationality": "DE", "home_currency": "EUR", "wallet_balance_cents": 500000},
    {"user_id": "demo-jack",  "display_name": "Jack Wilson",   "persona": "budget",
     "nationality": "AU", "home_currency": "AUD", "wallet_balance_cents": 500000},
]

# Persona → PRESET. Pre-fills only EMPTY request fields (ease of selection), never a
# weight/score. interests feed the day-planner; pace its density; dietary the dining
# filter. Kept lowercase to match the day-planner's interest/term matching.
# avoid_lodging_types / prefer_lodging_types are likewise plain string-list membership
# fields (not numeric weights) — same honesty framing as interests/dietary above; only
# family/budget carry a lodging-type signal (no invented signal for hiker/culture).
# dining_tier is a single 3-value enum (authentic/cheap/family) — the day-planner's
# SECONDARY meal-reorder signal (agents/day_planner_agent.py:_DINING_TIER_RANK); never a
# filter, never a score.
PERSONA_PRESETS: dict[str, dict[str, Any]] = {
    "foodie":  {"interests": ["food", "markets", "dining"],           "pace": "moderate",
                "dining_tier": "authentic"},
    "hiker":   {"interests": ["hiking", "nature", "outdoors"],        "pace": "active"},
    "culture": {"interests": ["museums", "heritage", "history"],     "pace": "moderate"},
    "family":  {"interests": ["family-friendly", "parks", "gardens"], "pace": "relaxed",
                "avoid_lodging_types":  ["hostel", "motel"],
                "prefer_lodging_types": ["apartment", "guest_house"],
                "dining_tier": "family"},
    "budget":  {"interests": ["free", "markets", "walking"],          "pace": "active",
                "prefer_lodging_types": ["hostel", "guest_house"],
                "dining_tier": "cheap"},
}


def merge_user_prefs(
    store,
    user_id: str,
    new_prefs: dict | None,
    *,
    display_name: str | None = None,
    persona: str | None = None,
    nationality: str | None = None,
    home_currency: str | None = None,
) -> dict | None:
    """Merge new_prefs into an EXISTING demo user's prefs dict and persist via
    store.upsert_demo_user, preserving every scalar field not explicitly overridden.

    This is the ONE prefs-merge/persistence path — both PUT /preferences
    (server.py) and the Telegram /start deep-link handler (telegram_companion.py)
    call this, so there is never a second independent "merge a key into
    user.prefs" implementation that could drift out of sync.

    No create path (matches the no-register invariant): returns None if user_id
    does not correspond to a known/existing demo user, rather than upserting a
    brand-new row.
    """
    if not user_id:
        return None
    try:
        existing = store.get_user(user_id)
    except Exception:  # noqa: BLE001
        existing = None
    if not existing:
        return None
    store.upsert_demo_user({
        "user_id": user_id,
        "display_name": display_name if display_name is not None else existing.get("display_name"),
        "persona": persona if persona is not None else existing.get("persona"),
        "nationality": nationality if nationality is not None else existing.get("nationality"),
        "home_currency": home_currency if home_currency is not None else existing.get("home_currency"),
        # Wallet is the SIMULATED $5k demo invariant — never editable via prefs merge.
        "wallet_balance_cents": existing.get("wallet_balance_cents"),
        "prefs": {**(existing.get("prefs") or {}), **(new_prefs or {})},
    })
    return store.get_user(user_id)


def seed_demo_users(store) -> None:
    """Idempotent: upsert the 5 demo users. Safe to re-run on every startup
    (ON CONFLICT updates in place — never duplicates, never wipes saved trips)."""
    for row in DEMO_USERS:
        try:
            store.upsert_demo_user(dict(row))
        except Exception:  # noqa: BLE001 — seeding must never crash startup
            pass


def _empty(v: Any) -> bool:
    return v is None or v == "" or v == [] or v == {}


def apply_user_prefs(body: dict, store) -> dict:
    """MERGE a known demo user's profile into the request body, IN PLACE, BEFORE the
    deterministic core. Precedence: explicit per-request value > profile > preset >
    system default. Anonymous / unknown user_id → NO-OP (body byte-identical → var-0).

    Sets only request-level fields the core already consumes as explicit inputs:
      nationality, home_currency (display-only), wallet_balance_cents, and the persona
      PRESET pre-fills (interests/dietary/pace). It does NOT set the transport `persona`.
    Returns the (possibly mutated) body for convenience.
    """
    if not isinstance(body, dict):
        return body
    uid = body.get("user_id")
    if not uid:
        return body
    try:
        user = store.get_user(uid)
    except Exception:  # noqa: BLE001
        user = None
    if not user:
        return body  # unknown/anonymous → unchanged

    # Profile scalars — fill only when the request did not specify them (explicit wins).
    if _empty(body.get("nationality")) and user.get("nationality"):
        body["nationality"] = user["nationality"]
    if _empty(body.get("home_currency")) and user.get("home_currency"):
        body["home_currency"] = user["home_currency"]
    if _empty(body.get("wallet_balance_cents")) and user.get("wallet_balance_cents"):
        body["wallet_balance_cents"] = int(user["wallet_balance_cents"])

    # Persona PRESET → pre-fill EMPTY interests/dietary/pace (ease of selection, not a
    # weight). A user prefs override (stored prefs_json) wins over the canned preset.
    preset = dict(PERSONA_PRESETS.get((user.get("persona") or "").lower(), {}))
    overrides = user.get("prefs") or {}
    preset.update({k: v for k, v in overrides.items() if k in ("interests", "dietary", "pace")})
    for key in ("interests", "dietary", "pace", "avoid_lodging_types",
                "prefer_lodging_types", "dining_tier"):
        if key in preset and _empty(body.get(key)):
            body[key] = preset[key]

    # STRUCTURED /negotiate path: legs are already present and the core reads per-leg
    # interests/dietary/pace (the top-level fields above are consumed only by the text
    # parser). So also pre-fill EMPTY leg fields here — mirrors negotiate_from_text's
    # per-leg threading. Harmless on the text path (no legs in the body at merge time).
    legs = body.get("legs")
    if isinstance(legs, list):
        for leg in legs:
            if isinstance(leg, dict):
                for key in ("interests", "dietary", "pace", "avoid_lodging_types",
                            "prefer_lodging_types", "dining_tier"):
                    if key in preset and _empty(leg.get(key)):
                        leg[key] = preset[key]
    return body
