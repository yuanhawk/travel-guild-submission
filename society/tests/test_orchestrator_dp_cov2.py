"""
test_orchestrator_dp_cov2.py — Coverage for the orchestrator negotiation core.

Target sub-area (orchestration/orchestrator.py):
  - _negotiate_dp()          — R0b/R0c/R0d full DP flow + no_fit infeasibility
                               (lines ~2840-3266)
  - _negotiate_greedy()      — proportional-split + greedy proposals
                               (lines ~3128-3232)
  - _run_negotiation_rounds() — two-phase booking (CHECK → Transport → Critic →
                               COMMIT), ALL-OR-NONE rule, transport-infeasible
                               block, Critic-rejected re-plan, Budget-veto
                               re-plan, commit-time veto re-plan,
                               cannot_price/unavailable hard terminal,
                               no-Critic direct-commit, commit_failed terminal
                               (lines ~3440-4369)
  - _do_commit()             — irreversible commit + commit_failed conversion
                               (lines ~3387-3434)
  - _primary_dest_token()    — ISO2 / city-slug fallback (lines ~4376-4419)
  - _mint_booking_ref()      — destination-aware re-mint (lines ~4421-4448)

DETERMINISM (var-0):
  These tests drive the orchestrator's INTERNAL negotiation methods DIRECTLY,
  using a TravelOrchestrator subclass that overrides the agent-dispatch helpers
  (_call_planner / _call_budget_check / _call_budget_commit / _call_critic /
  _call_transport / _call_day_planner / _propose_with_area_ladder /
  _gather_candidates_for_dp) with SCRIPTED, in-process responses.

  This is the same house pattern as test_orchestrator_partial.py (see
  `_RaisingOrch`, `_CommitRaises`), but extended to script the full
  CHECK/Transport/Critic/COMMIT sequence per round. No live LLM, no network,
  no merchant transport, no real catalog, no wall-clock, no random.

  Driving the internal methods directly (rather than full negotiate()) avoids
  the gate-ordering / area-staging / merchant-tool-name drift risks: the
  Destination/Compliance/Health/Risk/Fraud gates and the proportional-split
  Planner never run, so the test reaches the targeted round-loop branches
  with no false cannot_satisfy from a missing mandatory agent.

  The pure helpers (_primary_dest_token, _mint_booking_ref) are exercised as
  pure functions (no instance state needed).
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(__file__))

import pytest  # noqa: E402

from orchestration.orchestrator import TravelOrchestrator  # noqa: E402


# ===========================================================================
# Scripted orchestrator — overrides the dispatch helpers with canned responses
# ===========================================================================

class _ScriptedOrchestrator(TravelOrchestrator):
    """
    A TravelOrchestrator whose agent-dispatch helpers return SCRIPTED, in-process
    responses (no clients, no network). Each override pops from a per-skill list
    (so multi-round flows can vary their answers), repeating the last entry when
    a list is exhausted (mirrors SequencedMockTransport).
    """

    def __init__(
        self,
        *,
        planner_legs: list[dict] | None = None,
        planner_dp_used: bool = False,
        accommodation_scripts: dict[str, list[dict]] | None = None,
        budget_check_scripts: list[dict] | None = None,
        budget_commit_scripts: list[dict] | None = None,
        critic_scripts: list[dict | None] | None = None,
        transport_scripts: list[dict | None] | None = None,
        day_planner_result: dict | None = None,
        gather_scripts: dict[str, list[dict]] | None = None,
        gather_reasons: dict[str, str] | None = None,
    ) -> None:
        super().__init__()
        self._planner_legs = planner_legs or []
        self._planner_dp_used = planner_dp_used
        # accommodation_scripts: leg_id -> list of acc_result dicts (each call pops)
        self._acc_scripts = accommodation_scripts or {}
        self._acc_calls: dict[str, int] = {}
        self._budget_check_scripts = list(budget_check_scripts or [])
        self._budget_check_i = 0
        self._budget_commit_scripts = list(budget_commit_scripts or [])
        self._budget_commit_i = 0
        self._critic_scripts = list(critic_scripts or [])
        self._critic_i = 0
        self._transport_scripts = list(transport_scripts or [])
        self._transport_i = 0
        self._day_planner_result = day_planner_result
        self._gather_scripts = gather_scripts or {}
        self._gather_reasons_seed = gather_reasons or {}
        # Spy log of dispatch calls in ORDER (for ordering assertions).
        self.call_order: list[str] = []

    # --- planner --------------------------------------------------------
    def _call_planner(self, payload: dict) -> dict:  # type: ignore[override]
        self.call_order.append("planner")
        return {"legs": list(self._planner_legs), "dp_allocation": self._planner_dp_used}

    # --- DP candidate gather -------------------------------------------
    def _gather_candidates_for_dp(self, *, leg_id, city, **kwargs):  # type: ignore[override]
        if not hasattr(self, "_gather_reasons"):
            self._gather_reasons = {}
        self._gather_reasons[leg_id] = self._gather_reasons_seed.get(leg_id, "no_inventory")
        return list(self._gather_scripts.get(leg_id, []))

    # --- accommodation greedy proposal ---------------------------------
    def _propose_with_area_ladder(self, *, leg_meta, target_areas, area_stage,  # type: ignore[override]
                                  leg_id, max_cents):
        self.call_order.append(f"acc:{leg_id}")
        i = self._acc_calls.get(leg_id, 0)
        seq = self._acc_scripts.get(leg_id, [])
        if not seq:
            return {"fit": "no_fit"}
        if i >= len(seq):
            i = len(seq) - 1
        self._acc_calls[leg_id] = i + 1
        return seq[i]

    # --- budget check ---------------------------------------------------
    def _call_budget_check(self, payload: dict) -> dict:  # type: ignore[override]
        self.call_order.append("budget.check")
        if not self._budget_check_scripts:
            raise RuntimeError("no budget_check script")
        i = min(self._budget_check_i, len(self._budget_check_scripts) - 1)
        self._budget_check_i += 1
        entry = self._budget_check_scripts[i]
        if isinstance(entry, BaseException):
            raise entry
        return entry

    # --- budget commit --------------------------------------------------
    def _call_budget_commit(self, payload: dict) -> dict:  # type: ignore[override]
        self.call_order.append("budget.commit")
        if not self._budget_commit_scripts:
            raise RuntimeError("no budget_commit script")
        i = min(self._budget_commit_i, len(self._budget_commit_scripts) - 1)
        self._budget_commit_i += 1
        return self._budget_commit_scripts[i]

    # --- critic ---------------------------------------------------------
    def _call_critic(self, payload: dict):  # type: ignore[override]
        self.call_order.append("critic")
        if not self._critic_scripts:
            return None
        i = min(self._critic_i, len(self._critic_scripts) - 1)
        self._critic_i += 1
        return self._critic_scripts[i]

    # --- transport ------------------------------------------------------
    def _call_transport(self, legs, persona="default", overland_only=False):  # type: ignore[override]
        self.call_order.append("transport")
        if not self._transport_scripts:
            return None
        i = min(self._transport_i, len(self._transport_scripts) - 1)
        self._transport_i += 1
        entry = self._transport_scripts[i]
        if isinstance(entry, BaseException):
            raise entry
        return entry

    # --- day planner ----------------------------------------------------
    def _call_day_planner(self, legs):  # type: ignore[override]
        self.call_order.append("day_planner")
        return self._day_planner_result

    # --- _attach_secondary is OFF the money path; stub to a no-op so a DP
    #     success does not pull in allocator/Critic machinery we are not
    #     scripting (keeps the test focused on the round-loop core).
    def _attach_secondary(self, *, result, **kwargs):  # type: ignore[override]
        return result


# ===========================================================================
# Fixture builders
# ===========================================================================

def _leg(leg_id: str, city: str, per_leg_budget_cents: int, **extra) -> dict:
    leg = {
        "leg_id": leg_id,
        "city": city,
        "checkin": "2026-12-01",
        "checkout": "2026-12-04",
        "adults": 1,
        "per_leg_budget_cents": per_leg_budget_cents,
    }
    leg.update(extra)
    return leg


def _proposal(hotel_id: str, total_cents: int, area: str = "") -> dict:
    return {
        "fit": "ok",
        "proposal": {
            "hotel_id": hotel_id,
            "total_cents": total_cents,
            "review_score": 8.0,
            "star_rating": 4.0,
            "amenities": [],
            "title": hotel_id,
            "area": area,
            "ranking_source": "test",
            "provenance": "merchant",
        },
    }


def _check_ok(checkout_id: str = "co-1") -> dict:
    return {"decision": "check_ok", "checkout_id": checkout_id}


def _check_veto(ceiling: int) -> dict:
    return {"decision": "veto", "veto_reason": "over_budget",
            "budget_ceiling_cents": ceiling}


def _commit_accept(booking_ref: str = "BK-co-local-abc123",
                   total_cents: int = 10000,
                   checkout_id: str = "co-1") -> dict:
    return {"decision": "accept", "booking_ref": booking_ref,
            "total_cents": total_cents, "checkout_id": checkout_id}


def _commit_veto(ceiling: int) -> dict:
    return {"decision": "veto", "veto_reason": "repriced",
            "budget_ceiling_cents": ceiling}


def _commit_needs_consent(message: str = "Buyer consent required to proceed.") -> dict:
    return {"decision": "needs_consent", "consent_message": message,
            "checkout_id": "co-1"}


def _commit_needs_mandate(message: str = "AP2 mandate required to proceed.") -> dict:
    return {"decision": "needs_mandate", "mandate_message": message,
            "checkout_id": "co-1"}


def _critic_verified() -> dict:
    return {"decision": "verified", "violations": [], "quality_score": 0.9}


def _critic_rejected(leg_id: str) -> dict:
    return {
        "decision": "rejected",
        "quality_score": 0.1,
        "violations": [
            {"code": "QUALITY_FLOOR", "leg_id": leg_id, "route_to": "accommodation",
             "detail": "below quality floor"},
        ],
    }


def _transport_ok() -> dict:
    return {"infeasible_edges": [], "edges": []}


def _transport_infeasible() -> dict:
    return {
        "infeasible_edges": [{"from_leg": "leg-0", "to_leg": "leg-1"}],
        "edges": [],
    }


def _run_rounds(orch: _ScriptedOrchestrator, *, legs: list[dict],
                proposals: dict, total_budget_cents: int = 100000,
                effective_ceiling: int | None = None,
                lodging_budget_cents: int | None = None) -> dict:
    """Drive _run_negotiation_rounds with leg/ceiling/proposal state derived
    from `legs`. proposals: leg_id -> proposal dict | None."""
    if effective_ceiling is None:
        effective_ceiling = total_budget_cents
    ceilings = {leg["leg_id"]: leg["per_leg_budget_cents"] for leg in legs}
    leg_meta = {leg["leg_id"]: leg for leg in legs}
    # needed by the day-planner off-path branch
    orch._trip_request_legs = [
        {"city": leg["city"], "dest_country": leg.get("dest_country", "")}
        for leg in legs
    ]
    orch._trip_id = "test-trip"
    return orch._run_negotiation_rounds(
        user_id="u1",
        total_budget_cents=total_budget_cents,
        effective_ceiling=effective_ceiling,
        idempotency_key="trip-test",
        negotiation_log=[],
        legs=legs,
        ceilings=ceilings,
        proposals=proposals,
        leg_meta=leg_meta,
        target_areas={},
        area_stage={},
        lodging_budget_cents=lodging_budget_cents,
    )


def _log_actions(result: dict) -> list[str]:
    return [e.get("action", "") for e in result.get("negotiation_log", [])]


# ===========================================================================
# _run_negotiation_rounds — two-phase booking ORDER (CHECK→Transport→Critic→COMMIT)
# ===========================================================================

def test_run_negotiation_rounds_two_phase_booking_order() -> None:
    legs = [_leg("leg-0", "tokyo", 30000), _leg("leg-1", "osaka", 30000)]
    proposals = {
        "leg-0": _proposal("tokyo-h", 12000)["proposal"],
        "leg-1": _proposal("osaka-h", 11000)["proposal"],
    }
    orch = _ScriptedOrchestrator(
        budget_check_scripts=[_check_ok()],
        budget_commit_scripts=[_commit_accept(total_cents=23000)],
        critic_scripts=[_critic_verified()],
        transport_scripts=[_transport_ok()],
    )
    result = _run_rounds(orch, legs=legs, proposals=proposals)

    assert result["outcome"] == "success", result
    assert result.get("booking_ref"), "expected booking_ref on success"
    # Two-phase order: CHECK -> Transport -> Critic -> COMMIT.
    order = [c for c in orch.call_order
             if c in ("budget.check", "transport", "critic", "budget.commit")]
    assert order == ["budget.check", "transport", "critic", "budget.commit"], order
    assert _log_actions(result)[-1] == "accept"
    assert len(result.get("legs", [])) == 2


# ===========================================================================
# ALL-OR-NONE rule — no_fit blocks BEFORE budget.check
# ===========================================================================

def test_run_negotiation_rounds_partial_no_fit_returns_cannot_satisfy() -> None:
    legs = [_leg("leg-0", "tokyo", 30000), _leg("leg-1", "osaka", 30000)]
    proposals = {
        "leg-0": _proposal("tokyo-h", 12000)["proposal"],
        "leg-1": None,  # no_fit
    }
    orch = _ScriptedOrchestrator(
        budget_check_scripts=[_check_ok()],   # must NOT be reached
        transport_scripts=[_transport_ok()],
        critic_scripts=[_critic_verified()],
    )
    result = _run_rounds(orch, legs=legs, proposals=proposals)

    assert result["outcome"] == "cannot_satisfy", result
    assert not result.get("booking_ref")
    assert "budget.check" not in orch.call_order, (
        "ALL-OR-NONE: budget.check must NOT be called with a no_fit leg"
    )
    assert "cannot_satisfy_partial_legs" in _log_actions(result)


# ===========================================================================
# Transport infeasible → cannot_satisfy (no Critic, no commit)
# ===========================================================================

def test_run_negotiation_rounds_transport_infeasible_blocks() -> None:
    legs = [_leg("leg-0", "tokyo", 30000), _leg("leg-1", "osaka", 30000)]
    proposals = {
        "leg-0": _proposal("tokyo-h", 12000)["proposal"],
        "leg-1": _proposal("osaka-h", 11000)["proposal"],
    }
    orch = _ScriptedOrchestrator(
        budget_check_scripts=[_check_ok()],
        transport_scripts=[_transport_infeasible()],
        critic_scripts=[_critic_verified()],   # must NOT be reached
        budget_commit_scripts=[_commit_accept()],
    )
    result = _run_rounds(orch, legs=legs, proposals=proposals)

    assert result["outcome"] == "cannot_satisfy", result
    assert "critic" not in orch.call_order, "Critic must NOT run after transport-infeasible"
    assert "budget.commit" not in orch.call_order, "no commit after transport-infeasible"
    assert "transport_infeasible" in _log_actions(result)


# ===========================================================================
# M3 — a Transport agent call that RAISES must fail conservative, not crash
# ===========================================================================

def test_run_negotiation_rounds_transport_raises_fails_conservative() -> None:
    """M3: an unguarded Transport call previously let a single agent failure
    (timeout / RPC error / failed task) propagate out of the negotiation loop
    entirely, surfacing a raw server_error instead of the honest conservative
    terminal used for a malformed transport verdict. Must now degrade to the
    SAME cannot_satisfy terminal, with the round's checkout simply abandoned
    (no commit attempted) and NO exception escaping."""
    legs = [_leg("leg-0", "tokyo", 30000), _leg("leg-1", "osaka", 30000)]
    proposals = {
        "leg-0": _proposal("tokyo-h", 12000)["proposal"],
        "leg-1": _proposal("osaka-h", 11000)["proposal"],
    }
    orch = _ScriptedOrchestrator(
        budget_check_scripts=[_check_ok()],
        transport_scripts=[RuntimeError("A2A RPC error for transport.feasibility: HTTP 500")],
        critic_scripts=[_critic_verified()],  # must NOT be reached
        budget_commit_scripts=[_commit_accept()],
    )
    result = _run_rounds(orch, legs=legs, proposals=proposals)

    assert result["outcome"] == "cannot_satisfy", result
    assert "critic" not in orch.call_order, "Critic must NOT run after a transport failure"
    assert "budget.commit" not in orch.call_order, "no commit after a transport failure"
    assert "transport_error" in _log_actions(result)
    # The raw exception text must not leak as the ONLY explanation — the
    # honest conservative reason must be present.
    assert "conservative" in result["reason"].lower()


# ===========================================================================
# L1 — budget.check exception classification: transient vs genuinely-unsupported
# ===========================================================================

def test_run_negotiation_rounds_budget_check_transient_error_honest_reason() -> None:
    """L1: a TRANSIENT budget.check failure (agent IS wired, e.g. a network drop
    or merchant restart) must NOT be misdiagnosed as 'budget agent does not
    support two-phase / upgrade to v2.0.0' — it must fail closed with an honest
    'temporarily unavailable ... retry' reason instead."""
    legs = [_leg("leg-0", "tokyo", 30000)]
    proposals = {"leg-0": _proposal("tokyo-h", 12000)["proposal"]}
    orch = _ScriptedOrchestrator(
        budget_check_scripts=[RuntimeError("A2A RPC error for budget.check: connection reset")],
    )
    result = _run_rounds(orch, legs=legs, proposals=proposals)

    assert result["outcome"] == "cannot_satisfy", result
    assert "temporarily unavailable" in result["reason"].lower(), result
    assert "retry" in result["reason"].lower(), result
    assert "upgrade" not in result["reason"].lower(), (
        "must not tell the operator to upgrade an already-supported agent"
    )
    assert "cannot_satisfy_budget_check_transient" in _log_actions(result)


def test_run_negotiation_rounds_budget_check_unsupported_keeps_upgrade_message() -> None:
    """Negative case for L1: the genuinely-unsupported case (no budget client/URL
    at all — _call_budget_check's own explicit RuntimeError) must KEEP the
    'upgrade to v2.0.0' guidance, since that diagnosis is actually correct there."""
    legs = [_leg("leg-0", "tokyo", 30000)]
    proposals = {"leg-0": _proposal("tokyo-h", 12000)["proposal"]}
    orch = _ScriptedOrchestrator(
        budget_check_scripts=[RuntimeError("No budget client or URL configured")],
    )
    result = _run_rounds(orch, legs=legs, proposals=proposals)

    assert result["outcome"] == "cannot_satisfy", result
    assert "upgrade" in result["reason"].lower(), result
    assert "cannot_satisfy_no_two_phase" in _log_actions(result)


# ===========================================================================
# No Critic configured → proceed directly to COMMIT
# ===========================================================================

def test_run_negotiation_rounds_critic_not_configured() -> None:
    legs = [_leg("leg-0", "tokyo", 30000)]
    proposals = {"leg-0": _proposal("tokyo-h", 12000)["proposal"]}
    orch = _ScriptedOrchestrator(
        budget_check_scripts=[_check_ok()],
        transport_scripts=[_transport_ok()],
        critic_scripts=[],   # None → no Critic gate
        budget_commit_scripts=[_commit_accept(total_cents=12000)],
    )
    result = _run_rounds(orch, legs=legs, proposals=proposals)

    assert result["outcome"] == "success", result
    assert result.get("booking_ref")
    # critic dispatch returns None (no gate) → commit proceeds directly.
    assert "budget.commit" in orch.call_order
    assert _log_actions(result)[-1] == "accept"


# ===========================================================================
# Critic verified → _do_commit (irreversible) with idempotency threaded
# ===========================================================================

def test_run_negotiation_rounds_critic_verified_commits() -> None:
    legs = [_leg("leg-0", "tokyo", 30000)]
    proposals = {"leg-0": _proposal("tokyo-h", 12000)["proposal"]}

    seen_commit_payload = {}

    class _SpyCommit(_ScriptedOrchestrator):
        def _call_budget_commit(self, payload):  # type: ignore[override]
            seen_commit_payload.update(payload)
            return super()._call_budget_commit(payload)

    orch = _SpyCommit(
        budget_check_scripts=[_check_ok("co-XYZ")],
        transport_scripts=[_transport_ok()],
        critic_scripts=[_critic_verified()],
        budget_commit_scripts=[_commit_accept(total_cents=12000, checkout_id="co-XYZ")],
    )
    result = _run_rounds(orch, legs=legs, proposals=proposals)

    assert result["outcome"] == "success", result
    assert seen_commit_payload.get("buyer_consent") is True, seen_commit_payload
    # var-0 fix (task #49 re-plan): the commit idempotency_key is now the base
    # key plus a ':'-separated digest of the FINAL negotiated package (see
    # _package_digest in orchestrator.py) -- a prefix match, not an exact one.
    assert seen_commit_payload.get("idempotency_key", "").startswith("trip-test:"), seen_commit_payload
    assert seen_commit_payload.get("checkout_id") == "co-XYZ"
    assert result.get("booking_ref")


# ===========================================================================
# Critic rejected → re-plan accommodation; round 2 verified → success
# ===========================================================================

def test_run_negotiation_rounds_critic_rejected_replan() -> None:
    legs = [_leg("leg-0", "tokyo", 30000), _leg("leg-1", "osaka", 30000)]
    proposals = {
        "leg-0": _proposal("tokyo-h", 12000)["proposal"],
        "leg-1": _proposal("osaka-h", 11000)["proposal"],
    }
    orch = _ScriptedOrchestrator(
        budget_check_scripts=[_check_ok(), _check_ok()],
        transport_scripts=[_transport_ok(), _transport_ok()],
        # round 1: rejected (leg-1 routed to accommodation); round 2: verified
        critic_scripts=[_critic_rejected("leg-1"), _critic_verified()],
        # re-plan for leg-1 returns a cheaper hotel
        accommodation_scripts={"leg-1": [_proposal("osaka-cheap", 9000)]},
        budget_commit_scripts=[_commit_accept(total_cents=21000)],
    )
    result = _run_rounds(orch, legs=legs, proposals=proposals)

    assert result["outcome"] == "success", result
    actions = _log_actions(result)
    assert "critic_rejected" in actions, actions
    assert actions[-1] == "accept"
    # the re-plan re-proposed leg-1
    assert "acc:leg-1" in orch.call_order


# ===========================================================================
# Budget veto → re-plan priciest leg; round 2 check_ok → success
# ===========================================================================

def test_run_negotiation_rounds_budget_veto_replan_priciest_leg() -> None:
    legs = [_leg("leg-0", "tokyo", 30000), _leg("leg-1", "osaka", 30000)]
    proposals = {
        "leg-0": _proposal("tokyo-pricey", 20000)["proposal"],   # priciest
        "leg-1": _proposal("osaka-h", 11000)["proposal"],
    }
    orch = _ScriptedOrchestrator(
        # round 1 veto (ceiling 25000), round 2 check_ok
        budget_check_scripts=[_check_veto(25000), _check_ok()],
        transport_scripts=[_transport_ok()],
        critic_scripts=[_critic_verified()],
        # priciest leg (leg-0) re-proposed cheaper under the tightened ceiling
        accommodation_scripts={"leg-0": [_proposal("tokyo-cheap", 13000)]},
        budget_commit_scripts=[_commit_accept(total_cents=24000)],
    )
    result = _run_rounds(orch, legs=legs, proposals=proposals,
                         total_budget_cents=31000, effective_ceiling=31000)

    assert result["outcome"] == "success", result
    actions = _log_actions(result)
    assert "veto_received" in actions, actions
    assert actions[-1] == "accept"
    assert "acc:leg-0" in orch.call_order, "priciest leg-0 should be re-planned"


# ===========================================================================
# Commit-time veto (after Critic verified) → re-plan; round 2 success
# ===========================================================================

def test_run_negotiation_rounds_commit_time_veto_replan() -> None:
    legs = [_leg("leg-0", "tokyo", 30000), _leg("leg-1", "osaka", 30000)]
    proposals = {
        "leg-0": _proposal("tokyo-pricey", 20000)["proposal"],
        "leg-1": _proposal("osaka-h", 11000)["proposal"],
    }
    orch = _ScriptedOrchestrator(
        budget_check_scripts=[_check_ok(), _check_ok()],
        transport_scripts=[_transport_ok(), _transport_ok()],
        critic_scripts=[_critic_verified(), _critic_verified()],
        # round 1 commit VETO (repriced), round 2 commit accept
        budget_commit_scripts=[_commit_veto(25000),
                               _commit_accept(total_cents=24000)],
        accommodation_scripts={"leg-0": [_proposal("tokyo-cheap", 13000)]},
    )
    result = _run_rounds(orch, legs=legs, proposals=proposals,
                         total_budget_cents=31000, effective_ceiling=31000)

    assert result["outcome"] == "success", result
    actions = _log_actions(result)
    # commit-time veto after Critic verified is logged as commit_time_<decision>
    assert any(a.startswith("commit_time_") for a in actions), actions
    assert actions[-1] == "accept"
    assert orch.call_order.count("budget.commit") == 2, "two commit attempts"


# ===========================================================================
# M1 — commit-time needs_consent / needs_mandate → honest HALT (NOT a veto
# re-plan, NOT a fabricated "cannot re-plan" cannot_satisfy).
# ===========================================================================

@pytest.mark.parametrize(
    "commit_script,expected_reason",
    [
        (_commit_needs_consent(), "needs_consent"),
        (_commit_needs_mandate(), "needs_mandate"),
    ],
)
def test_run_negotiation_rounds_commit_needs_consent_halts_no_replan(
    commit_script: dict, expected_reason: str,
) -> None:
    """M1: a commit-time needs_consent/needs_mandate decision must halt with
    outcome=needs_consent surfacing the real consent/mandate message — NOT be
    misrouted into the price-veto re-plan loop (which, for an in-budget
    package, re_planned stays False and previously produced a fabricated
    'Commit-time veto ... Cannot re-plan' cannot_satisfy)."""
    legs = [_leg("leg-0", "tokyo", 30000), _leg("leg-1", "osaka", 30000)]
    proposals = {
        "leg-0": _proposal("tokyo-h", 12000)["proposal"],
        "leg-1": _proposal("osaka-h", 11000)["proposal"],
    }
    orch = _ScriptedOrchestrator(
        budget_check_scripts=[_check_ok()],
        transport_scripts=[_transport_ok()],
        critic_scripts=[_critic_verified()],
        budget_commit_scripts=[commit_script],
    )
    result = _run_rounds(orch, legs=legs, proposals=proposals)

    assert result["outcome"] == "needs_consent", result
    assert result["reason"] == expected_reason, result
    assert result.get("detail"), "must surface the real consent/mandate message"
    # NEVER re-planned/re-proposed a cheaper hotel for this — it's a halt, not a veto.
    assert "acc:leg-0" not in orch.call_order
    assert "acc:leg-1" not in orch.call_order
    assert orch.call_order.count("budget.commit") == 1, "must not retry commit"
    actions = _log_actions(result)
    assert expected_reason in actions, actions
    assert not any(a.startswith("commit_time_") for a in actions), (
        "must not be logged as a commit-time veto"
    )


def test_run_negotiation_rounds_commit_needs_consent_no_critic_path() -> None:
    """Same halt, on the no-Critic (direct-commit) branch."""
    legs = [_leg("leg-0", "tokyo", 30000)]
    proposals = {"leg-0": _proposal("tokyo-h", 12000)["proposal"]}
    orch = _ScriptedOrchestrator(
        budget_check_scripts=[_check_ok()],
        transport_scripts=[_transport_ok()],
        critic_scripts=[],  # no Critic wired -> direct-commit branch
        budget_commit_scripts=[_commit_needs_consent("please confirm in person")],
    )
    result = _run_rounds(orch, legs=legs, proposals=proposals)

    assert result["outcome"] == "needs_consent", result
    assert result["reason"] == "needs_consent"
    assert result["detail"] == "please confirm in person"
    assert orch.call_order.count("budget.commit") == 1


# ===========================================================================
# Budget cannot_price / unavailable → hard terminal (no retry)
# ===========================================================================

@pytest.mark.parametrize("decision", ["cannot_price", "unavailable"])
def test_run_negotiation_rounds_budget_cannot_price_terminal(decision: str) -> None:
    legs = [_leg("leg-0", "tokyo", 30000)]
    proposals = {"leg-0": _proposal("tokyo-h", 12000)["proposal"]}
    orch = _ScriptedOrchestrator(
        budget_check_scripts=[{"decision": decision, "veto_reason": "sold_out"}],
        transport_scripts=[_transport_ok()],
        critic_scripts=[_critic_verified()],
        budget_commit_scripts=[_commit_accept()],
    )
    result = _run_rounds(orch, legs=legs, proposals=proposals)

    assert result["outcome"] == "cannot_satisfy", result
    assert "transport" not in orch.call_order, "no gates after cannot_price"
    assert "budget.commit" not in orch.call_order
    assert f"budget_{decision}" in _log_actions(result)


# ===========================================================================
# _do_commit — accept path threads idempotency + buyer_consent
# ===========================================================================

def test_do_commit_accept_threads_consent_and_idempotency() -> None:
    seen = {}

    class _SpyCommit(_ScriptedOrchestrator):
        def _call_budget_commit(self, payload):  # type: ignore[override]
            seen.update(payload)
            return {"decision": "accept", "booking_ref": "BK-x", "checkout_id": "co-9"}

    orch = _SpyCommit()
    res = orch._do_commit(user_id="u1", checkout_id="co-9", idempotency_key="trip-99")
    assert res["decision"] == "accept"
    assert seen["buyer_consent"] is True
    assert seen["idempotency_key"] == "trip-99"
    assert seen["checkout_id"] == "co-9"


# ===========================================================================
# _do_commit raises → commit_failed terminal (no re-plan, ambiguous state)
# ===========================================================================

def test_do_commit_raises_commit_failed_terminal() -> None:
    class _CommitRaises(_ScriptedOrchestrator):
        def _call_budget_commit(self, payload):  # type: ignore[override]
            raise RuntimeError("merchant 5xx at commit")

    orch = _CommitRaises()
    res = orch._do_commit(user_id="u1", checkout_id="co-1", idempotency_key="trip-1")
    assert res["decision"] == "commit_failed"
    assert res["idempotency_key"] == "trip-1"
    assert res["checkout_id"] == "co-1"


def test_run_negotiation_rounds_commit_failed_is_terminal_no_replan() -> None:
    legs = [_leg("leg-0", "tokyo", 30000)]
    proposals = {"leg-0": _proposal("tokyo-h", 12000)["proposal"]}

    class _CommitRaises(_ScriptedOrchestrator):
        def _call_budget_commit(self, payload):  # type: ignore[override]
            self.call_order.append("budget.commit")
            raise RuntimeError("merchant 5xx at commit")

    orch = _CommitRaises(
        budget_check_scripts=[_check_ok()],
        transport_scripts=[_transport_ok()],
        critic_scripts=[_critic_verified()],
    )
    result = _run_rounds(orch, legs=legs, proposals=proposals)

    assert result["outcome"] == "cannot_satisfy", result
    assert result.get("needs_reconciliation") is True
    assert result.get("idempotency_key") == "trip-test"
    # commit attempted exactly once (no re-plan after ambiguous commit failure)
    assert orch.call_order.count("budget.commit") == 1


# ===========================================================================
# Day-planner OFF-path: a day-planner failure must NOT block the commit
# ===========================================================================

def test_run_negotiation_rounds_day_planner_failure_non_fatal() -> None:
    legs = [_leg("leg-0", "tokyo", 30000)]
    proposals = {"leg-0": _proposal("tokyo-h", 12000)["proposal"]}

    class _DayPlannerRaises(_ScriptedOrchestrator):
        def _call_day_planner(self, legs):  # type: ignore[override]
            self.call_order.append("day_planner")
            raise RuntimeError("day planner blew up")

    orch = _DayPlannerRaises(
        budget_check_scripts=[_check_ok()],
        transport_scripts=[_transport_ok()],
        critic_scripts=[_critic_verified()],
        budget_commit_scripts=[_commit_accept(total_cents=12000)],
    )
    result = _run_rounds(orch, legs=legs, proposals=proposals)

    # Day-planner raised but commit still succeeded.
    assert result["outcome"] == "success", result
    assert "day_planner" in orch.call_order
    assert "budget.commit" in orch.call_order
    # L2: the failure must be surfaced HONESTLY, not silently degrade to a
    # plan indistinguishable from "no day-planner wired at all".
    assert "day_plans" not in result
    assert result.get("day_planner_degraded") is True, result
    assert any(
        "Day-by-day activity and meal planning could not be completed" in a
        for a in result.get("advisories", [])
    ), result.get("advisories")


def test_run_negotiation_rounds_day_planner_not_configured_no_false_alarm() -> None:
    """Negative case for L2: when NO Day-planner is configured at all
    (day_plan_result stays None with no exception — the ScriptedOrchestrator's
    default), there must be NO day_planner_degraded flag / advisory — that's
    the normal not-wired bypass, not a failure."""
    legs = [_leg("leg-0", "tokyo", 30000)]
    proposals = {"leg-0": _proposal("tokyo-h", 12000)["proposal"]}
    orch = _ScriptedOrchestrator(
        budget_check_scripts=[_check_ok()],
        transport_scripts=[_transport_ok()],
        critic_scripts=[_critic_verified()],
        budget_commit_scripts=[_commit_accept(total_cents=12000)],
        # day_planner_result intentionally omitted -> None, no exception.
    )
    result = _run_rounds(orch, legs=legs, proposals=proposals)

    assert result["outcome"] == "success", result
    assert "day_plans" not in result
    assert "day_planner_degraded" not in result
    assert "advisories" not in result


def test_run_negotiation_rounds_day_planner_plan_attached() -> None:
    legs = [_leg("leg-0", "tokyo", 30000)]
    proposals = {"leg-0": _proposal("tokyo-h", 12000)["proposal"]}
    day_plan = {"leg_plans": [{"leg_id": "leg-0", "catalog_hit": True, "days": []}]}
    orch = _ScriptedOrchestrator(
        budget_check_scripts=[_check_ok()],
        transport_scripts=[_transport_ok()],
        critic_scripts=[_critic_verified()],
        budget_commit_scripts=[_commit_accept(total_cents=12000)],
        day_planner_result=day_plan,
    )
    result = _run_rounds(orch, legs=legs, proposals=proposals)
    assert result["outcome"] == "success", result
    # The success builder surfaces the day plan additively when present.
    assert "day_plans" in result or "day_plan" in result or result.get("day_plans") is not None


# ===========================================================================
# _negotiate_dp — full DP flow: DP-selected hotels → CHECK/Transport/Critic/COMMIT
# ===========================================================================

def test_negotiate_dp_gather_candidates_and_allocate() -> None:
    legs = [
        _leg("leg-0", "tokyo", 30000, dp_selected_hotel_id="tokyo-dp",
             dp_selected_total_cents=12000),
        _leg("leg-1", "osaka", 30000, dp_selected_hotel_id="osaka-dp",
             dp_selected_total_cents=11000),
    ]
    trip = {
        "user_id": "u1",
        "total_budget_cents": 100000,
        "legs": [
            {"city": "tokyo", "checkin": "2026-12-01", "checkout": "2026-12-04", "adults": 1},
            {"city": "osaka", "checkin": "2026-12-04", "checkout": "2026-12-07", "adults": 1},
        ],
    }
    orch = _ScriptedOrchestrator(
        planner_legs=legs,
        planner_dp_used=True,
        gather_scripts={
            "leg-0": [{"hotel_id": "tokyo-dp", "total_cents": 12000,
                       "review_score": 8.5, "star_rating": 4.0}],
            "leg-1": [{"hotel_id": "osaka-dp", "total_cents": 11000,
                       "review_score": 8.0, "star_rating": 4.0}],
        },
        budget_check_scripts=[_check_ok()],
        transport_scripts=[_transport_ok()],
        critic_scripts=[_critic_verified()],
        budget_commit_scripts=[_commit_accept(total_cents=23000)],
    )
    orch._trip_id = "test-trip"
    orch._trip_request_legs = trip["legs"]
    result = orch._negotiate_dp(
        trip_request=trip,
        user_id="u1",
        total_budget_cents=100000,
        effective_ceiling=100000,
        idempotency_key="trip-dp",
        negotiation_log=[],
        target_areas={},
        area_stage={},
        lodging_budget_cents=100000,
    )
    assert result["outcome"] == "success", result
    assert "planner" in orch.call_order
    # DP selections used directly: no greedy acc proposal needed before booking.
    assert len(result.get("legs", [])) == 2
    # the booked hotels are the DP-selected ones
    booked = {l["hotel_id"] for l in result["legs"]}
    assert booked == {"tokyo-dp", "osaka-dp"}, booked


# ===========================================================================
# _negotiate_dp — a leg with no candidates → cannot_satisfy BEFORE Planner
# ===========================================================================

def test_negotiate_dp_empty_candidates_cannot_satisfy() -> None:
    trip = {
        "user_id": "u1",
        "total_budget_cents": 100000,
        "legs": [
            {"city": "tokyo", "checkin": "2026-12-01", "checkout": "2026-12-04", "adults": 1},
            {"city": "boracay", "checkin": "2026-12-04", "checkout": "2026-12-07", "adults": 1},
        ],
    }
    orch = _ScriptedOrchestrator(
        planner_legs=[],            # planner must NOT be reached
        gather_scripts={
            "leg-0": [{"hotel_id": "tokyo-dp", "total_cents": 12000}],
            "leg-1": [],            # boracay: no candidates
        },
        gather_reasons={"leg-1": "no_inventory"},
        budget_check_scripts=[_check_ok()],   # must NOT be reached
    )
    orch._trip_id = "test-trip"
    result = orch._negotiate_dp(
        trip_request=trip,
        user_id="u1",
        total_budget_cents=100000,
        effective_ceiling=100000,
        idempotency_key="trip-dp",
        negotiation_log=[],
        target_areas={},
        area_stage={},
        lodging_budget_cents=100000,
    )
    assert result["outcome"] == "cannot_satisfy", result
    assert "planner" not in orch.call_order, "Planner must NOT run when a leg has no candidates"
    assert "budget.check" not in orch.call_order
    reason = result.get("reason", "").lower()
    assert "boracay" in reason, reason
    assert "inventory" in reason or "lodging" in reason, reason


def test_negotiate_dp_empty_candidates_over_budget_reason() -> None:
    """#70 honest attribution: over_budget cause is distinguished from no_inventory."""
    trip = {
        "user_id": "u1",
        "total_budget_cents": 100000,
        "legs": [
            {"city": "singapore", "checkin": "2026-12-01", "checkout": "2026-12-04", "adults": 1},
        ],
    }
    orch = _ScriptedOrchestrator(
        planner_legs=[],
        gather_scripts={"leg-0": []},
        gather_reasons={"leg-0": "over_budget"},
    )
    orch._trip_id = "test-trip"
    result = orch._negotiate_dp(
        trip_request=trip,
        user_id="u1",
        total_budget_cents=100000,
        effective_ceiling=100000,
        idempotency_key="trip-dp",
        negotiation_log=[],
        target_areas={},
        area_stage={},
        lodging_budget_cents=100000,
    )
    assert result["outcome"] == "cannot_satisfy", result
    reason = result.get("reason", "").lower()
    assert "singapore" in reason, reason
    assert "budget" in reason, reason


# ===========================================================================
# _negotiate_greedy — proportional split → greedy proposals → success
# ===========================================================================

def test_negotiate_greedy_proportional_split() -> None:
    legs = [_leg("leg-0", "tokyo", 50000), _leg("leg-1", "osaka", 50000)]
    trip = {
        "user_id": "u1",
        "total_budget_cents": 100000,
        "legs": [
            {"city": "tokyo", "checkin": "2026-12-01", "checkout": "2026-12-04", "adults": 1},
            {"city": "osaka", "checkin": "2026-12-04", "checkout": "2026-12-07", "adults": 1},
        ],
    }
    orch = _ScriptedOrchestrator(
        planner_legs=legs,
        planner_dp_used=False,
        accommodation_scripts={
            "leg-0": [_proposal("tokyo-greedy", 12000)],
            "leg-1": [_proposal("osaka-greedy", 11000)],
        },
        budget_check_scripts=[_check_ok()],
        transport_scripts=[_transport_ok()],
        critic_scripts=[_critic_verified()],
        budget_commit_scripts=[_commit_accept(total_cents=23000)],
    )
    orch._trip_id = "test-trip"
    orch._trip_request_legs = trip["legs"]
    result = orch._negotiate_greedy(
        trip_request=trip,
        user_id="u1",
        total_budget_cents=100000,
        effective_ceiling=100000,
        idempotency_key="trip-greedy",
        negotiation_log=[],
        target_areas={},
        area_stage={},
        lodging_budget_cents=100000,
    )
    assert result["outcome"] == "success", result
    assert "planner" in orch.call_order
    booked = {l["hotel_id"] for l in result["legs"]}
    assert booked == {"tokyo-greedy", "osaka-greedy"}, booked
    # greedy proposed each leg before the rounds
    assert "acc:leg-0" in orch.call_order and "acc:leg-1" in orch.call_order


# ===========================================================================
# _primary_dest_token — pure function
# ===========================================================================

def test_primary_dest_token_explicit_dest_country() -> None:
    leg_meta = {"leg-0": {"dest_country": "JP", "city": "tokyo"}}
    proposals = {"leg-0": {"hotel_id": "h"}}
    assert TravelOrchestrator._primary_dest_token(leg_meta, proposals) == "JP"


def test_primary_dest_token_city_iso2_fallback() -> None:
    # No dest_country → city resolved via CITY_TO_ISO2 (tokyo → JP).
    leg_meta = {"leg-0": {"city": "tokyo"}}
    proposals = {"leg-0": {"hotel_id": "h"}}
    tok = TravelOrchestrator._primary_dest_token(leg_meta, proposals)
    assert tok == "JP", tok


def test_primary_dest_token_city_slug_fallback() -> None:
    # Unknown city (not in CITY_TO_ISO2) → slugified city name.
    leg_meta = {"leg-0": {"city": "Port-au-Prince Zzz"}}
    proposals = {"leg-0": {"hotel_id": "h"}}
    tok = TravelOrchestrator._primary_dest_token(leg_meta, proposals)
    assert tok == "port-au-prince-zzz", tok


def test_primary_dest_token_no_signal_returns_unk() -> None:
    assert TravelOrchestrator._primary_dest_token({}, {}) == "UNK"


def test_primary_dest_token_uses_first_booked_leg() -> None:
    # leg-0 has no proposal → primary destination is the first BOOKED leg (leg-1).
    leg_meta = {
        "leg-0": {"dest_country": "FR", "city": "paris"},
        "leg-1": {"dest_country": "JP", "city": "tokyo"},
    }
    proposals = {"leg-0": None, "leg-1": {"hotel_id": "h"}}
    assert TravelOrchestrator._primary_dest_token(leg_meta, proposals) == "JP"


# ===========================================================================
# _mint_booking_ref — pure function
# ===========================================================================

def test_mint_booking_ref_destination_aware_remint() -> None:
    out = TravelOrchestrator._mint_booking_ref("BK-co-local-abc123", "JP")
    assert out == "BK-JP-abc123", out


def test_mint_booking_ref_none_input() -> None:
    assert TravelOrchestrator._mint_booking_ref(None, "JP") is None


def test_mint_booking_ref_empty_input() -> None:
    assert TravelOrchestrator._mint_booking_ref("", "JP") == ""


def test_mint_booking_ref_preserves_suffix_without_cosmetic_prefix() -> None:
    # No 'co-local-' prefix → just strip BK- and graft dest token.
    out = TravelOrchestrator._mint_booking_ref("BK-xyz999", "ID")
    assert out == "BK-ID-xyz999", out


# ===========================================================================
# #94 — _enrich_leg_meta_dest_country backfills the Planner-skeleton's missing
# dest_country from self._trip_request_legs (the side-channel introduced for
# the day-planner, #87), so _primary_dest_token no longer silently falls
# through to a CITY_TO_ISO2 guess for every trip.
# ===========================================================================

def test_enrich_leg_meta_dest_country_backfills_from_trip_request_legs() -> None:
    """
    leg_meta, as built by _negotiate_dp/_negotiate_greedy, comes straight from
    the Planner-agent skeleton output — city/checkin/checkout/adults/
    per_leg_budget_cents only, NEVER dest_country (see the #30 day-planner
    block comment, ~line 2498). Before #94 this meant _primary_dest_token's
    `lm.get("dest_country")` branch was DEAD CODE in production: it always
    read None and fell through to the CITY_TO_ISO2 city guess, regardless of
    what dest_country the caller explicitly supplied.
    """
    orch = TravelOrchestrator()
    # Mirrors the true skeleton shape: NO dest_country key at all.
    leg_meta = {
        "leg-0": {"leg_id": "leg-0", "city": "richmond", "checkin": "2026-12-01",
                  "checkout": "2026-12-04", "adults": 1, "per_leg_budget_cents": 30000},
    }
    orch._trip_request_legs = [
        {"city": "richmond", "dest_country": "US", "checkin": "2026-12-01",
         "checkout": "2026-12-04", "adults": 1},
    ]
    orch._enrich_leg_meta_dest_country(leg_meta)
    assert leg_meta["leg-0"]["dest_country"] == "US", leg_meta


def test_enrich_leg_meta_dest_country_never_overwrites_explicit_value() -> None:
    """Additive only — a leg that already carries dest_country is left alone."""
    orch = TravelOrchestrator()
    leg_meta = {"leg-0": {"leg_id": "leg-0", "city": "richmond", "dest_country": "CA"}}
    orch._trip_request_legs = [{"city": "richmond", "dest_country": "US"}]
    orch._enrich_leg_meta_dest_country(leg_meta)
    assert leg_meta["leg-0"]["dest_country"] == "CA", leg_meta


def test_enrich_leg_meta_dest_country_noop_without_trip_request_legs() -> None:
    """No side-channel set (e.g. a direct _negotiate_dp/_negotiate_greedy caller
    that bypasses negotiate()) → no-op, byte-identical to pre-#94 behaviour."""
    orch = TravelOrchestrator()
    leg_meta = {"leg-0": {"leg_id": "leg-0", "city": "richmond"}}
    orch._enrich_leg_meta_dest_country(leg_meta)
    assert "dest_country" not in leg_meta["leg-0"], leg_meta


def test_negotiate_dp_booking_ref_uses_explicit_dest_country_not_city_guess() -> None:
    """
    #94 END-TO-END regression: "richmond" is a city-name collision — it exists
    under BOTH Canada and the United States in the merchant catalog, and
    CITY_TO_ISO2['richmond'] happens to resolve to 'CA' (catalog insertion
    order). Before #94, a caller who explicitly requested
    dest_country='US' still got a booking_ref minted with the WRONG 'CA'
    token, because leg_meta (built from the dest_country-less Planner
    skeleton) never carried the caller's dest_country through to
    _primary_dest_token. This drives the REAL _negotiate_dp path (Planner
    skeleton WITHOUT dest_country, exactly like production) and asserts the
    minted booking_ref reflects the caller's explicit 'US', not the city-map
    guess.
    """
    from utils.intent_parser import CITY_TO_ISO2
    if CITY_TO_ISO2.get("richmond") != "CA":
        pytest.skip(
            "This public export ships a redacted 10-row sample catalog "
            "(ucp-merchant/catalog.json), not the real curated catalog richmond's "
            "city-name-collision behavior depends on — CITY_TO_ISO2 is built from "
            "that file at import time, so this environment has no city that "
            "actually exhibits the CA/US collision this test needs to distinguish "
            "the two code paths. See _enrich_leg_meta_dest_country's other unit "
            "tests above for the same fix covered without needing real catalog data."
        )

    # Skeleton legs — NO dest_country (mirrors the real Planner agent output).
    skeleton_legs = [
        _leg("leg-0", "richmond", 30000, dp_selected_hotel_id="richmond-us-h",
             dp_selected_total_cents=12000),
    ]
    trip = {
        "user_id": "u1",
        "total_budget_cents": 100000,
        "legs": [
            {"city": "richmond", "dest_country": "US",
             "checkin": "2026-12-01", "checkout": "2026-12-04", "adults": 1},
        ],
    }
    orch = _ScriptedOrchestrator(
        planner_legs=skeleton_legs,
        planner_dp_used=True,
        gather_scripts={
            "leg-0": [{"hotel_id": "richmond-us-h", "total_cents": 12000,
                       "review_score": 8.0, "star_rating": 4.0}],
        },
        budget_check_scripts=[_check_ok()],
        transport_scripts=[_transport_ok()],
        critic_scripts=[_critic_verified()],
        budget_commit_scripts=[_commit_accept("BK-co-local-richmond1", total_cents=12000)],
    )
    orch._trip_id = "test-trip"
    orch._trip_request_legs = trip["legs"]
    result = orch._negotiate_dp(
        trip_request=trip,
        user_id="u1",
        total_budget_cents=100000,
        effective_ceiling=100000,
        idempotency_key="trip-dp-richmond",
        negotiation_log=[],
        target_areas={},
        area_stage={},
        lodging_budget_cents=100000,
    )
    assert result["outcome"] == "success", result
    ref = result.get("booking_ref", "")
    assert ref.startswith("BK-US-"), (
        f"#94 regression: booking_ref must use the caller's explicit "
        f"dest_country=US, not the richmond→CA CITY_TO_ISO2 guess: {ref!r}"
    )


if __name__ == "__main__":
    import sys as _sys
    _sys.exit(pytest.main([__file__, "-q"]))
