import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { centsToUsd, bpToPct, riskSignalList, API_BASE, negotiateText, getEmergencies } from './api';
import type { NegotiateResult, EmergenciesResponse } from './api';

// ─── GROUP A: centsToUsd additional cases ──────────────────────────────────
describe('centsToUsd additional cases', () => {
  it('formats 1 cent as $0.01', () => {
    expect(centsToUsd(1)).toBe('$0.01');
  });
  it('formats 100 cents as $1.00', () => {
    expect(centsToUsd(100)).toBe('$1.00');
  });
  it('formats 50 cents as $0.50', () => {
    expect(centsToUsd(50)).toBe('$0.50');
  });
  it('formats 1000000 cents as $10,000.00', () => {
    expect(centsToUsd(1000000)).toBe('$10,000.00');
  });
});

// ─── GROUP B: bpToPct rounding cases ──────────────────────────────────────
describe('bpToPct rounding cases', () => {
  it('bpToPct(2885) returns 28.9', () => {
    expect(bpToPct(2885)).toBe(28.9);
  });
  it('bpToPct(10000) returns 100', () => {
    expect(bpToPct(10000)).toBe(100);
  });
  it('bpToPct(1) returns 0', () => {
    expect(bpToPct(1)).toBe(0);
  });
  it('bpToPct(50) returns 0.5', () => {
    expect(bpToPct(50)).toBe(0.5);
  });
  it('bpToPct(100) returns 1', () => {
    expect(bpToPct(100)).toBe(1);
  });
});

// ─── GROUP C: riskSignalList additional shapes ─────────────────────────────
describe('riskSignalList additional shapes', () => {
  it('{ per_leg: [] } returns []', () => {
    const rs: NegotiateResult['risk_signals'] = { per_leg: [] };
    expect(riskSignalList(rs)).toEqual([]);
  });
  it('{ per_leg: [{ leg_id: "a" }, { leg_id: "b" }] } has length 2 and [1].leg_id === "b"', () => {
    const rs: NegotiateResult['risk_signals'] = {
      per_leg: [{ leg_id: 'a' }, { leg_id: 'b' }],
    };
    const result = riskSignalList(rs);
    expect(result).toHaveLength(2);
    expect(result[1].leg_id).toBe('b');
  });
  it('{ consolidator: "x", any_avoid_window: false } returns [] (no per_leg key)', () => {
    const rs: NegotiateResult['risk_signals'] = {
      consolidator: 'x',
      any_avoid_window: false,
    };
    expect(riskSignalList(rs)).toEqual([]);
  });
});

// ─── GROUP D: API_BASE invariants ─────────────────────────────────────────
describe('API_BASE invariants', () => {
  it('is a string (empty = same-origin/proxy mode is valid)', () => {
    // VITE_API_BASE may be a full https URL (deployed) OR empty for the local-dev
    // proxy / same-origin mode — both are valid, so don't require non-empty.
    expect(typeof API_BASE).toBe('string');
  });
  it('does not end with "/"', () => {
    expect(API_BASE.endsWith('/')).toBe(false);
  });
});

// ─── GROUP E: negotiateText + getEmergencies with mocked fetch ─────────────
describe('negotiateText + getEmergencies (fetch mocked)', () => {
  const mockFetch = vi.fn();
  beforeEach(() => {
    vi.stubGlobal('fetch', mockFetch);
    mockFetch.mockReset();
  });
  afterEach(() => vi.unstubAllGlobals());

  it('E.1 negotiateText 200 success: returns JSON body unchanged', async () => {
    mockFetch.mockResolvedValue({
      ok: true,
      status: 200,
      json: () => Promise.resolve({ outcome: 'success', legs: [] }),
      text: () => Promise.resolve(''),
    });
    const result = await negotiateText({ text: 'trip' });
    expect(result.outcome).toBe('success');
    const [url, opts] = mockFetch.mock.calls[0];
    expect(url).toContain('/negotiate_text');
    expect(opts.method).toBe('POST');
  });

  it('E.2 negotiateText 402 insufficient_funds: returns JSON body WITHOUT throwing', async () => {
    mockFetch.mockResolvedValue({
      ok: false,
      status: 402,
      json: () => Promise.resolve({ outcome: 'insufficient_funds' }),
      text: () => Promise.resolve(''),
    });
    const result = await negotiateText({ text: 'trip' });
    expect(result.outcome).toBe('insufficient_funds');
  });

  it('E.3 negotiateText 403 budget_veto: returns JSON body WITHOUT throwing', async () => {
    mockFetch.mockResolvedValue({
      ok: false,
      status: 403,
      json: () => Promise.resolve({ outcome: 'budget_veto' }),
      text: () => Promise.resolve(''),
    });
    const result = await negotiateText({ text: 'trip' });
    expect(result.outcome).toBe('budget_veto');
  });

  it('E.4 negotiateText 500: throws with message containing "HTTP 500"', async () => {
    mockFetch.mockResolvedValue({
      ok: false,
      status: 500,
      json: () => Promise.resolve({}),
      text: () => Promise.resolve('Internal Server Error'),
    });
    await expect(negotiateText({ text: 'trip' })).rejects.toThrow('HTTP 500');
  });

  it('E.5 negotiateText 404: throws with message containing "HTTP 404"', async () => {
    mockFetch.mockResolvedValue({
      ok: false,
      status: 404,
      json: () => Promise.resolve({}),
      text: () => Promise.resolve('Not Found'),
    });
    await expect(negotiateText({ text: 'trip' })).rejects.toThrow('HTTP 404');
  });

  it('E.6 negotiateText network error: rejects propagate', async () => {
    mockFetch.mockRejectedValue(new Error('network failure'));
    await expect(negotiateText({ text: 'trip' })).rejects.toThrow('network failure');
  });

  it('E.7 getEmergencies 200: returns JSON, fetch called with URL containing "/emergencies", no options object', async () => {
    const payload: EmergenciesResponse = { status: 'ok', countries: [] };
    mockFetch.mockResolvedValue({
      ok: true,
      json: () => Promise.resolve(payload),
    });
    const result = await getEmergencies();
    expect(result).toEqual(payload);
    const [url, opts] = mockFetch.mock.calls[0];
    expect(url).toContain('/emergencies');
    expect(opts).toBeUndefined();
  });

  it('E.8 negotiateText sends correct JSON body', async () => {
    mockFetch.mockResolvedValue({
      ok: true,
      status: 200,
      json: () => Promise.resolve({ outcome: 'success', legs: [] }),
      text: () => Promise.resolve(''),
    });
    await negotiateText({ text: 'trip to Tokyo', wallet_balance_cents: 250000 });
    const body = JSON.parse(mockFetch.mock.calls[0][1].body);
    expect(body.wallet_balance_cents).toBe(250000);
    expect(body.text).toBe('trip to Tokyo');
  });
});
