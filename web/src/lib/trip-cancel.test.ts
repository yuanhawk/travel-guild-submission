import { describe, it, expect, vi, afterEach } from 'vitest';
import { getTrip, cancelTrip } from './api';
import type { NegotiateResult, CancelResult } from './api';

// ─── getTrip — GET /trips/{key} contract guard ────────────────────────────
// Guards: correct URL encoding, 200→envelope, 404→null (stale/gone).

function mockGet(status: number, body?: unknown) {
  return vi.fn(async (_url: string) => ({
    ok: status >= 200 && status < 300,
    status,
    json: async () => body,
    text: async () => JSON.stringify(body),
  })) as unknown as typeof fetch;
}

type MockedFetch = { mock: { calls: [string, RequestInit | undefined][] } };

describe('getTrip — GET /trips/{key}', () => {
  afterEach(() => vi.restoreAllMocks());

  it('sends a GET request to /trips/<encoded-key>', async () => {
    const envelope: NegotiateResult = {
      outcome: 'plan_ready', idempotency_key: 'trip-abc-123',
      legs: [], day_plans: [], payment_status: 'held',
    };
    const f = mockGet(200, envelope);
    vi.stubGlobal('fetch', f);
    await getTrip('trip-abc-123');
    const [url, init] = (f as unknown as MockedFetch).mock.calls[0];
    // URL must include /trips/ + the key. SECURITY (secrets-in-URL fix): ownership
    // proof now rides as request headers, not query params, so the path is bare.
    expect(url).toMatch(/\/trips\/trip-abc-123$/);
    // GET: no method specified (defaults to GET) OR explicitly GET; no body
    expect(!init || init.method === undefined || init.method === 'GET').toBe(true);
    expect(!init || init.body === undefined).toBe(true);
  });

  it('sends owner_token (and session_token when logged in) as X-Owner-Token/X-Session-Token headers (#155, secrets-in-URL fix)', async () => {
    const f = mockGet(200, { outcome: 'plan_ready', idempotency_key: 'trip-auth-1' });
    vi.stubGlobal('fetch', f);
    // Anonymous (no session): owner_token always present, session_token absent.
    await getTrip('trip-auth-1');
    const [anonUrl, anonInit] = (f as unknown as MockedFetch).mock.calls[0];
    expect(anonUrl).not.toMatch(/[?&]owner_token=/);
    const anonHeaders = new Headers(anonInit?.headers);
    expect(anonHeaders.get('x-owner-token')).toBeTruthy();
    expect(anonHeaders.has('x-session-token')).toBe(false);

    // Logged-in: session_token is threaded too — mirrors how authFields() is
    // threaded into every POST body (/confirm, /cancel, /refine, /replan).
    vi.doMock('./session', () => ({
      authFields: () => ({ session_token: 'sess-tok-xyz', owner_token: 'owner-tok-abc' }),
    }));
    vi.resetModules();
    const { getTrip: getTripWithSession } = await import('./api');
    vi.stubGlobal('fetch', f);
    await getTripWithSession('trip-auth-2');
    const [loggedInUrl, loggedInInit] = (f as unknown as MockedFetch).mock.calls[1];
    expect(loggedInUrl).not.toContain('session_token=');
    expect(loggedInUrl).not.toContain('owner_token=');
    const loggedInHeaders = new Headers(loggedInInit?.headers);
    expect(loggedInHeaders.get('x-session-token')).toBe('sess-tok-xyz');
    expect(loggedInHeaders.get('x-owner-token')).toBe('owner-tok-abc');
    vi.doUnmock('./session');
    vi.resetModules();
  });

  it('URL-encodes special characters in the key', async () => {
    const f = mockGet(200, { outcome: 'plan_ready' });
    vi.stubGlobal('fetch', f);
    await getTrip('trip key with spaces/slashes');
    const [url] = (f as unknown as MockedFetch).mock.calls[0];
    expect(url).toContain('trip%20key%20with%20spaces%2Fslashes');
    expect(url).not.toContain(' ');
  });

  it('returns the parsed NegotiateResult envelope on 200', async () => {
    const envelope: NegotiateResult = {
      outcome: 'plan_ready',
      idempotency_key: 'trip-restore-1',
      payment_status: 'held',
      package_total_with_fees_cents: 184000,
      legs: [{ leg_id: 'leg-0', city: 'Tokyo' }],
      day_plans: [{ leg_id: 'leg-0', city: 'Tokyo', num_days: 3 }],
    };
    vi.stubGlobal('fetch', mockGet(200, envelope));
    const result = await getTrip('trip-restore-1');
    expect(result).not.toBeNull();
    expect(result?.idempotency_key).toBe('trip-restore-1');
    expect(result?.outcome).toBe('plan_ready');
    expect(result?.legs).toHaveLength(1);
    expect(result?.legs?.[0].city).toBe('Tokyo');
  });

  it('returns null on 404 (stale/gone — caller clears the stale key)', async () => {
    vi.stubGlobal('fetch', mockGet(404));
    const result = await getTrip('trip-gone');
    expect(result).toBeNull();
  });

  it('returns null on any non-2xx status (e.g. 500)', async () => {
    vi.stubGlobal('fetch', mockGet(500, { error: 'Internal Server Error' }));
    const result = await getTrip('trip-error');
    expect(result).toBeNull();
  });

  it('returns null on 404 (not owner or gone — #155: denied reads are 404, not 403, to avoid an existence oracle)', async () => {
    vi.stubGlobal('fetch', mockGet(404));
    const result = await getTrip('trip-not-owner-or-gone');
    expect(result).toBeNull();
  });

  it('returns null on 401 / 403 too (belt-and-suspenders — any non-2xx maps to null)', async () => {
    vi.stubGlobal('fetch', mockGet(403));
    const result = await getTrip('trip-forbidden');
    expect(result).toBeNull();
  });
});

// ─── cancelTrip — POST /cancel contract guard ─────────────────────────────
// Guards: POST method, correct body shape, response parsing for all outcomes.

function mockPost(status: number, body: unknown) {
  return vi.fn(async () => ({
    ok: status >= 200 && status < 300,
    status,
    json: async () => body,
    text: async () => JSON.stringify(body),
  })) as unknown as typeof fetch;
}

describe('cancelTrip — POST /cancel', () => {
  afterEach(() => vi.restoreAllMocks());

  it('POSTs to /cancel with idempotency_key in the body', async () => {
    const f = mockPost(200, { outcome: 'cancelled', refunded_cents: 324000, wallet_balance_cents: 824000 });
    vi.stubGlobal('fetch', f);
    await cancelTrip('trip-booked-99');
    const [url, init] = (f as unknown as MockedFetch).mock.calls[0];
    expect(url).toMatch(/\/cancel$/);
    expect(init?.method).toBe('POST');
    const body = JSON.parse(String(init?.body));
    expect(body.idempotency_key).toBe('trip-booked-99');
  });

  it('includes user_id in the body when supplied', async () => {
    const f = mockPost(200, { outcome: 'cancelled', wallet_balance_cents: 824000 });
    vi.stubGlobal('fetch', f);
    await cancelTrip('trip-booked-99', 'demo-mei');
    const body = JSON.parse(String((f as unknown as MockedFetch).mock.calls[0][1]?.body));
    expect(body.user_id).toBe('demo-mei');
  });

  it('omits user_id when not supplied (guest cancel)', async () => {
    const f = mockPost(200, { outcome: 'cancelled', wallet_balance_cents: 824000 });
    vi.stubGlobal('fetch', f);
    await cancelTrip('trip-guest-99');
    const body = JSON.parse(String((f as unknown as MockedFetch).mock.calls[0][1]?.body));
    expect(body.user_id).toBeUndefined();
  });

  it('parses outcome=cancelled + refunded_cents + wallet_balance_cents', async () => {
    const payload: CancelResult = {
      outcome: 'cancelled',
      refunded_cents: 324000,
      wallet_balance_cents: 824000,
    };
    vi.stubGlobal('fetch', mockPost(200, payload));
    const r = await cancelTrip('trip-booked-1');
    expect(r.outcome).toBe('cancelled');
    expect(r.refunded_cents).toBe(324000);
    expect(r.wallet_balance_cents).toBe(824000);
  });

  it('parses outcome=already_cancelled (idempotent — no double-refund)', async () => {
    vi.stubGlobal('fetch', mockPost(200, { outcome: 'already_cancelled', wallet_balance_cents: 500000 }));
    const r = await cancelTrip('trip-already-cancelled');
    expect(r.outcome).toBe('already_cancelled');
    expect(r.wallet_balance_cents).toBe(500000);
    // No refunded_cents on a second cancel
    expect(r.refunded_cents).toBeUndefined();
  });

  it('parses wallet_credit_cents as the refund field (alias the server may return)', async () => {
    vi.stubGlobal('fetch', mockPost(200, {
      outcome: 'cancelled',
      wallet_credit_cents: 184000,   // some backends return this field name
      wallet_balance_cents: 684000,
    }));
    const r = await cancelTrip('trip-alias-1');
    expect(r.outcome).toBe('cancelled');
    expect(r.wallet_credit_cents).toBe(184000);
    expect(r.wallet_balance_cents).toBe(684000);
  });

  it('parses outcome=invalid_request + reason', async () => {
    vi.stubGlobal('fetch', mockPost(200, { outcome: 'invalid_request', reason: 'key not found' }));
    const r = await cancelTrip('trip-invalid');
    expect(r.outcome).toBe('invalid_request');
    expect(r.reason).toBe('key not found');
  });

  it('throws on non-2xx HTTP error (like other postJson callers)', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => ({
      ok: false, status: 500, text: async () => 'Internal Server Error',
    })));
    await expect(cancelTrip('trip-x')).rejects.toThrow();
  });
});
