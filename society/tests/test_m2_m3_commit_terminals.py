"""test_m2_m3_commit_terminals.py — MEDIUM regressions in the COMMIT-time
decision ladder (commit_plan / _map_complete_response).

M2: commit_plan() — the /confirm production entrypoint (distinct from the
atomic /negotiate commit=true loop's _run_negotiation_rounds, which already
has its own needs_consent/needs_mandate ladder via _needs_consent_terminal) —
conflated needs_consent/needs_mandate/veto commit decisions into a generic
"commit_errored"+needs_reconciliation terminal telling the client to retry the
SAME idempotency_key — a retry that can never succeed for any of these three
cases (the merchant will just ask for consent/mandate again, or re-price
again) and misstates the real, actionable cause.

FIX: commit_plan() now mirrors the loop's decision ladder — needs_consent/
needs_mandate -> the needs_consent outcome shape; a genuine re-price veto ->
an honest veto terminal WITHOUT needs_reconciliation; commit_failed (a
genuinely ambiguous RAISED commit) is the ONLY case that still carries
needs_reconciliation.

M3: budget_agent._map_complete_response had no mapping for a merchant 409
verdict at COMMIT time (status:"void"/reason:"counterparty_insolvent",
status:"error"/reason:"item_unavailable") — both fell through to a terminal
`raise ValueError`, which _do_commit converts to decision "commit_failed"
(documented as "server-side state is AMBIGUOUS, retry to reconcile"). A 409
void/unavailable is a DEFINITE merchant-side outcome (not booked, session
voided) — advising a same-key retry can never succeed.

FIX: _map_complete_response now maps status "void"/"error" to decision
"unavailable" (mirrors the CHECK-time _cannot_price_result vocabulary), which
_do_commit's callers (commit_plan's M2 ladder, and the two commit-time call
sites inside _run_negotiation_rounds) now turn into an honest non-
reconciliation terminal instead of commit_failed.
"""
import copy
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from agents.budget_agent import BudgetAgent
from tests.test_trace_var0 import _SeqTransport, _merchant_result, _PM_TRIP
from tests.test_wallet_sim import _build_society


def _agent() -> BudgetAgent:
    return BudgetAgent(merchant_transport=_SeqTransport({}))


# ---------------------------------------------------------------------------
# M3 — unit coverage of _map_complete_response's new void/error branches.
# ---------------------------------------------------------------------------

def test_map_complete_void_counterparty_insolvent_is_unavailable_not_raise():
    agent = _agent()
    sc = {
        "status": "void", "reason": "counterparty_insolvent", "id": "co_1",
        "total_cents": 54000, "currency": "USD",
    }
    r = agent._map_complete_response(sc, 409, "co_1", 54000, "k1")
    assert r["decision"] == "unavailable", r
    assert r["veto_reason"] == "counterparty_insolvent"
    assert r["idempotency_key"] == "k1"


def test_map_complete_error_item_unavailable_is_unavailable_not_raise():
    agent = _agent()
    sc = {
        "status": "error", "reason": "item_unavailable", "id": "co_1",
        "total_cents": 54000, "currency": "USD",
    }
    r = agent._map_complete_response(sc, 409, "co_1", 54000, "k1")
    assert r["decision"] == "unavailable", r
    assert r["veto_reason"] == "item_unavailable"


def test_map_complete_still_raises_for_a_genuinely_unknown_status():
    """REGRESSION guard: the new void/error branches must not swallow every
    unmapped status — a truly unexpected one still raises (→ _do_commit's
    genuinely-ambiguous commit_failed path)."""
    agent = _agent()
    sc = {"status": "totally_unrecognized_status", "id": "co_1", "total_cents": 54000}
    try:
        agent._map_complete_response(sc, 500, "co_1", 54000, "k1")
        assert False, "expected a ValueError for a genuinely unmapped status"
    except ValueError:
        pass


# ---------------------------------------------------------------------------
# M2 — commit_plan()'s decision ladder.
# ---------------------------------------------------------------------------

def _plan():
    """A held plan_ready envelope + orch, via the same harness
    test_commit_plan_requires_dates.py uses (bookable Port-Moresby trip)."""
    from tests.test_trace_var0 import _merchant_result as _mr
    COMPLETE_OK = _mr({
        "id": "co_pm", "status": "complete", "user_id": "trace-test",
        "line_items": [], "total_cents": 54000, "currency": "USD",
        "buyer_consent": True, "booking_ref": "BK-trace-1",
        "wallet_session_id": "trip-x", "wallet_debit_cents": 54000,
        "wallet_balance_cents": 446000, "simulated": True,
    })
    FUND_OK = _mr({
        "status": "ok", "wallet_session_id": "trip-x", "seed_cents": 500000,
        "balance_cents": 500000, "simulated": True, "note": "sim",
    })
    orch = _build_society(complete_resp=(200, COMPLETE_OK), fund_resp=(200, FUND_OK))
    res = orch.negotiate(copy.deepcopy(_PM_TRIP), commit=False)
    return res, orch


def _swap_complete_resp(orch, complete_resp):
    """Point the SAME orchestrator's budget agent at a new complete_checkout
    response for the next commit_plan() call (mirrors how _build_society
    wires the merchant transport, without rebuilding the whole society)."""
    # Simplest robust approach: rebuild just the budget agent's transport.
    from agents.budget_agent import BudgetAgent
    from starlette.testclient import TestClient
    CREATE_OK = _merchant_result({
        "id": "co_pm", "status": "incomplete", "user_id": "trace-test",
        "line_items": [], "total_cents": 54000, "currency": "USD",
        "buyer_consent": False, "booking_ref": "",
    })
    UPDATE_OK = _merchant_result({
        "id": "co_pm", "status": "incomplete", "total_cents": 54000,
        "buyer_consent": True, "currency": "USD",
    })
    budget_transport = _SeqTransport({
        "create_checkout": (200, CREATE_OK),
        "update_checkout": (200, UPDATE_OK),
        "complete_checkout": complete_resp,
    })
    orch._budget_client = TestClient(BudgetAgent(merchant_transport=budget_transport).build_app())


def test_commit_plan_needs_consent_returns_actionable_outcome_not_reconciliation():
    res, orch = _plan()
    ctx = res["_confirm_ctx"]
    envelope = {k: v for k, v in res.items() if k != "_confirm_ctx"}

    NEEDS_CONSENT = _merchant_result({
        "id": "co_pm", "status": "requires_consent", "total_cents": 54000,
        "message": "buyer_consent required (HITL gate)",
    })
    _swap_complete_resp(orch, (200, NEEDS_CONSENT))

    booked = orch.commit_plan(
        user_id=ctx["user_id"], checkout_id=ctx["checkout_id"],
        idempotency_key=ctx["idempotency_key"], plan_envelope=envelope,
        dest_token=ctx["dest_token"])

    assert booked.get("outcome") == "needs_consent", booked
    assert booked.get("reason") == "needs_consent", booked
    assert not booked.get("needs_reconciliation"), (
        "REGRESSION (M2): needs_consent must NOT be conflated with a "
        "genuinely ambiguous commit — retrying the same key can never "
        "satisfy a consent requirement."
    )
    assert booked.get("idempotency_key") == ctx["idempotency_key"]


def test_commit_plan_needs_mandate_returns_actionable_outcome_not_reconciliation():
    res, orch = _plan()
    ctx = res["_confirm_ctx"]
    envelope = {k: v for k, v in res.items() if k != "_confirm_ctx"}

    NEEDS_MANDATE = _merchant_result({
        "id": "co_pm", "status": "requires_mandate", "total_cents": 54000,
        "message": "ap2_mandate required for L3 autonomous checkout",
    })
    _swap_complete_resp(orch, (200, NEEDS_MANDATE))

    booked = orch.commit_plan(
        user_id=ctx["user_id"], checkout_id=ctx["checkout_id"],
        idempotency_key=ctx["idempotency_key"], plan_envelope=envelope,
        dest_token=ctx["dest_token"])

    assert booked.get("outcome") == "needs_consent", booked
    assert booked.get("reason") == "needs_mandate", booked
    assert not booked.get("needs_reconciliation"), (
        "REGRESSION (M2): needs_mandate must NOT be conflated with a "
        "genuinely ambiguous commit."
    )


def test_commit_plan_veto_is_honest_terminal_without_reconciliation():
    res, orch = _plan()
    ctx = res["_confirm_ctx"]
    envelope = {k: v for k, v in res.items() if k != "_confirm_ctx"}

    VETO = _merchant_result({
        "status": "denied", "reason": "price_exceeds_budget", "id": "co_pm",
        "total_cents": 90000, "budget_ceiling_cents": 80000, "currency": "USD",
    })
    _swap_complete_resp(orch, (403, VETO))

    booked = orch.commit_plan(
        user_id=ctx["user_id"], checkout_id=ctx["checkout_id"],
        idempotency_key=ctx["idempotency_key"], plan_envelope=envelope,
        dest_token=ctx["dest_token"])

    assert booked.get("outcome") == "cannot_satisfy", booked
    assert booked.get("reason") == "veto", booked
    assert not booked.get("needs_reconciliation"), (
        "REGRESSION (M2): a genuine commit-time re-price veto is an honest, "
        "actionable outcome (re-plan at the new price) — it must NOT tell "
        "the client the server-side state is ambiguous."
    )


def test_commit_plan_void_counterparty_insolvent_is_honest_terminal_not_reconciliation():
    """M2+M3 integration: a merchant 409 void at COMMIT time must surface as
    an honest, definite terminal — never commit_errored/needs_reconciliation."""
    res, orch = _plan()
    ctx = res["_confirm_ctx"]
    envelope = {k: v for k, v in res.items() if k != "_confirm_ctx"}

    VOID = _merchant_result({
        "id": "co_pm", "status": "void", "reason": "counterparty_insolvent",
        "total_cents": 54000, "currency": "USD",
    })
    _swap_complete_resp(orch, (409, VOID))

    booked = orch.commit_plan(
        user_id=ctx["user_id"], checkout_id=ctx["checkout_id"],
        idempotency_key=ctx["idempotency_key"], plan_envelope=envelope,
        dest_token=ctx["dest_token"])

    assert booked.get("outcome") == "cannot_satisfy", booked
    assert booked.get("reason") in ("unavailable", "cannot_price"), booked
    assert not booked.get("needs_reconciliation"), (
        "REGRESSION (M3): a merchant 409 void/counterparty_insolvent is a "
        "DEFINITE (never-booked) outcome — advising a same-key retry via "
        "needs_reconciliation can never succeed."
    )
    assert booked.get("booking_ref") is None


def test_commit_plan_error_item_unavailable_is_honest_terminal_not_reconciliation():
    res, orch = _plan()
    ctx = res["_confirm_ctx"]
    envelope = {k: v for k, v in res.items() if k != "_confirm_ctx"}

    ERR = _merchant_result({
        "id": "co_pm", "status": "error", "reason": "item_unavailable",
        "total_cents": 54000, "currency": "USD",
    })
    _swap_complete_resp(orch, (409, ERR))

    booked = orch.commit_plan(
        user_id=ctx["user_id"], checkout_id=ctx["checkout_id"],
        idempotency_key=ctx["idempotency_key"], plan_envelope=envelope,
        dest_token=ctx["dest_token"])

    assert booked.get("outcome") == "cannot_satisfy", booked
    assert booked.get("reason") in ("unavailable", "cannot_price"), booked
    assert not booked.get("needs_reconciliation"), (
        "REGRESSION (M3): item_unavailable is a DEFINITE outcome, not an "
        "ambiguous commit."
    )


def test_commit_plan_genuinely_failed_commit_still_needs_reconciliation():
    """Sanity counterpart: a genuinely RAISED/ambiguous commit (the ONE case
    this ladder must still route to needs_reconciliation) is unaffected."""
    res, orch = _plan()
    ctx = res["_confirm_ctx"]
    envelope = {k: v for k, v in res.items() if k != "_confirm_ctx"}

    # An unmapped status still raises inside _map_complete_response, which
    # _do_commit converts to decision "commit_failed" — the one legitimately
    # ambiguous case.
    BROKEN = _merchant_result({
        "id": "co_pm", "status": "totally_unrecognized_status",
        "total_cents": 54000, "currency": "USD",
    })
    _swap_complete_resp(orch, (500, BROKEN))

    booked = orch.commit_plan(
        user_id=ctx["user_id"], checkout_id=ctx["checkout_id"],
        idempotency_key=ctx["idempotency_key"], plan_envelope=envelope,
        dest_token=ctx["dest_token"])

    assert booked.get("outcome") == "cannot_satisfy", booked
    assert booked.get("reason") == "commit_errored", booked
    assert booked.get("needs_reconciliation") is True, (
        "a genuinely ambiguous RAISED commit must still ask for reconciliation"
    )
