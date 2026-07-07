import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { planStreaming } from './planStream';
import type { StreamEvent } from './stream';

// Flush pending microtasks (fetch promise chains) so EventSource gets created.
const flush = () => new Promise<void>((r) => setTimeout(r, 0));

// ─── shared EventSource mock (mirrors stream.test.ts pattern) ─────────────
let last: any = null;
let fetchCallCount = 0;
let fetchBodies: unknown[] = [];

function mockFetch(responses: Array<{ ok?: boolean; status?: number; body: unknown }>) {
  let callIdx = 0;
  return vi.fn(async (_url: string, init?: RequestInit) => {
    if (init?.body) fetchBodies.push(JSON.parse(init.body as string));
    const resp = responses[Math.min(callIdx++, responses.length - 1)];
    const ok = resp.ok ?? true;
    const status = resp.status ?? (ok ? 200 : 500);
    return {
      ok,
      status,
      json: async () => resp.body,
      text: async () => JSON.stringify(resp.body),
    };
  });
}

beforeEach(() => {
  last = null;
  fetchCallCount = 0;
  fetchBodies = [];
  vi.stubGlobal(
    'EventSource',
    class {
      url: string;
      onmessage: ((e: { data: string }) => void) | null = null;
      onerror: ((e: unknown) => void) | null = null;
      closed = false;
      constructor(url: string) {
        this.url = url;
        last = this;
      }
      close() { this.closed = true; }
      emit(data: string) {
        this.onmessage && this.onmessage({ data });
      }
    },
  );
});
afterEach(() => vi.unstubAllGlobals());

const baseBody = { text: 'Tokyo 3 nights', plan: true, wallet_balance_cents: 500000 };

// ─── happy path ────────────────────────────────────────────────────────────
describe('planStreaming — happy path', () => {
  it('emits events + resolves done with negotiate_finished.result; closes ES', async () => {
    const f = mockFetch([{ body: { stream_id: 's1' } }]);
    vi.stubGlobal('fetch', f);

    const events: StreamEvent[] = [];
    const run = planStreaming(baseBody, (e) => events.push(e));
    await flush();   // let the fetch promise resolve so EventSource is constructed

    // drive stream
    last.emit(JSON.stringify({ type: 'agent_started', agent: 'Risk', ts_ms: 1 }));
    last.emit(JSON.stringify({ type: 'negotiate_finished', result: { outcome: 'plan_ready', idempotency_key: 'k1' } }));

    const result = await run.done;
    expect(result).toEqual({ outcome: 'plan_ready', idempotency_key: 'k1' });
    expect(events).toHaveLength(2);
    expect(events[0].type).toBe('agent_started');
    expect(last.closed).toBe(true);

    // only one fetch call
    expect(f.mock.calls).toHaveLength(1);
    // first call had stream:true
    expect(fetchBodies[0]).toMatchObject({ stream: true });
  });
});

// ─── degrade: missing stream_id (503 server_busy) ─────────────────────────
describe('planStreaming — degrade: no stream_id', () => {
  it('falls back to blocking POST when first response has no stream_id', async () => {
    const f = mockFetch([
      { body: { error: 'server_busy' } },          // no stream_id
      { body: { outcome: 'plan_ready' } },           // fallback
    ]);
    vi.stubGlobal('fetch', f);

    const run = planStreaming(baseBody, () => {});
    const result = await run.done;

    expect(result).toEqual({ outcome: 'plan_ready' });
    expect(f.mock.calls).toHaveLength(2);
    // second call must have stream:false (or absent stream field)
    const body2 = fetchBodies[1] as Record<string, unknown>;
    expect(body2.stream).toBe(false);
    expect(last).toBeNull();   // no EventSource opened
  });
});

// ─── degrade: stream onerror ───────────────────────────────────────────────
describe('planStreaming — degrade: stream error before final', () => {
  it('triggers fallback blocking fetch on EventSource onerror', async () => {
    const f = mockFetch([
      { body: { stream_id: 's1' } },
      { body: { outcome: 'plan_ready', idempotency_key: 'k2' } },
    ]);
    vi.stubGlobal('fetch', f);

    const run = planStreaming(baseBody, () => {});
    await flush();   // let fetch resolve so EventSource is created
    // simulate EventSource error
    last.onerror(new Event('error'));

    const result = await run.done;
    expect(result).toEqual({ outcome: 'plan_ready', idempotency_key: 'k2' });
    expect(f.mock.calls).toHaveLength(2);
    expect(last.closed).toBe(true);
  });
});

// ─── degrade: timeout frame ────────────────────────────────────────────────
describe('planStreaming — degrade: timeout frame', () => {
  it('falls back when {type:"timeout"} received before finished', async () => {
    const f = mockFetch([
      { body: { stream_id: 's1' } },
      { body: { outcome: 'plan_ready' } },
    ]);
    vi.stubGlobal('fetch', f);

    const run = planStreaming(baseBody, () => {});
    await flush();   // let fetch resolve so EventSource is created
    last.emit(JSON.stringify({ type: 'timeout' }));

    const result = await run.done;
    expect(result).toMatchObject({ outcome: 'plan_ready' });
    expect(f.mock.calls).toHaveLength(2);
  });
});

// ─── degrade: POST stream:true throws ─────────────────────────────────────
describe('planStreaming — degrade: POST throws', () => {
  it('falls back when first fetch rejects', async () => {
    let callIdx = 0;
    const f = vi.fn(async () => {
      if (callIdx++ === 0) throw new Error('network fail');
      return { ok: true, status: 200, json: async () => ({ outcome: 'plan_ready' }), text: async () => '' };
    });
    vi.stubGlobal('fetch', f);

    const run = planStreaming(baseBody, () => {});
    const result = await run.done;
    expect(result).toMatchObject({ outcome: 'plan_ready' });
    expect(f.mock.calls).toHaveLength(2);
    expect(last).toBeNull();
  });
});

// ─── no double-resolve ─────────────────────────────────────────────────────
describe('planStreaming — no double-resolve', () => {
  it('resolves exactly once; late timeout after finished is ignored', async () => {
    const f = mockFetch([{ body: { stream_id: 's1' } }]);
    vi.stubGlobal('fetch', f);

    const resolved: unknown[] = [];
    const run = planStreaming(baseBody, () => {});
    run.done.then((r) => resolved.push(r));
    await flush();   // let fetch resolve so EventSource is created

    // resolve via negotiate_finished
    last.emit(JSON.stringify({ type: 'negotiate_finished', result: { outcome: 'plan_ready' } }));
    await run.done;

    // emit a late timeout — should be ignored
    last.emit(JSON.stringify({ type: 'timeout' }));
    await Promise.resolve();

    // only 1 fetch call (no fallback triggered)
    expect(f.mock.calls).toHaveLength(1);
    expect(resolved).toHaveLength(1);
  });
});

// ─── close() before final ──────────────────────────────────────────────────
describe('planStreaming — close() before final', () => {
  it('closes ES and promise never resolves after close', async () => {
    const f = mockFetch([{ body: { stream_id: 's1' } }]);
    vi.stubGlobal('fetch', f);

    const run = planStreaming(baseBody, () => {});
    run.close();

    // emit late negotiate_finished
    if (last) last.emit(JSON.stringify({ type: 'negotiate_finished', result: { outcome: 'plan_ready' } }));

    // race against a quick sentinel — done should NOT have resolved
    const winner = await Promise.race([
      run.done.then(() => 'resolved'),
      Promise.resolve('pending'),
    ]);
    expect(winner).toBe('pending');
    if (last) expect(last.closed).toBe(true);
  });
});

// ─── #1 AbortController: close() cancels the in-flight fetch ───────────────
describe('planStreaming — abort: close() aborts the in-flight POST', () => {
  it('threads an AbortSignal into fetch and aborts it on close()', async () => {
    const signals: (AbortSignal | undefined)[] = [];
    const f = vi.fn(async (_url: string, init?: RequestInit) => {
      signals.push(init?.signal ?? undefined);
      return { ok: true, status: 200, json: async () => ({ stream_id: 's1' }), text: async () => '' };
    });
    vi.stubGlobal('fetch', f);

    const run = planStreaming(baseBody, () => {});
    // fetch is invoked synchronously — the stream:true POST carries a live signal.
    expect(signals[0]).toBeInstanceOf(AbortSignal);
    expect(signals[0]!.aborted).toBe(false);

    run.close();
    expect(signals[0]!.aborted).toBe(true);
  });

  it('abort does not trigger a degrade / second fetch and done stays pending', async () => {
    const f = vi.fn(async () => ({
      ok: true, status: 200, json: async () => ({ stream_id: 's1' }), text: async () => '',
    }));
    vi.stubGlobal('fetch', f);

    const run = planStreaming(baseBody, () => {});
    run.close();
    await flush();   // let any rejected fetch promise settle

    expect(f.mock.calls).toHaveLength(1);   // no fallback fetch fired
    const winner = await Promise.race([
      run.done.then(() => 'resolved'),
      Promise.resolve('pending'),
    ]);
    expect(winner).toBe('pending');
  });
});

// ─── degrade: final frame missing payload ─────────────────────────────────
describe('planStreaming — degrade: negotiate_finished no result', () => {
  it('degrades to fallback when negotiate_finished has no result field', async () => {
    const f = mockFetch([
      { body: { stream_id: 's1' } },
      { body: { outcome: 'plan_ready' } },
    ]);
    vi.stubGlobal('fetch', f);

    const run = planStreaming(baseBody, () => {});
    await flush();   // let fetch resolve so EventSource is created
    last.emit(JSON.stringify({ type: 'negotiate_finished' }));   // no result

    const result = await run.done;
    expect(result).toMatchObject({ outcome: 'plan_ready' });
    expect(f.mock.calls).toHaveLength(2);
  });
});
