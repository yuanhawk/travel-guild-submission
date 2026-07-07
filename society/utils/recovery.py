"""
recovery.py — L3-core reactive disruption-recovery for the Travel Guild.

Design contract: the internal design spec §12.8 (reduced cut), §12.9
(two-tier reactivity + baseline/secondary + pressure-test).

## Overview

L3-core recovery handles a single fault class: a hotel in the booked package
becomes unavailable (sold-out).  Recovery proceeds:

  1. Detect the fault (hotel_id unavailable in the booked package).
  2. Activate the pre-vetted secondary (or re-allocate if secondary is also hit).
  3. Transport + Critic re-verify the recovery package.
  4. Enforce budget ceiling (recovery must be ≤ user budget).
  5. Present the swap to the user and obtain ONE fresh re-consent.
  6. A NEW checkout + NEW mandate over it → COMMIT → new booking_ref.

Per §12.8 RESOLVED: recovery is NOT autonomous.  `verifyMandate` binds the
signature to `checkout_id`; a swapped package needs a fresh mandate (the human
re-authorizes the swap; the merchant re-enforces budget).  Honest
`cannot_satisfy` if no secondary fits within budget.

## Pre-vetted secondary (plan-time)

After the baseline package converges + Critic-verifies, the orchestrator calls
`compute_and_verify_secondary(...)` which:
  - Calls `allocator.allocate_secondary(...)` to get the DP next-best with the
    highest-cost leg's chosen hotel excluded.
  - Critic-verifies the secondary package.
  - Stores it alongside the baseline in the negotiate() result.

The secondary is deterministic (same fault → same recovery, variance-0).
The Critic gate ensures the secondary is valid before a fault ever occurs.

## Recovery flow

`RecoveryOrchestrator.recover(...)` is called when a fault is detected:
  1. Checks whether the pre-vetted secondary covers the fault (i.e., the
     unavailable hotel is exactly the one the secondary excludes).
  2. If yes → use the secondary as the recovery plan.
  3. If no (secondary also hit, or secondary was for a different leg) →
     fall back to re-allocating from the remaining candidates with the
     unavailable hotel excluded.
  4. Transport gate (re-verify).
  5. Critic gate (re-verify).
  6. Budget ceiling check (≤ user_budget_cents).
  7. Present to user for re-consent (not auto-commit).
  8. On consent: new create_checkout → complete_checkout with fresh mandate
     → new booking_ref.

## Key safety invariants

- No autonomous recovery commit.  The human must sign a fresh mandate over
  the new checkout before `complete_checkout` fires.
- Budget ceiling enforced by the merchant: the recovery checkout total must
  pass `verifyMandate(budget_ceiling_cents >= liveCents)`.
- Honest `cannot_satisfy` when no in-budget secondary exists.
- Deterministic: same fault on the same baseline → same recovery (variance-0).

## Multi-fault CASCADE recovery (§12.1 — the marquee depth-add)

`RecoveryOrchestrator.recover_cascade(...)` extends the single-fault core to
survive a SEQUENCE of sequential faults (grounded in a real flight-cancellation
cascade): detect → recover → detect the NEXT fault on the NEW state → recover →
…, each cycle within budget, each with ONE fresh re-consent.  Two fault classes:

  - hotel_sold_out  → handled by `recover()` (the single-fault core; a hotel swap).
  - flight_cancel   → handled by `_recover_flight_cancel()` (a Transport-owned
                      re-route, e.g. via a hub with an unplanned overnight).

Each cycle is gated identically (Transport re-verify → Critic → budget → fresh
checkout → ONE fresh re-consent → commit), then the SUPERSEDED booking is voided
(commit-new first, then void-old via `cancel_recovery` → merchant cancel_checkout)
so exactly ONE itinerary is ever live.  Because each cycle's recovery_total_cents
is the FULL itinerary cost, cumulative committed spend == the final booking total
(no double-charge across cycles).  An unrecoverable fault stops the cascade with
an honest `cannot_satisfy`; the single live booking from the last successful cycle
stands, and no partial/autonomous spend occurs on the failing fault.  The
single-fault path (`recover` / `commit_recovery`) is UNCHANGED and remains the
verified L3 core.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Recovery result types
# ---------------------------------------------------------------------------

RECOVERY_OUTCOME_READY = "recovery_ready"         # recovery plan assembled; awaiting re-consent
RECOVERY_OUTCOME_COMMITTED = "recovery_committed"  # re-consent received; new booking_ref issued
RECOVERY_OUTCOME_CANNOT_SATISFY = "cannot_satisfy" # no in-budget recovery; honest terminal

# Cascade fault types (§12.1). A cascade is a SEQUENCE of these.
FAULT_HOTEL_SOLD_OUT = "hotel_sold_out"   # a booked hotel becomes unavailable
FAULT_FLIGHT_CANCEL = "flight_cancel"     # an inter-leg flight/transfer is cancelled

# Cascade outcome.
CASCADE_OUTCOME_SURVIVED = "cascade_survived"        # all faults recovered, final booking
CASCADE_OUTCOME_CANNOT_SATISFY = "cannot_satisfy"    # a fault was unrecoverable (honest)


# ---------------------------------------------------------------------------
# RecoveryOrchestrator
# ---------------------------------------------------------------------------

class RecoveryOrchestrator:
    """
    L3-core reactive disruption-recovery.

    Requires the same agent clients/URLs as TravelOrchestrator, plus
    access to the pre-vetted secondary (stored at plan time).

    Usage:
        ro = RecoveryOrchestrator(budget_client=..., critic_client=..., ...)
        result = ro.recover(
            original_booking=...,   # the booked package (from negotiate())
            unavailable_hotel_id=...,
            secondary_plan=...,     # pre-vetted secondary (from negotiate())
            per_leg_candidates=..., # full candidate sets (for re-allocation fallback)
            user_id=...,
            total_budget_cents=...,
        )
        # result["outcome"] == "recovery_ready" → present to user for re-consent
        # call ro.commit_recovery(result, fresh_mandate) to complete.
    """

    def __init__(
        self,
        budget_client: Any = None,
        critic_client: Any = None,
        transport_client: Any = None,
        budget_url: str | None = None,
        critic_url: str | None = None,
        transport_url: str | None = None,
        tracer: Any = None,
    ) -> None:
        self._budget_client = budget_client
        self._critic_client = critic_client
        self._transport_client = transport_client
        self._budget_url = budget_url
        self._critic_url = critic_url
        self._transport_url = transport_url
        # Side-channel tracer (Var-0 sacred: no-op by default — mirrors the
        # orchestrator pattern). Used to emit the SIMULATED prepaid wallet CREDIT
        # event on a confirmed void. A tracer bug can never crash recovery (the
        # emit site is try/except-guarded).
        self._tracer: Any = tracer if tracer is not None else (lambda *a, **kw: None)

    # ------------------------------------------------------------------
    # Agent call dispatch (mirrors TravelOrchestrator dispatch)
    # ------------------------------------------------------------------

    def _call_budget_check(self, payload: dict) -> dict:
        return self._dispatch(self._budget_client, self._budget_url, payload, "budget.check")

    def _call_budget_commit(self, payload: dict) -> dict:
        return self._dispatch(self._budget_client, self._budget_url, payload, "budget.commit")

    def _call_budget_cancel(self, payload: dict) -> dict:
        return self._dispatch(self._budget_client, self._budget_url, payload, "budget.cancel")

    def _call_critic(self, payload: dict) -> dict | None:
        if self._critic_client is None and self._critic_url is None:
            return None
        return self._dispatch(self._critic_client, self._critic_url, payload, "itinerary.verify")

    def _call_transport(self, legs: list[dict]) -> dict | None:
        if self._transport_client is None and self._transport_url is None:
            return None
        return self._dispatch(
            self._transport_client, self._transport_url,
            {"legs": legs}, "transport.feasibility"
        )

    def _dispatch(self, client: Any, url: str | None, payload: dict, skill_id: str) -> dict:
        from orchestration.orchestrator import _send_to_client, _send_to_url
        if client is not None:
            return _send_to_client(client, payload, skill_id)
        if url:
            return _send_to_url(url, payload, skill_id)
        raise RuntimeError(f"No client or URL for skill {skill_id}")

    # ------------------------------------------------------------------
    # Core recovery logic
    # ------------------------------------------------------------------

    def recover(
        self,
        *,
        original_booking: dict,
        unavailable_hotel_id: str,
        secondary_plan: dict | None,
        per_leg_candidates: list[dict] | None = None,
        user_id: str,
        total_budget_cents: int,
    ) -> dict[str, Any]:
        """
        Execute the L3 recovery flow for a sold-out hotel.

        Args:
            original_booking:      The booked package dict (from negotiate() → success result).
                                   Must include "legs" list with hotel_id per leg.
            unavailable_hotel_id:  The hotel_id that became sold-out.
            secondary_plan:        Pre-vetted secondary from the plan phase (or None).
                                   Schema: allocate_secondary() result + critic_result.
            per_leg_candidates:    Full per-leg candidate sets for re-allocation fallback.
            user_id:               The user's ID.
            total_budget_cents:    The original budget ceiling.

        Returns a dict:
          On success (recovery plan assembled, awaiting re-consent):
            {
              "outcome": "recovery_ready",
              "recovery_legs": [...],   # the new leg set (hotel swapped)
              "affected_leg_id": str,
              "swapped_from": str,      # original hotel_id
              "swapped_to": str,        # new hotel_id
              "recovery_checkout_id": str,  # a new checkout (no mandate yet)
              "recovery_total_cents": int,
              "critic_result": {...} | None,
              "transport_result": {...} | None,
              "source": "secondary" | "reallocate",
              "idempotency_key": str,   # for fresh commit
            }
          On failure:
            {"outcome": "cannot_satisfy", "reason": str, ...}
        """
        original_legs: list[dict] = original_booking.get("legs", [])

        # Identify which leg holds the unavailable hotel
        affected_leg_id: str | None = None
        for leg in original_legs:
            if leg.get("hotel_id") == unavailable_hotel_id:
                affected_leg_id = leg["leg_id"]
                break

        if affected_leg_id is None:
            return {
                "outcome": RECOVERY_OUTCOME_CANNOT_SATISFY,
                "reason": (
                    f"Hotel {unavailable_hotel_id!r} is not in the booked package. "
                    f"No recovery needed."
                ),
            }

        logger.info(
            "recovery: fault detected — hotel=%s leg=%s user=%s budget=%d¢",
            unavailable_hotel_id, affected_leg_id, user_id, total_budget_cents,
        )

        # ------------------------------------------------------------------
        # Step 1: Attempt to use the pre-vetted secondary.
        # The secondary is usable when it excludes the exact hotel that faulted
        # on the exact leg that is affected.
        # ------------------------------------------------------------------
        recovery_plan: dict | None = None
        source = "secondary"

        if (
            secondary_plan is not None
            and secondary_plan.get("feasible")
            and secondary_plan.get("affected_leg_id") == affected_leg_id
            and secondary_plan.get("excluded_hotel_id") == unavailable_hotel_id
        ):
            recovery_plan = secondary_plan
            logger.info(
                "recovery: using pre-vetted secondary (excluded=%s leg=%s total=%d¢)",
                unavailable_hotel_id, affected_leg_id,
                secondary_plan.get("total_cents", 0),
            )
        else:
            # Log why the pre-vetted secondary didn't match
            if secondary_plan is None:
                logger.info("recovery: no pre-vetted secondary available — re-allocating")
            elif not secondary_plan.get("feasible"):
                logger.info("recovery: pre-vetted secondary infeasible — re-allocating")
            else:
                logger.info(
                    "recovery: secondary covers different fault "
                    "(secondary.affected=%s secondary.excluded=%s vs fault hotel=%s leg=%s)"
                    " — re-allocating",
                    secondary_plan.get("affected_leg_id"),
                    secondary_plan.get("excluded_hotel_id"),
                    unavailable_hotel_id, affected_leg_id,
                )
            source = "reallocate"

        # ------------------------------------------------------------------
        # Step 2: Re-allocation fallback (if secondary didn't match).
        # ------------------------------------------------------------------
        if source == "reallocate":
            if not per_leg_candidates:
                return {
                    "outcome": RECOVERY_OUTCOME_CANNOT_SATISFY,
                    "reason": (
                        "No pre-vetted secondary matches the fault and no candidate "
                        "sets available for re-allocation."
                    ),
                }
            from utils.allocator import allocate_secondary
            recovery_plan = allocate_secondary(
                legs_with_candidates=per_leg_candidates,
                total_budget_cents=total_budget_cents,
                baseline_selection=original_legs,
                highest_cost_leg_id=affected_leg_id,
            )
            # Override the excluded hotel to the actual unavailable one
            # (allocate_secondary picks by baseline hotel; here we must
            #  exclude the unavailable hotel regardless of cost ranking).
            if not recovery_plan.get("feasible"):
                # Try explicitly excluding the unavailable hotel on the affected leg
                from utils.allocator import allocate
                reduced_per_leg = []
                for leg_cands in (per_leg_candidates or []):
                    lid = leg_cands.get("leg_id", "")
                    if lid == affected_leg_id:
                        filtered = [
                            c for c in leg_cands.get("candidates", [])
                            if c.get("hotel_id") != unavailable_hotel_id
                        ]
                        reduced_per_leg.append({"leg_id": lid, "candidates": filtered})
                    else:
                        reduced_per_leg.append(leg_cands)
                recovery_plan = allocate(reduced_per_leg, total_budget_cents)
                recovery_plan["affected_leg_id"] = affected_leg_id
                recovery_plan["excluded_hotel_id"] = unavailable_hotel_id

            if not recovery_plan.get("feasible"):
                logger.info(
                    "recovery: re-allocation infeasible — cannot_satisfy "
                    "(unavailable=%s leg=%s budget=%d¢)",
                    unavailable_hotel_id, affected_leg_id, total_budget_cents,
                )
                return {
                    "outcome": RECOVERY_OUTCOME_CANNOT_SATISFY,
                    "reason": (
                        f"No in-budget recovery exists after excluding hotel "
                        f"{unavailable_hotel_id!r} on leg {affected_leg_id!r}. "
                        f"Budget: {total_budget_cents}¢."
                    ),
                    "affected_leg_id": affected_leg_id,
                    "excluded_hotel_id": unavailable_hotel_id,
                }

        # recovery_plan is now feasible (either secondary or reallocate)
        recovery_selection: list[dict] = recovery_plan.get("selection", [])
        recovery_total_cents: int = int(recovery_plan.get("total_cents", 0))

        # ------------------------------------------------------------------
        # Step 3: Build the recovery leg list (merge original metadata with
        #         the new hotel selections).
        # ------------------------------------------------------------------
        # Build index: leg_id -> original leg metadata
        original_leg_meta: dict[str, dict] = {l["leg_id"]: l for l in original_legs}
        # Build index: leg_id -> new hotel selection
        new_sel_by_leg: dict[str, dict] = {s["leg_id"]: s for s in recovery_selection}

        recovery_legs: list[dict] = []
        swapped_from = ""
        swapped_to = ""
        for orig_leg in original_legs:
            lid = orig_leg["leg_id"]
            new_sel = new_sel_by_leg.get(lid)
            if new_sel and new_sel.get("hotel_id") != orig_leg.get("hotel_id"):
                # This leg was swapped
                swapped_from = orig_leg.get("hotel_id", "")
                swapped_to = new_sel["hotel_id"]
                recovery_legs.append({
                    **orig_leg,
                    "hotel_id": new_sel["hotel_id"],
                    "total_cents": new_sel["total_cents"],
                    "hotel_title": new_sel.get("title", new_sel["hotel_id"]),
                    "area": new_sel.get("area", orig_leg.get("area", "")),
                    "provenance": "merchant",
                    # Keep original fields for non-hotel metadata
                })
            else:
                recovery_legs.append(orig_leg)

        # ------------------------------------------------------------------
        # Step 4: Transport gate re-verification.
        # ------------------------------------------------------------------
        transport_legs = [
            {
                "leg_id": l["leg_id"],
                "city": l.get("city", ""),
                "area": l.get("area", ""),
                "checkin": l.get("checkin", ""),
                "checkout": l.get("checkout", ""),
                "adults": l.get("adults", 1),
                "hotel_id": l.get("hotel_id", ""),
                "total_cents": l.get("total_cents", 0),
                "provenance": l.get("provenance", "merchant"),
            }
            for l in recovery_legs
        ]
        transport_result = self._call_transport(transport_legs)

        if transport_result is not None:
            infeasible = transport_result.get("infeasible_edges", [])
            if infeasible:
                edge_summary = "; ".join(
                    f"{e.get('from_leg','?')}→{e.get('to_leg','?')}" for e in infeasible
                )
                logger.warning(
                    "recovery: transport infeasible after swap — %s", edge_summary
                )
                return {
                    "outcome": RECOVERY_OUTCOME_CANNOT_SATISFY,
                    "reason": f"Recovery transport infeasible: {edge_summary}",
                    "affected_leg_id": affected_leg_id,
                    "swapped_from": swapped_from,
                    "swapped_to": swapped_to,
                }

        # ------------------------------------------------------------------
        # Step 5: Critic gate re-verification.
        # ------------------------------------------------------------------
        critic_payload = {
            "user_id": user_id,
            "total_budget_cents": total_budget_cents,
            "legs": transport_legs,
            "planned_leg_count": len(recovery_legs),
            "transport_result": transport_result,
        }
        critic_result = self._call_critic(critic_payload)

        if critic_result is not None and critic_result.get("decision") != "verified":
            violations = critic_result.get("violations", [])
            v_summary = "; ".join(
                f"{v['code']} ({v.get('leg_id','pkg')}): {v['detail'][:80]}"
                for v in violations
            )
            logger.warning("recovery: Critic rejected recovery plan — %s", v_summary)
            return {
                "outcome": RECOVERY_OUTCOME_CANNOT_SATISFY,
                "reason": f"Recovery plan rejected by Critic: {v_summary}",
                "affected_leg_id": affected_leg_id,
                "swapped_from": swapped_from,
                "swapped_to": swapped_to,
                "critic_result": critic_result,
            }

        # ------------------------------------------------------------------
        # Step 6: Budget ceiling check.
        # The recovery total must be ≤ user budget.
        # This is also enforced server-side by the merchant at commit time,
        # but we check here for early honest failure + better user message.
        # ------------------------------------------------------------------
        if recovery_total_cents > total_budget_cents:
            logger.warning(
                "recovery: recovery total %d¢ > budget %d¢ — cannot_satisfy",
                recovery_total_cents, total_budget_cents,
            )
            return {
                "outcome": RECOVERY_OUTCOME_CANNOT_SATISFY,
                "reason": (
                    f"Recovery total {recovery_total_cents}¢ exceeds user budget "
                    f"{total_budget_cents}¢. No in-budget recovery available."
                ),
                "affected_leg_id": affected_leg_id,
                "swapped_from": swapped_from,
                "swapped_to": swapped_to,
            }

        # ------------------------------------------------------------------
        # Step 7: CREATE a new checkout for the recovery package.
        # Note: this is NOT committed yet.  The user must re-consent via a
        # fresh mandate before complete_checkout is called.
        # This is the §12.8 RESOLVED flow: recovery is NOT autonomous.
        # ------------------------------------------------------------------
        recovery_idempotency_key = f"recovery-{uuid.uuid4()}"

        line_items = [
            {
                "hotel_id": l["hotel_id"],
                "checkin": l["checkin"],
                "checkout": l["checkout"],
                "adults": l.get("adults", 1),
            }
            for l in recovery_legs
        ]

        check_payload = {
            "user_id": user_id,
            "line_items": line_items,
            "total_budget_cents": total_budget_cents,
            "idempotency_key": recovery_idempotency_key,
        }

        try:
            check_result = self._call_budget_check(check_payload)
        except Exception as exc:
            logger.error("recovery: budget.check failed: %s", exc)
            return {
                "outcome": RECOVERY_OUTCOME_CANNOT_SATISFY,
                "reason": f"Recovery checkout creation failed: {exc}",
            }

        check_decision = check_result.get("decision", "")
        if check_decision not in ("accept", "check_ok"):
            veto_reason = check_result.get("veto_reason", check_decision)
            logger.warning("recovery: budget check veto on recovery plan: %s", veto_reason)
            return {
                "outcome": RECOVERY_OUTCOME_CANNOT_SATISFY,
                "reason": f"Recovery plan vetoed by merchant: {veto_reason}",
                "affected_leg_id": affected_leg_id,
                "swapped_from": swapped_from,
                "swapped_to": swapped_to,
            }

        recovery_checkout_id = check_result.get("checkout_id", "")

        logger.info(
            "recovery: plan assembled — affected=%s %s→%s total=%d¢ checkout=%s source=%s",
            affected_leg_id, swapped_from, swapped_to,
            recovery_total_cents, recovery_checkout_id, source,
        )

        # ------------------------------------------------------------------
        # Return "recovery_ready" — awaiting ONE fresh re-consent from user.
        # The caller (orchestrator / UI layer) presents this to the user and
        # calls commit_recovery() once the user signs a fresh mandate.
        # ------------------------------------------------------------------
        return {
            "outcome": RECOVERY_OUTCOME_READY,
            "recovery_legs": recovery_legs,
            "affected_leg_id": affected_leg_id,
            "swapped_from": swapped_from,
            "swapped_to": swapped_to,
            "recovery_checkout_id": recovery_checkout_id,
            "recovery_total_cents": recovery_total_cents,
            "critic_result": critic_result,
            "transport_result": transport_result,
            "source": source,
            "idempotency_key": recovery_idempotency_key,
            # Human-readable summary for the consent prompt
            "recovery_summary": (
                f"Hotel {swapped_from!r} on leg {affected_leg_id} is no longer available. "
                f"Proposed swap: {swapped_to!r} — total cost {recovery_total_cents}¢ "
                f"(within budget {total_budget_cents}¢)."
            ),
        }

    def commit_recovery(
        self,
        recovery_ready: dict,
        fresh_mandate: dict,
        user_id: str,
    ) -> dict[str, Any]:
        """
        Complete the recovery booking after ONE fresh re-consent from the user.

        This is the §12.8 RESOLVED human-gated step:
          - The human has reviewed the recovery plan and signed a fresh mandate
            over the new checkout_id.
          - We call budget.commit with the fresh mandate.
          - The merchant verifies the mandate server-side (verifyMandate binds
            checkout_id, budget_ceiling, currency, freshness).
          - On success: a new booking_ref is issued.

        Args:
            recovery_ready:  The dict returned by recover() with outcome=recovery_ready.
            fresh_mandate:   The user-signed ap2_mandate dict for the new checkout.
                             Must bind to recovery_ready["recovery_checkout_id"].
            user_id:         The user's ID.

        Returns:
          {
            "outcome": "recovery_committed",
            "booking_ref": str,        # new booking reference
            "checkout_id": str,        # the new checkout ID
            "recovery_legs": [...],
            "recovery_total_cents": int,
            "swapped_from": str,
            "swapped_to": str,
            "affected_leg_id": str,
          }
          or
          {
            "outcome": "cannot_satisfy",
            "reason": str,
          }
        """
        if recovery_ready.get("outcome") != RECOVERY_OUTCOME_READY:
            return {
                "outcome": RECOVERY_OUTCOME_CANNOT_SATISFY,
                "reason": "commit_recovery called with non-ready recovery plan",
            }

        checkout_id = recovery_ready.get("recovery_checkout_id", "")
        idempotency_key = recovery_ready.get("idempotency_key", f"recovery-commit-{uuid.uuid4()}")

        commit_payload = {
            "user_id": user_id,
            "checkout_id": checkout_id,
            "ap2_mandate": fresh_mandate,
            "idempotency_key": idempotency_key,
        }

        try:
            commit_result = self._call_budget_commit(commit_payload)
        except Exception as exc:
            logger.error("utils.recovery.commit: budget.commit failed: %s", exc)
            return {
                "outcome": RECOVERY_OUTCOME_CANNOT_SATISFY,
                "reason": f"Recovery commit failed: {exc}",
            }

        commit_decision = commit_result.get("decision", "")
        if commit_decision != "accept":
            reason = commit_result.get("veto_reason") or commit_result.get("reason", commit_decision)
            logger.warning("utils.recovery.commit: merchant rejected commit: %s", reason)
            return {
                "outcome": RECOVERY_OUTCOME_CANNOT_SATISFY,
                "reason": f"Recovery commit rejected by merchant: {reason}",
                "checkout_id": checkout_id,
            }

        booking_ref = commit_result.get("booking_ref", "")
        logger.info(
            "utils.recovery.commit: SUCCESS — new booking_ref=%s checkout=%s %s→%s total=%d¢",
            booking_ref, checkout_id,
            recovery_ready.get("swapped_from"), recovery_ready.get("swapped_to"),
            recovery_ready.get("recovery_total_cents", 0),
        )

        return {
            "outcome": RECOVERY_OUTCOME_COMMITTED,
            "booking_ref": booking_ref,
            "checkout_id": checkout_id,
            "recovery_legs": recovery_ready.get("recovery_legs", []),
            "recovery_total_cents": recovery_ready.get("recovery_total_cents", 0),
            "swapped_from": recovery_ready.get("swapped_from", ""),
            "swapped_to": recovery_ready.get("swapped_to", ""),
            "affected_leg_id": recovery_ready.get("affected_leg_id", ""),
            "source": recovery_ready.get("source", ""),
        }

    def cancel_recovery(
        self,
        *,
        checkout_id: str,
        booking_ref: str,
        user_id: str,
    ) -> dict[str, Any]:
        """
        VOID a previously-committed booking (§12.1 cascade — release the
        SUPERSEDED itinerary so exactly one stays live).

        Calls budget.cancel → merchant cancel_checkout, which transitions the
        checkout to ``cancelled`` and releases its booking_ref.  The merchant
        cancel is idempotent (unknown/already-cancelled → safe success) and a
        cancelled checkout can never be re-completed.

        This NEVER raises: a void failure is non-fatal to the cascade (the new
        booking is already valid).  It returns a result dict the caller can log:
          {"voided": bool, "checkout_id": str, "booking_ref": str,
           "decision": str, "reason"?: str}
        """
        if not checkout_id:
            return {"voided": False, "checkout_id": "", "booking_ref": booking_ref,
                    "decision": "skipped", "reason": "no checkout_id to void"}

        cancel_payload = {"user_id": user_id, "checkout_id": checkout_id}
        try:
            cancel_result = self._call_budget_cancel(cancel_payload)
        except Exception as exc:
            logger.warning(
                "utils.recovery.cancel: budget.cancel failed for checkout=%s (%s) — "
                "the NEW booking stands; superseded booking may remain live",
                checkout_id, exc,
            )
            return {"voided": False, "checkout_id": checkout_id, "booking_ref": booking_ref,
                    "decision": "error", "reason": str(exc)}

        decision = cancel_result.get("decision", "")
        if decision == "cancelled":
            logger.info(
                "utils.recovery.cancel: VOIDED superseded booking_ref=%s checkout=%s "
                "(released; exactly one itinerary now live)",
                booking_ref, checkout_id,
            )
            # SIMULATED prepaid wallet CREDIT — emit the side-channel refund event
            # (var-0-exempt; fully guarded). Only fires when the merchant actually
            # credited a previously-debited booking (wallet_credit_cents present).
            if cancel_result.get("wallet_credit_cents") is not None:
                try:
                    self._tracer(
                        "wallet", "Wallet",
                        summary="wallet credited (void/refund)",
                        data={
                            "op": "credit",
                            "amount_cents": cancel_result.get("wallet_credit_cents"),
                            "balance_cents": cancel_result.get("wallet_balance_cents"),
                            "checkout_id": checkout_id,
                            "booking_ref": cancel_result.get("released_booking_ref", "") or booking_ref,
                            "simulated": True,
                        },
                    )
                except Exception:  # noqa: BLE001
                    pass
            return {"voided": True, "checkout_id": checkout_id, "booking_ref": booking_ref,
                    "decision": "cancelled",
                    "released_booking_ref": cancel_result.get("released_booking_ref", "")}

        logger.warning(
            "utils.recovery.cancel: void of checkout=%s did NOT confirm (decision=%s reason=%s) — "
            "the NEW booking stands; superseded booking may remain live",
            checkout_id, decision, cancel_result.get("reason", "?"),
        )
        return {"voided": False, "checkout_id": checkout_id, "booking_ref": booking_ref,
                "decision": decision, "reason": cancel_result.get("reason", "")}

    # ==================================================================
    # MULTI-FAULT CASCADE RECOVERY (§12.1) — the marquee depth-add.
    #
    # The grounding: a real flight-dependent trip derailed via a CASCADE —
    # an internal flight cancelled hours before departure → forced same-day
    # reroute + an unplanned overnight; then ANOTHER internal flight cancelled
    # overnight → a second reroute. The society must survive this:
    #   detect fault → recover → detect NEXT fault (on the NEW state) → recover
    #   → …, each cycle within budget, each with ONE fresh re-consent, honest
    #   cannot_satisfy if a fault is genuinely unrecoverable.
    #
    # Design contract preserved from the single-fault core:
    #   - NEVER autonomous re-spend: every cycle = a fresh checkout the human
    #     re-authorizes (H5 / §12.8 RESOLVED). The merchant re-enforces budget.
    #   - Deterministic where clamped: same fault sequence on the same baseline
    #     → same recovery (variance-0).
    #   - Honest terminal: if any fault has no in-budget recovery → cannot_satisfy,
    #     no fabrication, no partial spend.
    # ==================================================================

    def _recover_flight_cancel(
        self,
        *,
        current_legs: list[dict],
        from_city: str,
        to_city: str,
        reroute_option: dict | None,
        user_id: str,
        total_budget_cents: int,
    ) -> dict[str, Any]:
        """
        Recover a cancelled inter-leg flight/transfer (Transport-owned re-route).

        A cancelled flight does not change WHICH hotels are booked — it breaks the
        ROUTE between two legs. The deterministic recovery is a re-route supplied
        as ``reroute_option`` (a pre-vetted Plan B for that edge — e.g. re-route
        via a hub with an unplanned overnight, or shift the affected leg's day),
        expressed as a full replacement leg list.

        The re-route is gated EXACTLY like a hotel swap:
          Transport re-verify (the cancelled edge must be gone) → Critic → budget
          → fresh checkout (NOT committed; awaits one fresh re-consent).

        Returns a ``recovery_ready`` dict (compatible with commit_recovery) or
        ``cannot_satisfy``.
        """
        fc = (from_city or "").strip().lower()
        tc = (to_city or "").strip().lower()

        if reroute_option is None or not reroute_option.get("feasible"):
            return {
                "outcome": RECOVERY_OUTCOME_CANNOT_SATISFY,
                "reason": (
                    f"Flight {from_city!r}→{to_city!r} cancelled and no in-budget "
                    f"re-route is available."
                ),
                "fault_type": FAULT_FLIGHT_CANCEL,
                "cancelled_edge": [fc, tc],
            }

        reroute_legs: list[dict] = reroute_option.get("legs", [])
        if not reroute_legs:
            return {
                "outcome": RECOVERY_OUTCOME_CANNOT_SATISFY,
                "reason": f"Re-route for {from_city!r}→{to_city!r} produced no legs.",
                "fault_type": FAULT_FLIGHT_CANCEL,
                "cancelled_edge": [fc, tc],
            }

        # Transport re-verify: the re-routed leg sequence must NOT contain the
        # cancelled edge anymore (the whole point of the re-route).
        transport_legs = [
            {
                "leg_id": l["leg_id"],
                "city": l.get("city", ""),
                "area": l.get("area", l.get("city", "")),
                "checkin": l.get("checkin", ""),
                "checkout": l.get("checkout", ""),
                "adults": l.get("adults", 1),
                "hotel_id": l.get("hotel_id", ""),
                "total_cents": l.get("total_cents", 0),
                "provenance": l.get("provenance", "merchant"),
            }
            for l in reroute_legs
        ]
        # Pass the cancelled edge so Transport confirms the re-route avoids it.
        transport_payload = {
            "legs": transport_legs,
            "cancelled_transfers": [[fc, tc]],
        }
        transport_result = None
        if self._transport_client is not None or self._transport_url is not None:
            transport_result = self._dispatch(
                self._transport_client, self._transport_url,
                transport_payload, "transport.feasibility",
            )
            infeasible = transport_result.get("infeasible_edges", [])
            if infeasible:
                edge_summary = "; ".join(
                    f"{e.get('from_leg','?')}→{e.get('to_leg','?')}" for e in infeasible
                )
                return {
                    "outcome": RECOVERY_OUTCOME_CANNOT_SATISFY,
                    "reason": (
                        f"Re-route for cancelled flight {from_city!r}→{to_city!r} is still "
                        f"infeasible: {edge_summary}."
                    ),
                    "fault_type": FAULT_FLIGHT_CANCEL,
                    "cancelled_edge": [fc, tc],
                    "transport_result": transport_result,
                }

        # Critic re-verify.
        critic_result = self._call_critic({
            "user_id": user_id,
            "total_budget_cents": total_budget_cents,
            "legs": transport_legs,
            "planned_leg_count": len(reroute_legs),
            "transport_result": transport_result,
        })
        if critic_result is not None and critic_result.get("decision") != "verified":
            violations = critic_result.get("violations", [])
            v_summary = "; ".join(
                f"{v['code']} ({v.get('leg_id','pkg')}): {v['detail'][:80]}"
                for v in violations
            )
            return {
                "outcome": RECOVERY_OUTCOME_CANNOT_SATISFY,
                "reason": f"Re-route rejected by Critic: {v_summary}",
                "fault_type": FAULT_FLIGHT_CANCEL,
                "cancelled_edge": [fc, tc],
                "critic_result": critic_result,
            }

        reroute_total_cents = int(reroute_option.get("total_cents", sum(
            int(l.get("total_cents", 0)) for l in reroute_legs
        )))

        # Budget ceiling (the merchant re-enforces this too at commit).
        if reroute_total_cents > total_budget_cents:
            return {
                "outcome": RECOVERY_OUTCOME_CANNOT_SATISFY,
                "reason": (
                    f"Re-route total {reroute_total_cents}¢ exceeds budget "
                    f"{total_budget_cents}¢."
                ),
                "fault_type": FAULT_FLIGHT_CANCEL,
                "cancelled_edge": [fc, tc],
            }

        # Fresh checkout (NOT committed — awaits one fresh re-consent).
        recovery_idempotency_key = f"recovery-{uuid.uuid4()}"
        line_items = [
            {
                "hotel_id": l["hotel_id"],
                "checkin": l["checkin"],
                "checkout": l["checkout"],
                "adults": l.get("adults", 1),
            }
            for l in reroute_legs
        ]
        try:
            check_result = self._call_budget_check({
                "user_id": user_id,
                "line_items": line_items,
                "total_budget_cents": total_budget_cents,
                "idempotency_key": recovery_idempotency_key,
            })
        except Exception as exc:
            return {
                "outcome": RECOVERY_OUTCOME_CANNOT_SATISFY,
                "reason": f"Re-route checkout creation failed: {exc}",
                "fault_type": FAULT_FLIGHT_CANCEL,
                "cancelled_edge": [fc, tc],
            }

        if check_result.get("decision", "") not in ("accept", "check_ok"):
            veto_reason = check_result.get("veto_reason", check_result.get("decision"))
            return {
                "outcome": RECOVERY_OUTCOME_CANNOT_SATISFY,
                "reason": f"Re-route vetoed by merchant: {veto_reason}",
                "fault_type": FAULT_FLIGHT_CANCEL,
                "cancelled_edge": [fc, tc],
            }

        recovery_checkout_id = check_result.get("checkout_id", "")
        logger.info(
            "recovery: flight-cancel re-route assembled — %s→%s avoided, "
            "legs=%d total=%d¢ checkout=%s",
            fc, tc, len(reroute_legs), reroute_total_cents, recovery_checkout_id,
        )

        return {
            "outcome": RECOVERY_OUTCOME_READY,
            "recovery_legs": reroute_legs,
            "fault_type": FAULT_FLIGHT_CANCEL,
            "cancelled_edge": [fc, tc],
            "affected_leg_id": reroute_option.get("affected_leg_id", ""),
            "swapped_from": f"flight:{fc}->{tc}",
            "swapped_to": reroute_option.get("reroute_label", "re-routed"),
            "recovery_checkout_id": recovery_checkout_id,
            "recovery_total_cents": reroute_total_cents,
            "critic_result": critic_result,
            "transport_result": transport_result,
            "source": "reroute",
            "idempotency_key": recovery_idempotency_key,
            "recovery_summary": (
                f"Flight {from_city!r}→{to_city!r} cancelled. "
                f"Re-routed ({reroute_option.get('reroute_label','via hub + overnight')}) "
                f"— total {reroute_total_cents}¢ (within budget {total_budget_cents}¢)."
            ),
        }

    def recover_cascade(
        self,
        *,
        original_booking: dict,
        faults: list[dict],
        sign_mandate,
        user_id: str,
        total_budget_cents: int,
    ) -> dict[str, Any]:
        """
        Survive a SEQUENCE of faults (the multi-fault cascade, §12.1).

        Processes faults in order. Each cycle:
          1. Detect the fault against the CURRENT booked state.
          2. Recover (hotel swap via recover(), or flight re-route via
             _recover_flight_cancel()) within budget, Critic-verified.
          3. ONE fresh re-consent: the caller's ``sign_mandate(recovery_ready)``
             produces a fresh mandate the human authorizes; commit_recovery()
             completes a NEW checkout → NEW booking_ref.
          4. VOID the SUPERSEDED booking (commit-new FIRST, then void-old):
             the immediately-prior committed booking is cancelled at the merchant
             (cancel_checkout releases its booking_ref). Cycle 1 voids the
             baseline (only if the baseline was actually committed); cycle N voids
             cycle N-1's checkout. There is never a window with zero live
             bookings (a brief 2-booking overlap is fine, a gap is not). A void
             failure is logged + surfaced but does NOT fail the cascade.
          5. The NEXT fault then hits the NEW state and recovers again.

        Exactly-one-live-booking invariant (§12.1): because each cycle voids the
        superseded booking, at any time exactly ONE itinerary is live, and the
        cumulative committed spend equals the FINAL booking total (each cycle's
        recovery_total_cents is the full itinerary cost) — NOT the sum of the
        per-cycle totals. No double-charge across cycles.

        If any fault is unrecoverable → STOP with an honest cannot_satisfy. The
        single live booking from the last successful cycle stands (still exactly
        one); no fabrication, no partial spend on the failing fault.

        Args:
            original_booking:    The booked baseline (negotiate() success result),
                                 with "legs" (hotel_id per leg).
            faults:              Ordered list of fault dicts. Each fault:
                                 hotel:  {"type":"hotel_sold_out",
                                          "hotel_id": str,
                                          "secondary_plan": {...}|None,
                                          "per_leg_candidates": [...]|None}
                                 flight: {"type":"flight_cancel",
                                          "from_city": str, "to_city": str,
                                          "reroute_option": {...}|None}
            sign_mandate:        Callable(recovery_ready: dict) -> mandate dict.
                                 Represents the ONE fresh human re-consent per
                                 cycle (the merchant re-verifies it). For the
                                 mock/demo path this returns the L2 consent shape.
            user_id:             The user's ID.
            total_budget_cents:  The budget ceiling (held constant each cycle).

        Returns:
          {
            "outcome": "cascade_survived",
            "cycles": [ {fault, booking_ref, checkout_id,
                         voided_booking_ref, voided_checkout_id, ...}, ... ],
            "final_booking_ref": str,    # the ONE remaining live booking
            "final_legs": [...],
            "final_total_cents": int,    # == cumulative committed spend (no double-charge)
            "fault_count": int,
          }
          or
          {
            "outcome": "cannot_satisfy",
            "reason": str,
            "failed_fault": {...},
            "failed_cycle_index": int,
            "cycles": [ ... cycles that DID succeed ... ],
          }
        """
        current_booking = dict(original_booking)
        current_legs: list[dict] = list(current_booking.get("legs", []))
        cycles: list[dict] = []

        # The booking that is currently LIVE and would be superseded by the next
        # successful commit. Seeded from the baseline ONLY if the baseline was
        # actually COMMITTED (carries a committed checkout_id + booking_ref). If
        # the baseline was merely CHECKED (never committed), there is nothing to
        # void on cycle 1 — the void is conditional. (§12.1 exactly-one-live.)
        prev_committed_checkout: str = original_booking.get("checkout_id", "") or ""
        prev_committed_ref: str = original_booking.get("booking_ref", "") or ""

        logger.info(
            "cascade: starting — %d fault(s), budget=%d¢, user=%s, baseline_checkout=%s",
            len(faults), total_budget_cents, user_id, prev_committed_checkout or "(none)",
        )

        for idx, fault in enumerate(faults):
            ftype = fault.get("type", "")
            logger.info("cascade: cycle %d/%d — fault=%s", idx + 1, len(faults), ftype)

            # ---- Step 1+2: detect + recover this fault on the current state ----
            if ftype == FAULT_HOTEL_SOLD_OUT:
                recovery_ready = self.recover(
                    original_booking=current_booking,
                    unavailable_hotel_id=fault.get("hotel_id", ""),
                    secondary_plan=fault.get("secondary_plan"),
                    per_leg_candidates=fault.get("per_leg_candidates"),
                    user_id=user_id,
                    total_budget_cents=total_budget_cents,
                )
            elif ftype == FAULT_FLIGHT_CANCEL:
                recovery_ready = self._recover_flight_cancel(
                    current_legs=current_legs,
                    from_city=fault.get("from_city", ""),
                    to_city=fault.get("to_city", ""),
                    reroute_option=fault.get("reroute_option"),
                    user_id=user_id,
                    total_budget_cents=total_budget_cents,
                )
            else:
                return {
                    "outcome": CASCADE_OUTCOME_CANNOT_SATISFY,
                    "reason": f"Unknown fault type {ftype!r} at cycle {idx + 1}.",
                    "failed_fault": fault,
                    "failed_cycle_index": idx,
                    "cycles": cycles,
                }

            # ---- Honest terminal: this fault is unrecoverable ----
            if recovery_ready.get("outcome") != RECOVERY_OUTCOME_READY:
                logger.info(
                    "cascade: cycle %d UNRECOVERABLE — %s",
                    idx + 1, recovery_ready.get("reason", "?"),
                )
                return {
                    "outcome": CASCADE_OUTCOME_CANNOT_SATISFY,
                    "reason": recovery_ready.get("reason", "fault unrecoverable"),
                    "failed_fault": fault,
                    "failed_cycle_index": idx,
                    "recovery_result": recovery_ready,
                    "cycles": cycles,
                }

            # ---- Step 3: ONE fresh re-consent → commit a NEW checkout ----
            # Never autonomous: the human re-authorizes via sign_mandate (H5).
            fresh_mandate = sign_mandate(recovery_ready)
            commit = self.commit_recovery(recovery_ready, fresh_mandate, user_id)

            if commit.get("outcome") != RECOVERY_OUTCOME_COMMITTED:
                logger.info(
                    "cascade: cycle %d commit failed — %s",
                    idx + 1, commit.get("reason", "?"),
                )
                return {
                    "outcome": CASCADE_OUTCOME_CANNOT_SATISFY,
                    "reason": (
                        f"Re-consent/commit failed at cycle {idx + 1}: "
                        f"{commit.get('reason', '?')}"
                    ),
                    "failed_fault": fault,
                    "failed_cycle_index": idx,
                    "recovery_result": recovery_ready,
                    "commit_result": commit,
                    "cycles": cycles,
                }

            new_checkout = commit.get("checkout_id", "")
            new_ref = commit.get("booking_ref", "")

            # ---- Step 4: VOID the SUPERSEDED booking (commit-new FIRST, then
            # void-old). The new booking is already committed above, so there is
            # NEVER a window with zero live bookings — a brief 2-booking overlap
            # is fine, a gap is not. Cycle 1 voids the baseline (only if it was
            # actually committed); cycle N voids cycle N-1's checkout. A void
            # failure is logged + surfaced but does NOT fail the cascade (the new
            # booking is valid). End state: exactly ONE live booking. (§12.1)
            voided_checkout = ""
            voided_ref = ""
            if prev_committed_checkout:
                void_result = self.cancel_recovery(
                    checkout_id=prev_committed_checkout,
                    booking_ref=prev_committed_ref,
                    user_id=user_id,
                )
                if void_result.get("voided"):
                    voided_checkout = prev_committed_checkout
                    voided_ref = prev_committed_ref
                else:
                    logger.warning(
                        "cascade: cycle %d — superseded booking_ref=%s checkout=%s "
                        "did NOT void cleanly (%s); the NEW booking stands",
                        idx + 1, prev_committed_ref, prev_committed_checkout,
                        void_result.get("reason", void_result.get("decision", "?")),
                    )

            # ---- advance the state — next fault hits the NEW booking ----
            current_legs = list(recovery_ready.get("recovery_legs", current_legs))
            current_booking = {
                **current_booking,
                "legs": current_legs,
                "booking_ref": new_ref,
                "checkout_id": new_checkout,
                "total_booked_cents": recovery_ready.get("recovery_total_cents", 0),
            }
            cycles.append({
                "cycle_index": idx,
                "fault": fault,
                "fault_type": ftype,
                "source": recovery_ready.get("source", ""),
                "recovery_total_cents": recovery_ready.get("recovery_total_cents", 0),
                "booking_ref": new_ref,
                "checkout_id": new_checkout,
                "voided_booking_ref": voided_ref,
                "voided_checkout_id": voided_checkout,
                "summary": recovery_ready.get("recovery_summary", ""),
            })
            logger.info(
                "cascade: cycle %d RECOVERED — booking_ref=%s total=%d¢ (fresh re-consent); "
                "superseded=%s voided=%s",
                idx + 1, new_ref,
                recovery_ready.get("recovery_total_cents", 0),
                prev_committed_ref or "(none)", voided_ref or "(none)",
            )
            # The booking just committed becomes the one to supersede next cycle.
            prev_committed_checkout = new_checkout
            prev_committed_ref = new_ref

        # All faults survived.
        final_total = (
            cycles[-1]["recovery_total_cents"] if cycles
            else current_booking.get("total_booked_cents", 0)
        )
        logger.info(
            "cascade: SURVIVED all %d fault(s) — final booking_ref=%s total=%d¢",
            len(faults),
            current_booking.get("booking_ref", ""),
            final_total,
        )
        return {
            "outcome": CASCADE_OUTCOME_SURVIVED,
            "cycles": cycles,
            "fault_count": len(faults),
            "final_booking_ref": current_booking.get("booking_ref", ""),
            "final_checkout_id": current_booking.get("checkout_id", ""),
            "final_legs": current_legs,
            "final_total_cents": final_total,
            "total_budget_cents": total_budget_cents,
        }
