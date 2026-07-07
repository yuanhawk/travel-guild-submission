import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { openStream } from './stream';

// EventSource is a browser global — mock a minimal one so the SSE wiring is
// testable under node. `last` captures the most recent instance to drive frames.
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

describe('openStream', () => {
  it('parses wallet / risk / emergency frames into typed events', () => {
    const got: any[] = [];
    openStream('trip-1', (e) => got.push(e));
    last.emit(JSON.stringify({ type: 'wallet', data: { op: 'gate_blocked', amount_cents: 66000 } }));
    last.emit(JSON.stringify({ type: 'risk', data: { alert_tier: 'HIGH' } }));
    last.emit(JSON.stringify({ type: 'emergency', data: { status: 'monitoring' } }));
    expect(got).toHaveLength(3);
    expect(got[0].type).toBe('wallet');
    expect(got[0].data.op).toBe('gate_blocked');
    expect(got[2].data.status).toBe('monitoring');
  });

  it('tolerates a malformed frame: no throw, onEvent not called', () => {
    const got: any[] = [];
    expect(() => {
      openStream('trip-1', (e) => got.push(e));
      last.emit('{ not valid json');
    }).not.toThrow();
    expect(got).toHaveLength(0);
  });

  it('passes through unknown/future event types (forward-compatible)', () => {
    const got: any[] = [];
    openStream('trip-1', (e) => got.push(e));
    last.emit(JSON.stringify({ type: 'some_future_event', data: { x: 1 } }));
    expect(got[0].type).toBe('some_future_event');
  });

  it('close() closes the underlying EventSource', () => {
    const h = openStream('trip-1', () => {});
    h.close();
    expect(last.closed).toBe(true);
  });
});
