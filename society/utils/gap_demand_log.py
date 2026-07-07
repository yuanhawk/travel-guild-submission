"""gap_demand_log.py — Honest-coverage loop (see travel-agent-planning/HONEST-COVERAGE-LOOP.md).

OBSERVE-ONLY telemetry of what users ask for that the system honestly COULD NOT satisfy. Appends
anonymized, request-SHAPE gap events to a disk JSONL, aggregated offline by scripts/gap_backlog.py
into a demand-ranked "most requested, not yet covered" backlog that drives targeted enrichment.

Hard guarantees (the whole point):
  - OBSERVE-ONLY: written here, read ONLY by the offline aggregator. NEVER read back into any
    runtime decision → var-0 untouched, no fabrication path.
  - ANONYMOUS-FIRST (#40): records the request SHAPE (activity / city / country) only — never user
    identity, IP, or free text beyond the normalized term.
  - NEVER RAISES: a logging failure must never affect the response (called fully-guarded at the
    server boundary, mirroring the #64 telemetry emit).

Hooked at the SERVER boundary (orchestration/server.py), so only live traffic logs; tests and the
e2e harness call orchestrator.negotiate() directly and bypass this entirely.
"""

from __future__ import annotations

import json
import os
import re

_LOG = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".gap_demand_log.jsonl")
# On by default for the live server; set SOCIETY_GAP_LOG=0 to disable.
_ENABLED = os.environ.get("SOCIETY_GAP_LOG", "1") != "0"

# Matches the day-planner's honest note:
#   "...verified POI catalog for 'kyoto': ice cave, glacier — no match found in ..."
# NOTE: coupled to the note literal emitted in agents/day_planner_agent.py (the
# "verified POI catalog for {city!r}: {label} — ..." string). If that wording changes,
# update this regex too, else gap capture silently no-ops (honest miss, not a crash).
_ACTIVITY_NOTE_RE = re.compile(r"verified POI catalog for '([^']+)':\s*(.+?)\s+—")


def _append(event: dict) -> None:
    try:
        with open(_LOG, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n")
    except Exception:
        pass  # observe-only: a write failure must never affect the response


def log_result_gaps(result: dict, request: dict | None = None) -> None:
    """Extract honest gap signals from a FROZEN result (+ request) and append anonymized events.
    Observe-only: never mutates `result`, never read back, never raises."""
    if not _ENABLED or not isinstance(result, dict):
        return
    try:
        request = request if isinstance(request, dict) else {}
        ts = request.get("today")  # frozen request date — request-shape only, NO user identity
        events: list[dict] = []

        # (1) Unsatisfiable activities — the day-planner's honest "not in verified catalog" note.
        for dp in result.get("day_plans") or []:
            if not isinstance(dp, dict):
                continue
            city = (dp.get("city") or "").strip().lower()
            country = (dp.get("country") or "").strip().lower()
            for note in dp.get("notes") or []:
                m = _ACTIVITY_NOTE_RE.search(note) if isinstance(note, str) else None
                if not m:
                    continue
                for tok in m.group(2).split(","):
                    tok = tok.strip().lower()
                    if tok:
                        events.append({"kind": "activity", "value": tok,
                                       "context_city": city, "context_country": country, "ts": ts})

        # (2) cannot_satisfy — a destination the traveller wanted but we could not book.
        if result.get("outcome") == "cannot_satisfy":
            for leg in request.get("legs") or []:
                if not isinstance(leg, dict):
                    continue
                c = (leg.get("city") or "").strip().lower()
                if c:
                    events.append({"kind": "city", "value": c,
                                   "context_country": (leg.get("dest_country") or "").strip().lower(),
                                   "ts": ts})

        for e in events:
            _append(e)
    except Exception:
        pass  # observe-only firewall — never break the response
