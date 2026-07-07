import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { openStream } from './stream';

// Shared mock — captures the most recent EventSource instance in `last`.
let last: any = null;
beforeEach(() => {
  last = null;
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
      close() {
        this.closed = true;
      }
      emit(data: string) {
        this.onmessage && this.onmessage({ data });
      }
    },
  );
});
afterEach(() => vi.unstubAllGlobals());

// ---------------------------------------------------------------------------
// GROUP A: URL construction
// ---------------------------------------------------------------------------
describe('GROUP A: URL construction', () => {
  it('A.1 plain id "trip-1" produces a URL containing /stream/trip-1', () => {
    openStream('trip-1', () => {});
    expect(last.url).toContain('/stream/trip-1');
  });

  it('A.2 id "org/trip" is percent-encoded to /stream/org%2Ftrip', () => {
    openStream('org/trip', () => {});
    expect(last.url).toContain('/stream/org%2Ftrip');
  });

  it('A.3 id "trip 1" (space) is percent-encoded to /stream/trip%201', () => {
    openStream('trip 1', () => {});
    expect(last.url).toContain('/stream/trip%201');
  });

  it('A.4 id "a?b=c" is fully percent-encoded to /stream/a%3Fb%3Dc', () => {
    openStream('a?b=c', () => {});
    expect(last.url).toContain(`/stream/${encodeURIComponent('a?b=c')}`);
    expect(last.url).toContain('/stream/a%3Fb%3Dc');
  });
});

// ---------------------------------------------------------------------------
// GROUP B: onError callback
// ---------------------------------------------------------------------------
describe('GROUP B: onError callback', () => {
  it('B.1 fires onError callback when onerror is triggered', () => {
    const errors: unknown[] = [];
    openStream('s', () => {}, (e) => errors.push(e));
    last.onerror && last.onerror(new Event('error'));
    expect(errors).toHaveLength(1);
  });

  it('B.2 omitting onError does not throw when onerror fires', () => {
    expect(() => {
      openStream('s', () => {});
      last.onerror && last.onerror(new Event('error'));
    }).not.toThrow();
  });
});

// ---------------------------------------------------------------------------
// GROUP C: Event field preservation
// ---------------------------------------------------------------------------
describe('GROUP C: Event field preservation', () => {
  it('C.1 all fields (seq, ts_ms, agent, round, summary, data) are preserved', () => {
    const got: any[] = [];
    const frame = {
      seq: 42,
      ts_ms: 1700000000,
      type: 'agent',
      agent: 'risk_agent',
      round: 2,
      summary: 'Risk assessed',
      data: { alert_tier: 'HIGH' },
    };
    openStream('s', (e) => got.push(e));
    last.emit(JSON.stringify(frame));
    expect(got[0]).toMatchObject(frame);
  });

  it('C.2 round: null is preserved and not stripped', () => {
    const got: any[] = [];
    openStream('s', (e) => got.push(e));
    last.emit(JSON.stringify({ type: 'phase', round: null }));
    expect(got[0].round).toBeNull();
  });
});

// ---------------------------------------------------------------------------
// GROUP D: Multiple simultaneous streams
// ---------------------------------------------------------------------------
describe('GROUP D: Multiple simultaneous streams', () => {
  const instances: any[] = [];

  beforeEach(() => {
    instances.length = 0;
    vi.stubGlobal(
      'EventSource',
      class {
        url: string;
        onmessage: any = null;
        onerror: any = null;
        closed = false;
        constructor(url: string) {
          this.url = url;
          instances.push(this);
        }
        close() {
          this.closed = true;
        }
        emit(data: string) {
          this.onmessage?.({ data });
        }
      },
    );
  });

  it('D.1 event emitted on es1 only reaches got1, not got2', () => {
    const got1: any[] = [];
    const got2: any[] = [];
    openStream('stream-1', (e) => got1.push(e));
    openStream('stream-2', (e) => got2.push(e));
    const [es1] = instances;
    es1.emit(JSON.stringify({ type: 'ping', seq: 1 }));
    expect(got1).toHaveLength(1);
    expect(got2).toHaveLength(0);
  });

  it('D.2 event emitted on es2 only reaches got2, not got1', () => {
    const got1: any[] = [];
    const got2: any[] = [];
    openStream('stream-1', (e) => got1.push(e));
    openStream('stream-2', (e) => got2.push(e));
    const [, es2] = instances;
    es2.emit(JSON.stringify({ type: 'ping', seq: 2 }));
    expect(got2).toHaveLength(1);
    expect(got1).toHaveLength(0);
  });

  it('D.3 close() on first handle does not close the second stream EventSource', () => {
    const h1 = openStream('stream-1', () => {});
    openStream('stream-2', () => {});
    const [, es2] = instances;
    h1.close();
    expect(es2.closed).toBe(false);
  });
});

// ---------------------------------------------------------------------------
// GROUP E: All WalletOp values round-trip
// ---------------------------------------------------------------------------
describe('GROUP E: All WalletOp values round-trip', () => {
  const walletOps = ['seed', 'debit', 'gate_blocked', 'credit'] as const;

  for (const op of walletOps) {
    it(`WalletOp "${op}" round-trips through data.op`, () => {
      const got: any[] = [];
      openStream('s', (e) => got.push(e));
      last.emit(JSON.stringify({ type: 'wallet', data: { op, amount_cents: 100 } }));
      expect(got.at(-1).data.op).toBe(op);
    });
  }
});

// ---------------------------------------------------------------------------
// GROUP F: Sequential ordering
// ---------------------------------------------------------------------------
describe('GROUP F: Sequential ordering', () => {
  it('frames emitted in seq order 1-5 are received in the same order', () => {
    const got: any[] = [];
    openStream('s', (e) => got.push(e));
    for (let seq = 1; seq <= 5; seq++) {
      last.emit(JSON.stringify({ type: 'agent', seq }));
    }
    expect(got.map((e) => e.seq)).toEqual([1, 2, 3, 4, 5]);
  });
});
