"""
cost_breaker.py — process-level daily "denial-of-wallet" breaker.

Why
---
Once the auth wall drops and this PUBLIC judging engine faces the open internet,
the expensive PAID downstreams have no global daily ceiling:
  • DashScope LLM   — POST /negotiate_text intent parse (+ optional narrate)
  • Google Places   — POST /place_card (detail/autocomplete) + GET /place_photo
A scraper hammering those paths could run up an unbounded LLM/Places bill. This
module is a lightweight, process-level daily counter with an env-configured
ceiling per cost class, plus an explicit kill-switch — a request GATE that decides
whether the paid call is made.

Design guarantees (var-0 / judging reproducibility)
---------------------------------------------------
- DEFAULT OFF: with no cap env set and no kill-switch, allow() returns True BEFORE
  reading the clock or touching any lock/counter — zero side effects. The judging /
  UAT path is byte-identical to today.
- GATE ONLY: it never contributes to the planning digest, day_plans, cache keys, or
  any served field. It decides whether a paid call happens; it never alters a
  call's inputs or outputs.
- GRACEFUL DEGRADE: when enabled and the daily cap is exceeded (or the kill-switch
  is set), the caller degrades honestly — deterministic-parse fallback (LLM),
  no-narrative (narrate), or "temporarily unavailable" (Places) — never an
  exception to the client, never a paid call.

Env
---
- SOCIETY_DAILY_LLM_CAP     int — max DashScope LLM ops (parse + narrate) per UTC day.
- SOCIETY_DAILY_PLACES_CAP  int — max Google Places ops (detail/autocomplete/photo)
                                  per UTC day.
    unset / blank / non-integer → that class is UNGATED (disabled → byte-identical).
    0                           → trip immediately (zero paid calls allowed).
- SOCIETY_PLANNING_DISABLED  1/true/yes/on — global kill-switch: EVERY class trips
                                  immediately, regardless of the caps above.

Counters are process-local (per worker). A multi-worker deploy gets an effective
ceiling of roughly N_workers × cap — acceptable for a coarse wallet backstop; a
shared Redis/DB counter is a noted, non-blocking future refinement. The cap is a
budget SAFETY ceiling, not an exact accountant.
"""
from __future__ import annotations

import datetime
import os
import threading

# Cost classes → the env var that caps them.
_CLASS_ENV = {
    "llm": "SOCIETY_DAILY_LLM_CAP",
    "places": "SOCIETY_DAILY_PLACES_CAP",
}
_KILL_ENV = "SOCIETY_PLANNING_DISABLED"
_TRUE = {"1", "true", "yes", "on"}


class CostBreaker:
    """A per-UTC-day counter of expensive paid operations, per cost class.

    Thread-safe. Config (caps + kill-switch) is read from the environment on every
    call so a deploy can flip the switch without a restart and tests can toggle it
    in-process.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._day: str | None = None
        self._counts: dict[str, int] = {}

    @staticmethod
    def _killed() -> bool:
        return os.environ.get(_KILL_ENV, "").strip().lower() in _TRUE

    @staticmethod
    def _cap(cost_class: str) -> int | None:
        """The configured daily cap for a class, or None when UNGATED (disabled)."""
        raw = os.environ.get(_CLASS_ENV.get(cost_class, ""), "").strip()
        if not raw:
            return None
        try:
            return max(0, int(raw))
        except ValueError:
            # A malformed cap must fail OPEN (ungated) — never silently trip the
            # breaker on a typo and take down the judging path.
            return None

    @staticmethod
    def _today() -> str:
        return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")

    def allow(self, cost_class: str) -> bool:
        """Reserve one unit of the given paid-cost class for today.

        Returns True (and consumes one unit) when the call may proceed; False when
        the breaker is tripped (kill-switch on, or the daily cap already reached).

        DEFAULT-OFF fast path: an unset/blank cap returns True with NO clock read,
        NO lock, and NO mutation — so the judging path is byte-identical and pays
        zero overhead. The clock is only ever read when a cap is actually set.
        """
        # Kill-switch trips every class immediately (cheap env read, checked first).
        if self._killed():
            return False
        cap = self._cap(cost_class)
        if cap is None:
            return True  # ungated → byte-identical no-op (no clock / lock / counter)
        today = self._today()
        with self._lock:
            if today != self._day:  # new UTC day → reset counters
                self._day = today
                self._counts = {}
            used = self._counts.get(cost_class, 0)
            if used >= cap:
                return False
            self._counts[cost_class] = used + 1
            return True

    def snapshot(self) -> dict:
        """Read-only diagnostics view (NEVER used on any served/var-0 path)."""
        with self._lock:
            return {
                "day": self._day,
                "counts": dict(self._counts),
                "killed": self._killed(),
                "caps": {k: self._cap(k) for k in _CLASS_ENV},
            }


# Process-wide singleton — one wallet ceiling shared by every request in this worker.
_breaker = CostBreaker()


def get_breaker() -> CostBreaker:
    """Return the process-wide CostBreaker singleton."""
    return _breaker


def allow(cost_class: str) -> bool:
    """Module-level convenience: reserve one unit of a paid-cost class for today."""
    return _breaker.allow(cost_class)
