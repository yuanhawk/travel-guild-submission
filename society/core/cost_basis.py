"""
cost_basis.py — task #42 PART A: the per-cost-line BASIS DISCLOSURE layer.

WHAT THIS IS
------------
A PURE, stdlib-only builder that stamps every cost line in the negotiation
``result`` with an honest *basis discriminator* + a human label. It mirrors the
#37 booking_links pattern: a CLOSED discriminator set, a label map, and an
additive ``make_basis(...)`` attach helper. Domain numbers are NEVER touched —
this module only adds two deterministic STRING keys per cost line.

THE HONESTY CONTRACT (#42 PART A / §0 fail-honest)
--------------------------------------------------
Each cost line declares HOW its number was arrived at, conservatively:

    ucp_prepaid | deterministic_estimate | handoff | unknown

  * ``ucp_prepaid`` — a real merchant checkout happened (result["checkout_id"]
    truthy AND outcome=="success"). Only then may we say "prepaid".
  * ``deterministic_estimate`` — a number we computed from seeded data (e.g.
    visa/vaccine/premium FEES). Honest "estimate", never a live quote.
  * ``handoff`` — we assert NO price; the traveler books directly (transport
    durations only; attractions/restaurants handed off via #37).
  * ``unknown`` — basis cannot be conservatively established → treat as
    indicative only. This is also the safe FALLBACK on any bad input.

We NEVER imply a single guaranteed total: the package total is an integer sum of
mixed-basis legs, and the UI relabels it to say so.

THE VARIANCE-0 CONTRACT (var-0)
-------------------------------
Only deterministic STRINGS are added to ``result``. No integer cents are
recomputed or altered. NO wall-clock, NO random, NO I/O, NO catalog/poi/OSM
import (stdlib-only) → ``result`` stays byte-identical across re-runs.
"""

from __future__ import annotations

from typing import Any

# ---------------------------------------------------------------------------
# Allowed cost bases (the closed honesty discriminator set).
# ---------------------------------------------------------------------------
BASIS_UCP_PREPAID = "ucp_prepaid"
BASIS_DETERMINISTIC_ESTIMATE = "deterministic_estimate"
BASIS_HANDOFF = "handoff"
BASIS_UNKNOWN = "unknown"

ALLOWED_BASES = frozenset({
    BASIS_UCP_PREPAID,
    BASIS_DETERMINISTIC_ESTIMATE,
    BASIS_HANDOFF,
    BASIS_UNKNOWN,
})

# Human-readable label per basis (the UI renders these next to a number).
_BASIS_LABEL: dict[str, str] = {
    BASIS_UCP_PREPAID: "Prepaid via secure checkout",
    BASIS_DETERMINISTIC_ESTIMATE: "Estimated from seeded data (not a live quote)",
    BASIS_HANDOFF: "You book directly — no price asserted",
    BASIS_UNKNOWN: "Basis unknown — treat as indicative only",
}


def make_basis(basis: Any) -> dict[str, str]:
    """Return the additive {cost_basis, cost_basis_label} pair for ``basis``.

    Conservative + NEVER raises: an unknown/bad basis falls back to
    BASIS_UNKNOWN (never a KeyError), so a caller mistake degrades to the
    honest "indicative only" label rather than crashing the result.
    """
    # Guard: an unhashable input (list/dict) would raise on `in frozenset`, so
    # restrict the membership test to str — anything else degrades to UNKNOWN.
    b = basis if (isinstance(basis, str) and basis in ALLOWED_BASES) else BASIS_UNKNOWN
    return {"cost_basis": b, "cost_basis_label": _BASIS_LABEL[b]}
