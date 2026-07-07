"""
#5 — regression: /negotiate caps live stream queues so a client that POSTs but
never opens /stream cannot leak queues to OOM (which, on the single-orch-lock
service, would wedge everything).

L5 — regression: the orphan-queue TTL sweep must not evict a stream that is
STILL genuinely pending/active. queue_ts used to be stamped ONCE at POST time
and never refreshed, so the sweep measured "time since POST" rather than
"time since last genuine activity": a request queued behind several other
executor tasks (never getting a chance to even START its negotiation), or a
client that opens/reopens its EventSource late, could have its registration
evicted by a LATER request's sweep even though it is about to run / already
streaming. Fixed by refreshing queue_ts (a) the instant the worker actually
STARTS (before it even takes orch_lock) and (b) whenever GET /stream attaches.
"""
import asyncio
import json
import os
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from starlette.testclient import TestClient
from orchestration import server


def test_stream_cap_returns_503_when_full():
    app = server.build_app()
    orig = server.MAX_PENDING_STREAMS
    server.MAX_PENDING_STREAMS = 2  # tiny cap for the test
    try:
        with TestClient(app) as client:
            trip = {
                "user_id": "cap-test", "total_budget_cents": 150000,
                "legs": [{"city": "bali", "checkin": "2026-10-01",
                          "checkout": "2026-10-04", "adults": 1, "vibe": "beach"}],
            }
            # Two POSTs (never streamed) fill the cap.
            r1 = client.post("/negotiate", json=trip)
            r2 = client.post("/negotiate", json=trip)
            assert r1.status_code == 200, r1.text
            assert r2.status_code == 200, r2.text
            # The third must be rejected (busy), NOT leak another queue.
            r3 = client.post("/negotiate", json=trip)
            assert r3.status_code == 503, (r3.status_code, r3.text)
            assert r3.json().get("error") == "server_busy", r3.json()
            print("[#5] PASS — /stream queue cap rejects overflow with 503")
    finally:
        server.MAX_PENDING_STREAMS = orig


def test_worker_start_refreshes_queue_ts_before_it_can_be_evicted():
    """L5 core regression: a stream whose worker is still QUEUED (all executor
    workers busy) must not have its queue_ts silently look "fresh" until the
    worker actually starts — and once it DOES start, queue_ts must be
    refreshed (overwriting any stale value) BEFORE the worker even attempts
    orch_lock, proving the sweep can no longer evict a stream that just began
    running."""
    app = server.build_app()
    with TestClient(app) as client:
        # Saturate every negotiate-executor worker with blocking tasks so the
        # NEXT /negotiate's worker is guaranteed to be queued, not running.
        release = threading.Event()
        started = threading.Barrier(4 + 1, timeout=5)

        def _blocking_task():
            try:
                started.wait(timeout=5)
            except threading.BrokenBarrierError:
                pass
            release.wait(timeout=10)

        futures = [server._state.executor.submit(_blocking_task) for _ in range(4)]
        started.wait(timeout=5)  # all 4 workers now occupied

        try:
            trip = {
                "user_id": "l5-worker-start-test", "total_budget_cents": 150000,
                "legs": [{"city": "bali", "checkin": "2026-10-01",
                          "checkout": "2026-10-04", "adults": 1, "vibe": "beach"}],
            }
            resp = client.post("/negotiate", json=trip)
            assert resp.status_code == 200, resp.text
            stream_id = resp.json()["stream_id"]

            # The worker CANNOT have started yet (all 4 workers are still
            # blocked) — backdate queue_ts to simulate a stale registration
            # from long before this POST, proving nothing has touched it.
            stale_ts = time.time() - server.STREAM_QUEUE_TTL_S - 500
            server._state.queue_ts[stream_id] = stale_ts
            assert server._state.queue_ts[stream_id] == stale_ts, (
                "queue_ts changed before the worker could possibly have started "
                "— test setup invalid (workers not actually saturated)"
            )

            # Release the blocking tasks so this negotiate's worker can run.
            release.set()
            for f in futures:
                f.result(timeout=5)

            # Poll briefly for the worker to start and refresh queue_ts.
            deadline = time.time() + 5
            refreshed = None
            while time.time() < deadline:
                ts = server._state.queue_ts.get(stream_id)
                if ts is not None and ts != stale_ts:
                    refreshed = ts
                    break
                time.sleep(0.02)

            assert refreshed is not None, (
                "L5 REGRESSION: queue_ts was never refreshed once the worker "
                "started — a sweep could evict a stream that just began running."
            )
            assert refreshed > stale_ts + server.STREAM_QUEUE_TTL_S, (
                f"queue_ts refreshed to {refreshed} but that's not meaningfully "
                f"newer than the backdated {stale_ts}"
            )
        finally:
            release.set()
            for f in futures:
                if not f.done():
                    f.result(timeout=5)
            # Drain the stream so the negotiation completes and cleans up.
            try:
                with client.stream("GET", f"/stream/{stream_id}") as r:
                    for _ in r.iter_lines():
                        pass
            except Exception:
                pass


def test_stream_attach_refreshes_stale_queue_ts():
    """L5: GET /stream attach must refresh queue_ts immediately (synchronously,
    before the SSE generator even starts), so a client that opens its
    EventSource late — or reconnects after a dropped connection — is not
    evicted by a subsequent orphan sweep merely because time-since-POST now
    exceeds STREAM_QUEUE_TTL_S. Calls the route coroutine directly (the
    refresh happens before any streaming I/O, so no live SSE connection is
    needed to observe it)."""
    app = server.build_app()
    with TestClient(app):  # drives _lifespan so _state is initialized
        stream_id = "test-l5-attach-refresh-0001"
        server._state.queues[stream_id] = asyncio.Queue()
        stale_ts = time.time() - server.STREAM_QUEUE_TTL_S - 500
        server._state.queue_ts[stream_id] = stale_ts

        class _FakeRequest:
            path_params = {"stream_id": stream_id}

        asyncio.run(server.stream(_FakeRequest()))

        refreshed = server._state.queue_ts.get(stream_id)
        assert refreshed is not None
        assert refreshed > stale_ts + server.STREAM_QUEUE_TTL_S, (
            "L5 REGRESSION: GET /stream attach did not refresh queue_ts — a "
            "late-opening or reconnecting client would still be evicted by "
            "the orphan sweep despite actively attaching."
        )
        # Cleanup (this test registered the queue manually, outside a real POST).
        server._state.queues.pop(stream_id, None)
        server._state.queue_ts.pop(stream_id, None)
        server._state.stream_attached.discard(stream_id)


def test_l1_ttl_at_least_keepalive_tolerance():
    """L1 regression: STREAM_QUEUE_TTL_S must never be SHORTER than the
    keep-alive tolerance the SSE generator itself honors
    (STREAM_TIMEOUT_S * STREAM_MAX_SILENT_WAITS). queue_ts is stamped once at
    accept/attach/worker-start and is NEVER refreshed on each individual
    keep-alive frame, so the registry clock runs continuously for up to that
    many CONSECUTIVE silent waits before the stream itself would declare a
    genuine terminal timeout. A shorter TTL here lets the orphan sweep
    (triggered by any OTHER concurrent POST) evict an actively-kept-alive,
    still-connected stream's registry entries well before the stream's own
    patience budget is exhausted."""
    keepalive_tolerance = server.STREAM_TIMEOUT_S * server.STREAM_MAX_SILENT_WAITS
    assert server.STREAM_QUEUE_TTL_S >= keepalive_tolerance, (
        f"L1 REGRESSION: STREAM_QUEUE_TTL_S={server.STREAM_QUEUE_TTL_S} is "
        f"shorter than the keep-alive tolerance {keepalive_tolerance} "
        f"(STREAM_TIMEOUT_S={server.STREAM_TIMEOUT_S} * "
        f"STREAM_MAX_SILENT_WAITS={server.STREAM_MAX_SILENT_WAITS}) — the "
        f"orphan sweep can evict an actively-kept-alive, still-connected "
        f"stream before it would ever genuinely time out."
    )


def test_l2_refresh_helper_does_not_resurrect_an_evicted_entry():
    """L2 regression: `_refresh_queue_ts_if_registered` (the L5 worker-start
    refresh, now conditional) must NOT insert a queue_ts entry for a
    stream_id that isn't (or is no longer) registered — e.g. a worker that
    starts AFTER its stream was already evicted/closed by the orphan sweep, a
    terminal timeout, or client-disconnect cleanup (all of which pop `queues`
    and `queue_ts` together). Resurrecting a bare queue_ts entry with no
    corresponding `queues` entry is a lockstep-invariant violation the prior
    unconditional __setitem__ could introduce."""
    stream_id = "test-l2-evicted-worker-start-0001"
    assert stream_id not in server._state.queue_ts
    assert stream_id not in server._state.queues

    server._refresh_queue_ts_if_registered(stream_id)

    assert stream_id not in server._state.queue_ts, (
        "L2 REGRESSION: the worker-start refresh resurrected a queue_ts entry "
        "for a stream_id with no corresponding queues entry."
    )

    # Happy path: a GENUINELY still-registered stream_id IS refreshed.
    server._state.queues[stream_id] = asyncio.Queue()
    server._state.queue_ts[stream_id] = 0.0
    server._refresh_queue_ts_if_registered(stream_id)
    assert server._state.queue_ts[stream_id] > 0.0, (
        "the conditional refresh must still refresh a genuinely-registered stream"
    )
    server._state.queues.pop(stream_id, None)
    server._state.queue_ts.pop(stream_id, None)


def test_l3_second_concurrent_attach_gets_already_attached_not_shared_queue():
    """L3 regression: two concurrent GET /stream/{id} attaches to the SAME
    stream_id must NOT silently round-robin-share the same asyncio.Queue — the
    first attacher claims it; a second concurrent attacher gets an explicit
    `already_attached` terminal frame instead, and the first attacher's own
    stream is completely unaffected (still sees its own events end-to-end,
    including the sentinel, and still cleans up the registry on completion)."""
    app = server.build_app()
    with TestClient(app):  # drives _lifespan so _state is initialized
        stream_id = "test-l3-double-attach-0001"
        q = asyncio.Queue()
        server._state.queues[stream_id] = q

        class _FakeRequest:
            path_params = {"stream_id": stream_id}

        async def _run():
            resp1 = await server.stream(_FakeRequest())
            assert stream_id in server._state.stream_attached, (
                "first attach must claim the stream"
            )

            # Second concurrent attach to the SAME stream_id.
            resp2 = await server.stream(_FakeRequest())
            frames2 = []
            async for chunk in resp2.body_iterator:
                frames2.append(json.loads(chunk["data"]))
            assert len(frames2) == 1, frames2
            assert frames2[0]["type"] == "already_attached", frames2
            assert frames2[0]["stream_id"] == stream_id

            # The FIRST attacher's own stream is unaffected: feed it events
            # and confirm it sees them (not interleaved/stolen by resp2, which
            # never touched the real queue).
            await q.put({"type": "phase", "summary": "hi"})
            await q.put({"type": server.SENTINEL_TYPE, "outcome": {"outcome": "success"}})
            frames1 = []
            async for chunk in resp1.body_iterator:
                frames1.append(json.loads(chunk["data"]))
            assert [f["type"] for f in frames1] == ["phase", server.SENTINEL_TYPE], frames1

        asyncio.run(_run())

        # First attacher's `finally` block must have cleaned up the registry
        # (queues, queue_ts, AND the L3 claim set) once its stream completed.
        assert stream_id not in server._state.queues
        assert stream_id not in server._state.queue_ts
        assert stream_id not in server._state.stream_attached


def test_l8_investigated_queued_attach_already_closes_the_gap():
    """L8 investigation (PLAUSIBLE finding, verified here): a request queued
    behind executor saturation for longer than STREAM_QUEUE_TTL_S before its
    worker even starts is NOT an additional false-eviction gap beyond L1/L2 —
    GET /stream's attach-time refresh is UNCONDITIONAL (runs regardless of
    worker state) and every real client attaches essentially immediately
    after POST (that IS the streaming contract: POST returns stream_id, the
    client opens EventSource right away). This test proves the scenario the
    finding describes — a queue created long ago (queue_ts far past the TTL)
    whose worker has NOT yet started — survives an immediately-following
    sweep once the client attaches, with NO code change needed."""
    app = server.build_app()
    with TestClient(app):  # drives _lifespan so _state is initialized
        stream_id = "test-l8-queued-attach-refresh-0001"
        server._state.queues[stream_id] = asyncio.Queue()
        # Simulate: queue minted long ago, worker STILL hasn't started (no
        # worker-start refresh has fired yet) — well past the TTL.
        server._state.queue_ts[stream_id] = time.time() - server.STREAM_QUEUE_TTL_S - 10

        class _FakeRequest:
            path_params = {"stream_id": stream_id}

        # The client attaches now (the real-world "immediately after POST" case).
        asyncio.run(server.stream(_FakeRequest()))

        # A LATER concurrent request's sweep runs right after attach.
        now = time.time()
        stale = [sid for sid, ts in server._state.queue_ts.items()
                 if now - ts > server.STREAM_QUEUE_TTL_S]
        assert stream_id not in stale, (
            "L8: the attach-time refresh should have protected this entry "
            "from an immediately-following sweep even though its worker "
            "never started — if this fails, L8 is a REAL residual gap."
        )
        server._state.queues.pop(stream_id, None)
        server._state.queue_ts.pop(stream_id, None)
        server._state.stream_attached.discard(stream_id)


if __name__ == "__main__":
    test_stream_cap_returns_503_when_full()
    test_worker_start_refreshes_queue_ts_before_it_can_be_evicted()
    test_stream_attach_refreshes_stale_queue_ts()
    test_l1_ttl_at_least_keepalive_tolerance()
    test_l2_refresh_helper_does_not_resurrect_an_evicted_entry()
    test_l3_second_concurrent_attach_gets_already_attached_not_shared_queue()
    test_l8_investigated_queued_attach_already_closes_the_gap()
    print("ALL #5 BACKPRESSURE TESTS PASSED")
