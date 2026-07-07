"""
test_inv_insurance_exclusion_precedence.py — Invariant: EXCLUSION MUST ALWAYS WIN

Money-safety invariant tested here
-----------------------------------
INSURANCE EXCLUSION-PRECEDENCE (agents/insurance_agent.py)

When a peril matches BOTH a COVER clause AND an EXCLUDE clause for the same policy:

  1. The headline decision (per-peril status) MUST be EXCLUDED — not COVERED, not
     UNDETERMINED, not SUB_LIMITED.  EXCLUDE always wins regardless of clause order.
  2. The EXCLUDE clause id MUST be enumerated in matched_clause_ids (never masked).
  3. The COVER clause id MUST ALSO appear in matched_clause_ids (transparency:
     the traveler deserves to know that a COVER clause exists but is overridden).
  4. The peril MUST appear in excluded_perils_summary so the caller cannot silently
     ignore the exclusion.
  5. The peril MUST NOT appear in covered_perils (the covered list must never absorb
     an excluded peril).
  6. The EXCLUDE clause id MUST be present in the matched_clause_ids of the
     excluded_perils_summary entry.
  7. All of the above must hold across multiple canonical Peril values (not just one).
  8. Ordering of clauses passed to _status_for_peril must not matter (EXCLUDE wins
     whether it is the first or last clause in the list).

Seeding strategy (var-0 / deterministic / no LLM / no network)
---------------------------------------------------------------
The real seeded _POLICY_CLAUSES has no natural COVER+EXCLUDE collision on the same
peril because the catalog was engineered to avoid redundancy.  To exercise the
invariant against REAL product code we:

  a) Call _status_for_peril() directly with a SYNTHETIC collision clause list
     (two canonical-Peril clauses: one COVER, one EXCLUDE, same peril_class). This
     tests the inner deterministic matcher in isolation.

  b) Monkeypatch ins._POLICY_CLAUSES at the test level to inject a synthetic EXCLUDE
     clause for a peril that already has a COVER clause in the seeded catalog
     (travel_delay / COV-DELAY-1), then call assess_coverage() end-to-end and
     verify the full output shape.  No product code is changed; the monkeypatch is
     test-local.

Tests are ADD-ONLY and do NOT modify any product file.  CI-safe: var-0, no network,
no LLM, no paid API.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(__file__))

from agents import insurance_agent as ins
from agents.insurance_agent import (
    ClauseType,
    CoverageStatus,
    _POLICY_CLAUSES,
    _status_for_peril,
    assess_coverage,
)
from core.contracts import Peril

# ---------------------------------------------------------------------------
# Shared synthetic clause factory — builds a minimal COVER + EXCLUDE collision
# for any canonical peril on a fictional policy id (never touches real seeds).
# ---------------------------------------------------------------------------

_SYNTH_POLICY = "P-SYNTH-COLLISION"
_SYNTH_COVER_PREFIX = "SYNTH-COV"
_SYNTH_EXCLUDE_PREFIX = "SYNTH-EXC"


def _collision_clauses(peril_value: str) -> list[dict]:
    """
    Return [COVER-clause, EXCLUDE-clause] for the given canonical peril_value.
    Stable clause_ids are deterministic (no random nonces).
    """
    safe = peril_value.replace("_", "-").upper()
    return [
        {
            "clause_id": f"{_SYNTH_COVER_PREFIX}-{safe}-1",
            "policy_id": _SYNTH_POLICY,
            "clause_type": ClauseType.COVER,
            "peril_class": peril_value,
            "sub_limit_cents": None,
            "condition_text": None,
            "clause_text": (
                f"Synthetic cover clause: {peril_value} is covered up to the policy limit."
            ),
            "provenance": "seed:synth-test",
            "source_url": "https://synth.test/",
        },
        {
            "clause_id": f"{_SYNTH_EXCLUDE_PREFIX}-{safe}-1",
            "policy_id": _SYNTH_POLICY,
            "clause_type": ClauseType.EXCLUDE,
            "peril_class": peril_value,
            "sub_limit_cents": None,
            "condition_text": None,
            "clause_text": (
                f"Synthetic exclusion clause: {peril_value} is excluded."
            ),
            "provenance": "seed:synth-test",
            "source_url": "https://synth.test/",
        },
    ]


# ===========================================================================
# 1. INNER-MATCHER TESTS — _status_for_peril() directly
# ===========================================================================

def test_inner_matcher_exclude_wins_over_cover_status():
    """
    INV-EP-1: _status_for_peril with a COVER+EXCLUDE collision → status == EXCLUDED.

    The governing status must be EXCLUDED regardless of which clause appears first
    in the input list.  This is the core precedence rule (EXCLUDE > everything).
    """
    clauses = _collision_clauses(Peril.WAR.value)
    result = _status_for_peril(Peril.WAR.value, clauses)
    assert result["status"] == CoverageStatus.EXCLUDED, (
        f"INV-EP-1: EXCLUDE must win over COVER for peril={Peril.WAR.value!r}; "
        f"got status={result['status']!r}"
    )


def test_inner_matcher_exclude_wins_over_cover_status_reversed_order():
    """
    INV-EP-1b: Clause order must not matter — EXCLUDE wins even when the COVER clause
    is listed BEFORE the EXCLUDE clause.
    """
    clauses_normal = _collision_clauses(Peril.WAR.value)   # COVER then EXCLUDE
    clauses_reversed = list(reversed(clauses_normal))       # EXCLUDE then COVER

    r1 = _status_for_peril(Peril.WAR.value, clauses_normal)
    r2 = _status_for_peril(Peril.WAR.value, clauses_reversed)

    assert r1["status"] == CoverageStatus.EXCLUDED, (
        f"INV-EP-1b (normal order): status={r1['status']!r}, expected EXCLUDED"
    )
    assert r2["status"] == CoverageStatus.EXCLUDED, (
        f"INV-EP-1b (reversed order): status={r2['status']!r}, expected EXCLUDED"
    )
    # Status must be the same regardless of order (deterministic).
    assert r1["status"] == r2["status"], (
        "INV-EP-1b: clause order must not change the status outcome"
    )


def test_inner_matcher_exclude_clause_id_enumerated_in_matched_ids():
    """
    INV-EP-2: The EXCLUDE clause id MUST appear in matched_clause_ids.
    Transparency: the exclusion that governs the decision must never be masked.
    """
    clauses = _collision_clauses(Peril.TRAVEL_DELAY.value)
    exc_id = f"{_SYNTH_EXCLUDE_PREFIX}-TRAVEL-DELAY-1"
    result = _status_for_peril(Peril.TRAVEL_DELAY.value, clauses)

    assert exc_id in result["matched_clause_ids"], (
        f"INV-EP-2: exclude clause_id={exc_id!r} must be in matched_clause_ids; "
        f"got {result['matched_clause_ids']}"
    )


def test_inner_matcher_cover_clause_id_also_enumerated():
    """
    INV-EP-3: The COVER clause id MUST ALSO appear in matched_clause_ids even though
    EXCLUDE overrides it.  The traveler must be able to see that a cover clause
    exists but is overridden — suppressing it would be a silent deception.
    """
    clauses = _collision_clauses(Peril.TRAVEL_DELAY.value)
    cov_id = f"{_SYNTH_COVER_PREFIX}-TRAVEL-DELAY-1"
    result = _status_for_peril(Peril.TRAVEL_DELAY.value, clauses)

    assert cov_id in result["matched_clause_ids"], (
        f"INV-EP-3: cover clause_id={cov_id!r} must ALSO be listed in matched_clause_ids "
        f"(transparency: COVER exists but is overridden by EXCLUDE); "
        f"got {result['matched_clause_ids']}"
    )


def test_inner_matcher_not_covered_not_undetermined():
    """
    INV-EP-4: The result must not be COVERED or UNDETERMINED — both are incorrect
    when an EXCLUDE clause matches.  COVERED would allow the traveler to believe
    they are insured; UNDETERMINED would hide the exclusion.
    """
    for peril in (Peril.WAR.value, Peril.PANDEMIC.value, Peril.TRAVEL_DELAY.value):
        clauses = _collision_clauses(peril)
        result = _status_for_peril(peril, clauses)
        assert result["status"] != CoverageStatus.COVERED, (
            f"INV-EP-4: peril={peril!r} must not be COVERED when EXCLUDE clause present; "
            f"got {result['status']!r}"
        )
        assert result["status"] != CoverageStatus.UNDETERMINED, (
            f"INV-EP-4: peril={peril!r} must not be UNDETERMINED when EXCLUDE clause present; "
            f"got {result['status']!r}"
        )


# ===========================================================================
# 2. END-TO-END TESTS — assess_coverage() with monkeypatched collision seed
# ===========================================================================
# We inject a synthetic EXCLUDE clause for travel_delay into _POLICY_CLAUSES.
# In the real seeded catalog, travel_delay has only COV-DELAY-1 (COVER) on P-STD-01.
# After injection, travel_delay hits BOTH COV-DELAY-1 AND the synthetic EXCLUDE,
# which exercises the full assess_coverage pipeline against a collision scenario.

# The injected clause id — deterministic, checked in assertions below.
_INJECTED_EXC_ID = "TEST-INV-EXC-DELAY-9901"
_INJECTED_EXC_CLAUSE = {
    "clause_id": _INJECTED_EXC_ID,
    "policy_id": "P-STD-01",
    "clause_type": ClauseType.EXCLUDE,
    "peril_class": Peril.TRAVEL_DELAY.value,
    "sub_limit_cents": None,
    "condition_text": None,
    "clause_text": (
        "Travel delay arising from a declared national emergency is excluded "
        "(synthetic invariant-test clause, not a real policy term)."
    ),
    "provenance": "seed:invariant-test-only",
    "source_url": "https://invariant-test.example/",
}


def test_assess_coverage_exclude_wins_headline_status(monkeypatch):
    """
    INV-EP-5 (end-to-end): with BOTH a COVER and EXCLUDE clause for the same peril,
    assess_coverage must return status == EXCLUDED for that peril.

    Scenario: travel_delay already has COV-DELAY-1 (COVER) in P-STD-01.
    We inject TEST-INV-EXC-DELAY-9901 (EXCLUDE, same peril) and verify EXCLUDE wins.
    """
    monkeypatch.setattr(ins, "_POLICY_CLAUSES", list(_POLICY_CLAUSES) + [_INJECTED_EXC_CLAUSE])
    result = assess_coverage(
        peril_set=[Peril.TRAVEL_DELAY.value],
        policy_id="P-STD-01",
        insured_trip_cost_cents=150000,
    )
    rec = next(r for r in result["per_peril"] if r["peril_class"] == Peril.TRAVEL_DELAY.value)

    assert rec["status"] == CoverageStatus.EXCLUDED, (
        f"INV-EP-5: EXCLUDE must win for travel_delay when both COVER (COV-DELAY-1) "
        f"and EXCLUDE ({_INJECTED_EXC_ID}) match; got status={rec['status']!r}"
    )


def test_assess_coverage_exclude_clause_id_in_per_peril_matched_ids(monkeypatch):
    """
    INV-EP-6 (end-to-end): the governing EXCLUDE clause id must be listed in
    per_peril[n].matched_clause_ids — never hidden from the output.
    """
    monkeypatch.setattr(ins, "_POLICY_CLAUSES", list(_POLICY_CLAUSES) + [_INJECTED_EXC_CLAUSE])
    result = assess_coverage(
        peril_set=[Peril.TRAVEL_DELAY.value],
        policy_id="P-STD-01",
        insured_trip_cost_cents=150000,
    )
    rec = next(r for r in result["per_peril"] if r["peril_class"] == Peril.TRAVEL_DELAY.value)

    assert _INJECTED_EXC_ID in rec["matched_clause_ids"], (
        f"INV-EP-6: EXCLUDE clause id {_INJECTED_EXC_ID!r} must appear in "
        f"matched_clause_ids; got {rec['matched_clause_ids']}"
    )


def test_assess_coverage_cover_clause_also_enumerated(monkeypatch):
    """
    INV-EP-7 (end-to-end): the COVER clause (COV-DELAY-1) must ALSO remain in
    matched_clause_ids — it is overridden but must not be suppressed from the output.
    """
    monkeypatch.setattr(ins, "_POLICY_CLAUSES", list(_POLICY_CLAUSES) + [_INJECTED_EXC_CLAUSE])
    result = assess_coverage(
        peril_set=[Peril.TRAVEL_DELAY.value],
        policy_id="P-STD-01",
        insured_trip_cost_cents=150000,
    )
    rec = next(r for r in result["per_peril"] if r["peril_class"] == Peril.TRAVEL_DELAY.value)

    assert "COV-DELAY-1" in rec["matched_clause_ids"], (
        f"INV-EP-7: original COVER clause 'COV-DELAY-1' must still appear in "
        f"matched_clause_ids even when overridden by EXCLUDE; "
        f"got {rec['matched_clause_ids']}"
    )


def test_assess_coverage_peril_in_excluded_perils_summary(monkeypatch):
    """
    INV-EP-8 (end-to-end): a COVER+EXCLUDE collision peril must appear in
    excluded_perils_summary — the first-class exclusion output the caller relies on.
    """
    monkeypatch.setattr(ins, "_POLICY_CLAUSES", list(_POLICY_CLAUSES) + [_INJECTED_EXC_CLAUSE])
    result = assess_coverage(
        peril_set=[Peril.TRAVEL_DELAY.value],
        policy_id="P-STD-01",
        insured_trip_cost_cents=150000,
    )
    summary_perils = [entry["peril_class"] for entry in result["excluded_perils_summary"]]

    assert Peril.TRAVEL_DELAY.value in summary_perils, (
        f"INV-EP-8: collision peril {Peril.TRAVEL_DELAY.value!r} must appear in "
        f"excluded_perils_summary; got {summary_perils}"
    )


def test_assess_coverage_exclude_id_in_summary_matched_clause_ids(monkeypatch):
    """
    INV-EP-9 (end-to-end): the EXCLUDE clause id must be enumerated in the
    excluded_perils_summary entry's matched_clause_ids.  This is the specific
    requirement: the EXCLUDE clause id is still listed in the output.
    """
    monkeypatch.setattr(ins, "_POLICY_CLAUSES", list(_POLICY_CLAUSES) + [_INJECTED_EXC_CLAUSE])
    result = assess_coverage(
        peril_set=[Peril.TRAVEL_DELAY.value],
        policy_id="P-STD-01",
        insured_trip_cost_cents=150000,
    )
    summary_entry = next(
        (e for e in result["excluded_perils_summary"]
         if e["peril_class"] == Peril.TRAVEL_DELAY.value),
        None,
    )

    assert summary_entry is not None, (
        f"INV-EP-9: {Peril.TRAVEL_DELAY.value!r} must be present in excluded_perils_summary"
    )
    assert _INJECTED_EXC_ID in summary_entry["matched_clause_ids"], (
        f"INV-EP-9: EXCLUDE clause id {_INJECTED_EXC_ID!r} must be listed in "
        f"excluded_perils_summary[travel_delay].matched_clause_ids; "
        f"got {summary_entry['matched_clause_ids']}"
    )


def test_assess_coverage_peril_absent_from_covered_perils(monkeypatch):
    """
    INV-EP-10 (end-to-end): the collision peril must NOT appear in covered_perils.
    A peril with an active EXCLUDE clause must never be surfaced as covered.
    """
    monkeypatch.setattr(ins, "_POLICY_CLAUSES", list(_POLICY_CLAUSES) + [_INJECTED_EXC_CLAUSE])
    result = assess_coverage(
        peril_set=[Peril.TRAVEL_DELAY.value],
        policy_id="P-STD-01",
        insured_trip_cost_cents=150000,
    )

    assert Peril.TRAVEL_DELAY.value not in result["covered_perils"], (
        f"INV-EP-10: peril with COVER+EXCLUDE collision must NOT appear in "
        f"covered_perils (EXCLUDE must win); covered_perils={result['covered_perils']}"
    )


# ===========================================================================
# 3. MULTI-PERIL REGRESSION — collision peril must not bleed into other perils
# ===========================================================================

def test_multi_peril_collision_does_not_bleed_to_other_perils(monkeypatch):
    """
    INV-EP-11: When travel_delay is the collision peril (EXCLUDED), other perils in
    the same assess_coverage call must still retain their correct status.

    Specifically: civil_unrest must still be EXCLUDED (via EXC-UNREST-1), and
    baggage_loss must still be COVERED (via COV-BAG-1).  Neither may absorb
    travel_delay's collision outcome.
    """
    monkeypatch.setattr(ins, "_POLICY_CLAUSES", list(_POLICY_CLAUSES) + [_INJECTED_EXC_CLAUSE])
    result = assess_coverage(
        peril_set=[Peril.TRAVEL_DELAY.value, Peril.CIVIL_UNREST.value, Peril.BAGGAGE_LOSS.value],
        policy_id="P-STD-01",
        insured_trip_cost_cents=120000,
    )
    status_by_peril = {r["peril_class"]: r["status"] for r in result["per_peril"]}

    # travel_delay: collision → EXCLUDED
    assert status_by_peril[Peril.TRAVEL_DELAY.value] == CoverageStatus.EXCLUDED, (
        f"INV-EP-11: travel_delay must be EXCLUDED; got {status_by_peril[Peril.TRAVEL_DELAY.value]!r}"
    )
    # civil_unrest: independent EXCLUDE clause (EXC-UNREST-1) still applies
    assert status_by_peril[Peril.CIVIL_UNREST.value] == CoverageStatus.EXCLUDED, (
        f"INV-EP-11: civil_unrest must still be EXCLUDED; got {status_by_peril[Peril.CIVIL_UNREST.value]!r}"
    )
    # baggage_loss: independent COVER clause (COV-BAG-1) — must not be contaminated
    assert status_by_peril[Peril.BAGGAGE_LOSS.value] == CoverageStatus.COVERED, (
        f"INV-EP-11: baggage_loss must still be COVERED; got {status_by_peril[Peril.BAGGAGE_LOSS.value]!r}"
    )


# ===========================================================================
# 4. EXPLAIN_EXCLUSION SURFACE — collision peril must surface EXCLUDED headline
# ===========================================================================

def test_explain_exclusion_reports_excluded_status_for_collision(monkeypatch):
    """
    INV-EP-12 (surface): explain_exclusion() on a collision peril must produce a
    headline with status == EXCLUDED and must include the EXCLUDE clause id —
    never report COVERED, UNDETERMINED, or omit the clause.
    """
    monkeypatch.setattr(ins, "_POLICY_CLAUSES", list(_POLICY_CLAUSES) + [_INJECTED_EXC_CLAUSE])
    assessment = assess_coverage(
        peril_set=[Peril.TRAVEL_DELAY.value, Peril.BAGGAGE_LOSS.value],
        policy_id="P-STD-01",
        insured_trip_cost_cents=150000,
    )
    explanation = ins.explain_exclusion(assessment, Peril.TRAVEL_DELAY.value, region="Test Region")

    assert explanation["status"] == CoverageStatus.EXCLUDED, (
        f"INV-EP-12: explain_exclusion status must be EXCLUDED for collision peril; "
        f"got {explanation['status']!r}"
    )
    # The headline must reference the EXCLUDE clause id (from matched_clauses or headline text)
    # — the EXCLUDE clause must be surfaced, not hidden.
    assert _INJECTED_EXC_ID in explanation["headline"] or any(
        c["clause_id"] == _INJECTED_EXC_ID for c in explanation["matched_clauses"]
    ), (
        f"INV-EP-12: EXCLUDE clause id {_INJECTED_EXC_ID!r} must appear in the "
        f"explanation headline or matched_clauses; "
        f"headline={explanation['headline']!r}, "
        f"matched_clauses={explanation['matched_clauses']}"
    )
    # The headline must not describe the EXCLUDED peril itself as covered.
    # Note: explain_exclusion may emit "you are covered for X, NOT for <excluded-peril>"
    # as a DC0 contrast phrase — that is legitimate because it contrasts a DIFFERENT
    # covered peril against the excluded one.  What we must rule out is any phrasing
    # that claims travel_delay itself is covered.
    headline_lower = explanation["headline"].lower()
    # These phrases would only appear if the excluded peril was incorrectly described
    # as covered (the word "EXCLUDES" / "NOT for" must dominate).
    assert "EXCLUDES" in explanation["headline"] or "NOT for" in explanation["headline"], (
        f"INV-EP-12: headline must clearly signal exclusion (EXCLUDES / NOT for); "
        f"got headline={explanation['headline']!r}"
    )
    # The headline must not claim the excluded peril (travel_delay) is itself covered,
    # i.e., must not contain 'travel delay is covered' or 'travel delay will pay'.
    assert "travel delay is covered" not in headline_lower, (
        f"INV-EP-12: headline must not claim 'travel delay is covered'; "
        f"got headline={explanation['headline']!r}"
    )
    assert "travel delay will pay" not in headline_lower, (
        f"INV-EP-12: headline must not claim 'travel delay will pay'; "
        f"got headline={explanation['headline']!r}"
    )


# ===========================================================================
# 5. CLAUSE-PRECEDENCE ORDER EXHAUSTIVE CHECK (all combinations)
# ===========================================================================

def test_inner_matcher_exclude_beats_all_other_clause_types():
    """
    INV-EP-13: EXCLUDE must win in every possible two-clause combination:
      EXCLUDE + COVER      → EXCLUDED
      EXCLUDE + SUBLIMIT   → EXCLUDED
      EXCLUDE + CONDITION  → EXCLUDED

    Uses a canonical peril value (Peril.BAGGAGE_LOSS) with a fictional policy.
    Verifies the _CLAUSE_PRECEDENCE table is wired correctly for all three combos.
    """
    peril = Peril.BAGGAGE_LOSS.value
    safe = peril.replace("_", "-").upper()

    exclude_clause = {
        "clause_id": f"SYNTH-EXC-{safe}-X",
        "policy_id": _SYNTH_POLICY,
        "clause_type": ClauseType.EXCLUDE,
        "peril_class": peril,
        "sub_limit_cents": None,
        "condition_text": None,
        "clause_text": "Excluded.",
        "provenance": "seed:synth-test",
        "source_url": "https://synth.test/",
    }

    for competing_type in (ClauseType.COVER, ClauseType.SUBLIMIT, ClauseType.CONDITION):
        other_clause = {
            "clause_id": f"SYNTH-{competing_type}-{safe}-X",
            "policy_id": _SYNTH_POLICY,
            "clause_type": competing_type,
            "peril_class": peril,
            "sub_limit_cents": 999999 if competing_type == ClauseType.SUBLIMIT else None,
            "condition_text": "Some condition." if competing_type == ClauseType.CONDITION else None,
            "clause_text": f"Synthetic {competing_type} clause.",
            "provenance": "seed:synth-test",
            "source_url": "https://synth.test/",
        }

        # Test both orderings to prove order-independence.
        for clauses in ([exclude_clause, other_clause], [other_clause, exclude_clause]):
            result = _status_for_peril(peril, clauses)
            assert result["status"] == CoverageStatus.EXCLUDED, (
                f"INV-EP-13: EXCLUDE must beat {competing_type} for peril={peril!r}; "
                f"clause order={[c['clause_type'] for c in clauses]}; "
                f"got status={result['status']!r}"
            )
            assert exclude_clause["clause_id"] in result["matched_clause_ids"], (
                f"INV-EP-13: EXCLUDE clause id must be in matched_clause_ids; "
                f"got {result['matched_clause_ids']}"
            )


if __name__ == "__main__":
    import subprocess
    import sys
    raise SystemExit(
        subprocess.call([
            sys.executable, "-m", "pytest", __file__,
            "-v", "-p", "no:xdist", "-p", "no:cacheprovider",
        ])
    )
