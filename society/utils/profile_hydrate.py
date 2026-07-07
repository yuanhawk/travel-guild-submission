"""profile_hydrate.py — #40 IAM: hydrate a trip_request's identity defaults from a saved
user profile.

ANONYMOUS-FIRST: with no profile (the default), the request is returned unchanged — the
society never requires an account. When a returning user has an opt-in profile (passports,
home currency, dietary/vibe prefs, held vaccine/insurance certs), we fill ONLY the fields the
current request leaves blank. The explicit request ALWAYS wins (explicit > profile > empty),
so saving a profile never silently overrides what the traveller asked for this trip.

Pure + deterministic (no LLM/clock/network) -> var-0: the same (request, profile) always yields
the same hydrated request. The held passports feed compliance.gate_leg_best_passport (#70).
"""

from __future__ import annotations

import copy

# Identity fields a saved profile may carry into a trip when the request omits them.
PROFILE_FIELDS: tuple[str, ...] = (
    "nationality",
    "passports",
    "home_currency",
    "dietary",
    "vibe",
    "held_vaccine_certs",
    "held_certs",
    "held_insurance",
)


def _present(v: object) -> bool:
    """True iff the request already carries a real value for the field (so we must NOT override)."""
    return v is not None and v != "" and v != [] and v != {}


def hydrate_trip_from_profile(trip_request: dict, profile: dict | None) -> dict:
    """Return a copy of trip_request with blank identity fields filled from `profile`.

    Anonymous (profile is None/empty) -> an unchanged copy. Explicit request values are never
    overridden. Pure; does not mutate the input.
    """
    out = dict(trip_request)
    if not profile:
        return out
    for field in PROFILE_FIELDS:
        if field in profile and not _present(out.get(field)):
            # deepcopy so a hydrated list/dict can't be mutated downstream back into a shared
            # in-memory profile dict (the audit's aliasing footgun); harmless + cheap per trip.
            out[field] = copy.deepcopy(profile[field])
    return out
