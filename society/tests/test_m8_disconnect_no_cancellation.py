"""test_m8_disconnect_no_cancellation.py — M8 (PLAUSIBLE, INVESTIGATED, NOT
REPRODUCIBLE on this stack): a client disconnect during /confirm's
`await run_in_executor(mgmt_executor, _commit)` was hypothesized to be a
genuine asyncio cancellation point — "uvicorn cancels the request task on
client disconnect... the POST-await mark_booked never runs because the
awaiting coroutine was cancelled" — which would leave a genuinely-debited
merchant booking with no corresponding mark_booked write, orphaning the
wallet debit.

INVESTIGATION: empirically verified against this project's ACTUAL ASGI stack
(Starlette + uvicorn, the same versions pinned in society/requirements.txt,
the same middleware shape server.py's build_app() uses — plain ASGI
CORSMiddleware only, no BaseHTTPMiddleware anywhere in the stack, which is the
one Starlette construct that HAS historically implemented disconnect-driven
cancellation via a task-group race). A minimal Starlette app reproducing the
EXACT shape of confirm()'s hazardous window (`await request.json()` then
`await loop.run_in_executor(executor, blocking_commit)` then post-await code)
is driven by a raw socket that sends the full request and then closes the
connection — BOTH via a graceful FIN and via an ungraceful RST (SO_LINGER 0,
closer to "network drop" / "app killed mid-request" than a clean client
disconnect) — while the executor-backed "commit" is still mid-flight.

RESULT (both close styles): the executor-backed call is NEVER cancelled —
it runs to completion, and the POST-await code (the mark_booked stand-in)
ALSO always executes. No asyncio.CancelledError is ever raised into the
awaiting coroutine.

CONCLUSION: M8's premise does not hold for this codebase's actual ASGI stack.
`await loop.run_in_executor(...)` is not itself a disconnect-triggered
cancellation point under plain Starlette + uvicorn without BaseHTTPMiddleware
— the only construct that reintroduces that race. No code change was made for
M8. This test is a REGRESSION GUARD: if a future change (e.g. adopting
BaseHTTPMiddleware, or a Starlette/uvicorn upgrade that changes this
behavior) reintroduces disconnect-driven cancellation, this test will start
failing and M8 should be re-opened and fixed for real (e.g. shielding the
critical section, or moving mark_booked into the same thread as the merchant
debit so both survive or neither does).
"""
from __future__ import annotations

import asyncio
import socket
import struct
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import uvicorn
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route

_PORT_FIN = 8734
_PORT_RST = 8735


def _make_app(executor: ThreadPoolExecutor, events: dict):
    def _blocking_commit():
        events["commit_started"] = True
        time.sleep(1.2)  # stands in for the merchant round-trip in _commit()
        events["commit_finished"] = True
        return {"outcome": "success", "booking_ref": "BK-1"}

    async def confirm(request):
        await request.json()
        loop = asyncio.get_event_loop()
        # Mirrors server.py confirm()'s exact hazardous shape: an await on a
        # ThreadPoolExecutor-backed call, with real work happening AFTER it.
        try:
            result = await loop.run_in_executor(executor, _blocking_commit)
        except asyncio.CancelledError:
            events["cancelled"] = True
            raise
        events["mark_booked"] = True  # stands in for store.mark_booked(...)
        return JSONResponse(result)

    return Starlette(routes=[Route("/confirm", confirm, methods=["POST"])])


def _run_server(app, port: int) -> None:
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    uvicorn.Server(config).run()


def _drive_disconnect_probe(*, port: int, use_rst: bool) -> dict:
    executor = ThreadPoolExecutor(max_workers=2)
    events = {
        "commit_started": False, "commit_finished": False,
        "mark_booked": False, "cancelled": False,
    }
    app = _make_app(executor, events)
    t = threading.Thread(target=_run_server, args=(app, port), daemon=True)
    t.start()
    try:
        # Give uvicorn a moment to bind before connecting.
        deadline = time.time() + 5
        sock = None
        while time.time() < deadline:
            try:
                sock = socket.create_connection(("127.0.0.1", port), timeout=5)
                break
            except OSError:
                time.sleep(0.1)
        assert sock is not None, "test server never came up"

        if use_rst:
            # Ungraceful abort (RST) — closer to "network drop"/"client app
            # killed mid-request" than a clean FIN.
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))

        body = b'{"idempotency_key": "trip-m8-probe"}'
        req = (
            f"POST /confirm HTTP/1.1\r\n"
            f"Host: 127.0.0.1\r\n"
            f"Content-Type: application/json\r\n"
            f"Content-Length: {len(body)}\r\n"
            f"Connection: close\r\n\r\n"
        ).encode() + body
        sock.sendall(req)
        # Let the server start the blocking commit before we disconnect.
        deadline = time.time() + 5
        while time.time() < deadline and not events["commit_started"]:
            time.sleep(0.02)
        assert events["commit_started"], "the blocking commit never started"
        sock.close()  # simulate a client disconnect WHILE _commit() is mid-flight

        # Wait long enough for the blocking commit to finish server-side IF it
        # was never cancelled.
        deadline = time.time() + 5
        while time.time() < deadline and not events["commit_finished"]:
            time.sleep(0.02)
        # Give the post-await code a moment to run too.
        time.sleep(0.2)
    finally:
        executor.shutdown(wait=False)
    return events


def test_graceful_disconnect_does_not_cancel_the_awaited_commit():
    events = _drive_disconnect_probe(port=_PORT_FIN, use_rst=False)
    assert events["commit_finished"] is True, (
        "M8 would be REAL if the executor-backed commit stopped mid-flight "
        "on client disconnect"
    )
    assert events["cancelled"] is False
    assert events["mark_booked"] is True, (
        "M8 would be REAL if the post-await code (mark_booked) never ran "
        "after a client disconnect"
    )


def test_ungraceful_rst_disconnect_does_not_cancel_the_awaited_commit():
    events = _drive_disconnect_probe(port=_PORT_RST, use_rst=True)
    assert events["commit_finished"] is True
    assert events["cancelled"] is False
    assert events["mark_booked"] is True, (
        "M8 would be REAL if an abrupt connection reset mid-commit orphaned "
        "the post-commit mark_booked write"
    )
