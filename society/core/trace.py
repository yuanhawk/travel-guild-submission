"""
trace.py — Side-channel trace event layer for the Travel Guild orchestrator.

Design contract:
  - Var-0 sacred: the tracer is a side-channel NO-OP by default. negotiate()
    output is byte-identical with or without a tracer.
  - NoOpTracer: callable, zero overhead, returns None.
  - CollectingTracer: buffers TraceEvent objects; supports subscribers.
  - Tracer type alias: Callable[[TraceEvent], None] — but CollectingTracer
    uses a convenience signature on __call__ (type, agent, *, trip_id, ...).
  - try/except wrapping is applied at the EMIT SITE (orchestrator.py), not here,
    so a tracer bug never crashes a negotiation.
"""
from __future__ import annotations

import dataclasses
import time
from typing import Any, Callable, Literal

# ---------------------------------------------------------------------------
# EventType literals
# ---------------------------------------------------------------------------
EventType = Literal[
    "negotiate_started",
    "agent_started",
    "agent_completed",
    "agent_skipped",
    "round_started",
    "budget_veto",
    "critic_rejected",
    "gate_blocked",
    "consent_requested",
    "booked",
    "wallet",  # SIMULATED prepaid wallet seed/debit/credit (side-channel → var-0-exempt by contract)
    "negotiate_finished",
]

# ---------------------------------------------------------------------------
# TraceEvent — frozen dataclass (immutable, hashable, deterministic)
# ---------------------------------------------------------------------------

@dataclasses.dataclass(frozen=True)
class TraceEvent:
    seq: int
    ts_ms: float
    type: str          # one of EventType
    agent: str | None
    trip_id: str
    round: int | None
    summary: str
    data: dict

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


# ---------------------------------------------------------------------------
# Tracer type alias
# ---------------------------------------------------------------------------
Tracer = Callable[[TraceEvent], None]


# ---------------------------------------------------------------------------
# NoOpTracer — callable, zero overhead, returns None
# ---------------------------------------------------------------------------

class NoOpTracer:
    """
    A tracer that does nothing. This is the sacred Var-0 default — calling it
    costs nothing and returns None immediately.
    """
    def __call__(self, *args: Any, **kwargs: Any) -> None:  # noqa: ANN401
        return None


# ---------------------------------------------------------------------------
# CollectingTracer — buffers events, notifies subscribers
# ---------------------------------------------------------------------------

class CollectingTracer:
    """
    A tracer that collects TraceEvent objects into self.events and notifies
    any registered subscribers (each called synchronously on emit).

    Convenience __call__ signature (matches what orchestrator emits):
        tracer(type, agent=None, *, trip_id="", round=None, summary="", data=None)

    The raw TraceEvent is appended to self.events and each subscriber is called
    with the event.  Seq counter starts at 0 and is incremented per emit.
    """

    def __init__(self) -> None:
        self.events: list[TraceEvent] = []
        self.subscribers: list[Callable[[TraceEvent], None]] = []
        self._seq: int = 0

    def __call__(
        self,
        type: str,  # noqa: A002 — intentionally named 'type' to match EventType usage
        agent: str | None = None,
        *,
        trip_id: str = "",
        round: int | None = None,  # noqa: A002 — intentionally named 'round'
        summary: str = "",
        data: dict | None = None,
    ) -> None:
        event = TraceEvent(
            seq=self._seq,
            ts_ms=time.time() * 1000.0,
            type=type,
            agent=agent,
            trip_id=trip_id,
            round=round,
            summary=summary,
            data=data if data is not None else {},
        )
        self._seq += 1
        self.events.append(event)
        for sub in self.subscribers:
            try:
                sub(event)
            except Exception:  # noqa: BLE001 — subscriber bugs must never crash the tracer
                pass
