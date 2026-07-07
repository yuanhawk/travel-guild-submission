// Drives the live-stream plan: POST /negotiate_text {stream:true} → open /stream/{id} →
// forward each frame to onEvent → resolve with the final envelope. GRACEFUL DEGRADE: any
// failure (503 server_busy / missing stream_id / stream error / timeout / POST throw) falls
// back to ONE blocking negotiateText() — the deterministic plan is stable (var-0), so a second
// run yields the same plan. Never resolves twice; close() makes a late frame a no-op.
import { negotiateText } from './api';
import type { NegotiateBody, NegotiateResult } from './api';
import { openStream } from './stream';
import type { StreamEvent, StreamHandle } from './stream';

export interface StreamRun {
  done: Promise<NegotiateResult>;   // resolves on negotiate_finished OR the degrade fallback
  close: () => void;                // teardown (new trip / unmount): stop stream, ignore late frames
}

export function planStreaming(body: NegotiateBody, onEvent: (e: StreamEvent) => void): StreamRun {
  let handle: StreamHandle | null = null;
  let settled = false;
  let closed = false;
  const ac = new AbortController();   // #1 cancels the in-flight fetch(es) on close()

  const done = new Promise<NegotiateResult>((resolve, reject) => {
    const finishWith = (r: NegotiateResult): void => {
      if (settled || closed) return;
      settled = true;
      handle?.close();
      resolve(r);
    };
    const degrade = (): void => {
      if (settled || closed) return;
      handle?.close();   // clean up the stream handle if open
      // ONE blocking re-run (no stream flag). postJson tolerates 402/403 bodies.
      negotiateText({ ...body, stream: false }, ac.signal)
        .then((r) => { if (!settled && !closed) { settled = true; resolve(r); } })
        .catch((e) => { if (!settled && !closed) { settled = true; reject(e); } });
    };

    negotiateText({ ...body, stream: true }, ac.signal)
      .then((seed) => {
        if (closed) return;
        const sid = seed?.stream_id;
        if (!sid) { degrade(); return; }                 // 503 server_busy / contract miss
        handle = openStream(
          sid,
          (e) => {
            if (settled || closed) return;
            onEvent(e);
            if (e.type === 'negotiate_finished') {
              const r = e.result as NegotiateResult | undefined;
              if (r && typeof r.outcome === 'string') finishWith(r); else degrade();
            } else if (e.type === 'timeout') {
              degrade();                                  // 120s stall → blocking fallback
            }
          },
          () => { degrade(); },                           // EventSource error before final → degrade
        );
      })
      .catch(() => { degrade(); });                       // POST {stream:true} threw → degrade
  });

  // close() aborts the in-flight fetch(es) AND tears down the SSE stream. Order matters:
  // set closed=true BEFORE ac.abort() so the abort-rejection lands in the existing
  // settled/closed guards and never spuriously triggers a degrade or rejects done.
  return { done, close: () => { closed = true; ac.abort(); handle?.close(); } };
}
