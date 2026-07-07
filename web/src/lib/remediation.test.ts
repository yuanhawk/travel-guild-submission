// Audit-remediation coverage: the missing vitest cases a catch-up review
// enumerated for slices 2/3/4 (uiState branches, confirm/prefs replay, ICS byte-fold).
import { describe, it, expect, vi, afterEach } from 'vitest';
import { uiState, confirmPlan, putPreferences } from './api';
import { buildIcs } from './ics';
import type { NegotiateResult, Preferences } from './api';

// ── uiState: branches that had no test ──────────────────────────────────────
describe('uiState — previously-untested branches', () => {
  const S = (r: Partial<NegotiateResult> | null) => uiState(r as NegotiateResult | null);
  it("null → 'empty'", () => expect(S(null)).toBe('empty'));
  it("needs_clarification → 'needs_clarification'", () =>
    expect(S({ needs_clarification: true })).toBe('needs_clarification'));
  it("budget shortfall / min-feasible → 'budget_shortfall'", () => {
    expect(S({ outcome: 'cannot_satisfy', budget_shortfall_cents: 50000 })).toBe('budget_shortfall');
    expect(S({ outcome: 'cannot_satisfy', min_feasible_total_cents: 200000 })).toBe('budget_shortfall');
  });
  it("invalid_request → 'invalid'", () =>
    expect(S({ outcome: 'invalid_request', reason: 'invalid_request' })).toBe('invalid'));
  it("server_error → 'server_error' (NOT declined)", () =>
    expect(S({ outcome: 'cannot_satisfy', reason: 'server_error' })).toBe('server_error'));
  it("cannot_satisfy w/ no special reason → 'declined' (do-not-recommend zone)", () =>
    expect(S({ outcome: 'cannot_satisfy' })).toBe('declined'));
  it("unrecognised outcome → 'empty'", () =>
    expect(S({ outcome: 'something_else' })).toBe('empty'));
});

function mockFetch(status: number, body: unknown) {
  return vi.fn(async (_url: string, _init?: RequestInit) => ({
    ok: status >= 200 && status < 300, status,
    json: async () => body, text: async () => JSON.stringify(body),
  })) as unknown as typeof fetch;
}

// ── confirmPlan replays user_id (guest omits it) ────────────────────────────
describe('confirmPlan — user_id replay on /confirm', () => {
  afterEach(() => vi.restoreAllMocks());
  it('includes user_id in the body for an active traveller', async () => {
    const f = mockFetch(200, { outcome: 'success', booking_ref: 'BK-1' }); vi.stubGlobal('fetch', f);
    await confirmPlan('trip-abc', 'demo-mei');
    const body = JSON.parse(String((f as unknown as { mock: { calls: [unknown, RequestInit][] } }).mock.calls[0][1].body));
    expect(body).toMatchObject({ idempotency_key: 'trip-abc', user_id: 'demo-mei' });
  });
  it('omits user_id for a guest (undefined dropped by JSON)', async () => {
    const f = mockFetch(200, { outcome: 'success' }); vi.stubGlobal('fetch', f);
    await confirmPlan('trip-xyz');
    const body = JSON.parse(String((f as unknown as { mock: { calls: [unknown, RequestInit][] } }).mock.calls[0][1].body));
    expect(body.idempotency_key).toBe('trip-xyz');
    expect(body.user_id).toBeUndefined();
  });
});

// ── putPreferences PUTs the profile incl. nested prefs ──────────────────────
describe('putPreferences — PUT with nested prefs', () => {
  afterEach(() => vi.restoreAllMocks());
  it('sends method PUT to /preferences carrying nested prefs.{dietary,pace}', async () => {
    const f = mockFetch(200, { user_id: 'demo-mei', prefs: {} }); vi.stubGlobal('fetch', f);
    await putPreferences({ user_id: 'demo-mei', home_currency: 'EUR',
      prefs: { dietary: 'vegetarian', pace: 'relaxed' } } as Partial<Preferences> & { user_id: string });
    const [url, init] = (f as unknown as { mock: { calls: [unknown, RequestInit][] } }).mock.calls[0];
    expect(String(url)).toMatch(/\/preferences$/);
    expect(init.method).toBe('PUT');
    expect(JSON.parse(String(init.body))).toMatchObject({
      user_id: 'demo-mei', home_currency: 'EUR', prefs: { dietary: 'vegetarian', pace: 'relaxed' } });
  });
});

// ── ICS byte-aware folding (RFC 5545 §3.1) — regression-locks the multibyte fix ──
function maxOctetsPerLine(ics: string): number {
  return Math.max(...ics.split('\r\n').map((l) => new TextEncoder().encode(l).length));
}
describe('buildIcs — byte-aware folding for multibyte names', () => {
  const longName = '東京タワー'.repeat(20) + '🗻 café';   // 3-byte CJK + 4-byte emoji + 2-byte é
  const r: NegotiateResult = {
    outcome: 'success', booking_ref: 'BK-1',
    legs: [{ leg_id: 'l0', city: longName, country: 'Japan', checkin: '2026-10-01', checkout: '2026-10-03' }],
    day_plans: [{ leg_id: 'l0', city: longName, country: 'Japan', checkin: '2026-10-01', checkout: '2026-10-03',
      num_days: 2, days: [{ day_index: 0, meals: {},
        attractions: [{ name: longName, category: 'tourism=viewpoint' }] }] }],
  } as NegotiateResult;
  const ics = buildIcs(r);
  it('no physical line exceeds 75 octets', () => {
    expect(maxOctetsPerLine(ics)).toBeLessThanOrEqual(75);
  });
  it('never splits a multibyte codepoint (emoji intact, no replacement char)', () => {
    const unfolded = ics.split('\r\n').map((l, i) => (i === 0 ? l : l.replace(/^ /, ''))).join('');
    expect(unfolded).not.toContain('�');
    expect(unfolded).toContain('🗻');
  });
  it('remains deterministic (byte-identical rebuild)', () => {
    expect(buildIcs(r)).toBe(ics);
  });
});
