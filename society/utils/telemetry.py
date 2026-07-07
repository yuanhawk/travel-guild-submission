"""telemetry.py — #64 optional trip telemetry → Alibaba SLS + AMap heat-map dashboard.

OFF BY DEFAULT and **var-0-firewalled**. Telemetry is a pure EMIT-ONLY side effect: a trip-summary
(trip_id, outcome, cities+coords, total) is written when telemetry is enabled. It NEVER reads back
into planning, so the deterministic trip output is byte-identical whether telemetry is on or off.
Every failure is swallowed — a telemetry/SLS error can NEVER break a booking or change a result.

Enable with env `TELEMETRY_ENABLED=1` (the demo). Sinks, best-effort, in order:
  1. Local JSONL (`society/telemetry.jsonl`) — the demo sink the AMap dashboard reads (no cloud needed).
  2. Alibaba SLS (`aliyun.log` PutLogs) — additionally, when `SLS_ENABLED=1` + creds + the SDK are present.

Creds (read from env only, never hardcoded): ALIBABA_CLOUD_ACCESS_KEY_ID / _SECRET, SLS_ENDPOINT,
SLS_PROJECT, SLS_LOGSTORE. Activate-late (user wires real SLS for the live demo).
"""

from __future__ import annotations

import json
import os
import threading
from typing import Any

_LOCK = threading.Lock()
_LOCAL_LOG = os.path.join(os.path.dirname(os.path.dirname(__file__)), "telemetry.jsonl")


def telemetry_enabled() -> bool:
    return os.environ.get("TELEMETRY_ENABLED", "") == "1"


def emit_trip(event: dict[str, Any]) -> None:
    """Fire-and-forget trip telemetry. No-op unless TELEMETRY_ENABLED=1. NEVER raises."""
    if not telemetry_enabled():
        return
    try:
        line = json.dumps(event, sort_keys=True, ensure_ascii=False)
        with _LOCK:
            with open(_LOCAL_LOG, "a", encoding="utf-8") as fh:
                fh.write(line + "\n")
        if os.environ.get("SLS_ENABLED", "") == "1":
            _emit_sls(event)
    except Exception:
        # Telemetry must never affect the trip. Swallow everything.
        pass


def _emit_sls(event: dict[str, Any]) -> None:
    try:
        from aliyun.log import LogClient, LogItem, PutLogsRequest  # type: ignore
    except Exception:
        return  # SDK not installed → silently degrade to the local JSONL sink only
    ep = os.environ.get("SLS_ENDPOINT")
    ak = os.environ.get("ALIBABA_CLOUD_ACCESS_KEY_ID")
    sk = os.environ.get("ALIBABA_CLOUD_ACCESS_KEY_SECRET")
    proj = os.environ.get("SLS_PROJECT")
    logstore = os.environ.get("SLS_LOGSTORE")
    if not all((ep, ak, sk, proj, logstore)):
        return
    client = LogClient(ep, ak, sk)
    item = LogItem(contents=[(k, json.dumps(v) if isinstance(v, (dict, list)) else str(v))
                             for k, v in event.items()])
    client.put_logs(PutLogsRequest(proj, logstore, "", "", [item]))


def build_trip_event(trip_id: str, outcome: str, legs: list[dict[str, Any]] | None,
                     total_cents: int | None) -> dict[str, Any]:
    """Shape a deterministic trip-summary event for the dashboard (cities + coords + outcome).

    Pure (given inputs) — used by the orchestrator to emit on completion. `legs` items may carry
    {city, lat, lon}; missing coords are simply omitted (the dashboard plots what it has).
    """
    points = []
    for leg in (legs or []):
        if not isinstance(leg, dict):
            continue
        city = leg.get("city")
        lat, lon = leg.get("lat"), leg.get("lon")
        if city:
            pt: dict[str, Any] = {"city": city}
            if lat is not None and lon is not None:
                pt["lat"], pt["lon"] = lat, lon
            points.append(pt)
    return {
        "trip_id": trip_id,
        "outcome": outcome,
        "points": points,
        "total_cents": int(total_cents or 0),
    }
