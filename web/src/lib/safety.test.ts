import { describe, it, expect, vi, afterEach } from 'vitest';
import { summarizeWatch, safeGetEmergencies } from './safety';
import type { EmergenciesResponse } from './api';

// ─── summarizeWatch — pure logic ──────────────────────────────────────────
describe('summarizeWatch', () => {
  it('null → unavailable (never clear)', () => {
    const s = summarizeWatch(null);
    expect(s.tone).toBe('unavailable');
    expect(s.label).toBe('feed unavailable');
  });

  it('{status:unavailable} → unavailable (never mistaken for clear)', () => {
    const s = summarizeWatch({ status: 'unavailable', countries: [] });
    expect(s.tone).toBe('unavailable');
  });

  it('mixed high/medium/monitoring → alert; active=2, monitoring=1, label "2 active"', () => {
    const r: EmergenciesResponse = {
      status: 'ok',
      as_of: '2026-06-28',
      source: 'live:gdacs',
      countries: [
        { iso2: 'PH', hazard: 'typhoon', severity: 'high' },
        { iso2: 'MX', hazard: 'hurricane', severity: 'medium' },
        { iso2: 'JP', hazard: 'cyclone', severity: 'monitoring' },
      ],
    };
    const s = summarizeWatch(r);
    expect(s.tone).toBe('alert');
    expect(s.active).toBe(2);
    expect(s.monitoring).toBe(1);
    expect(s.label).toBe('2 active');
    expect(s.asOf).toBe('2026-06-28');
    expect(s.source).toBe('live:gdacs');
  });

  it('monitoring only → tone:monitoring', () => {
    const r: EmergenciesResponse = {
      status: 'ok',
      countries: [{ iso2: 'JP', hazard: 'cyclone', severity: 'monitoring' }],
    };
    const s = summarizeWatch(r);
    expect(s.tone).toBe('monitoring');
    expect(s.label).toBe('1 monitored');
    expect(s.active).toBe(0);
    expect(s.monitoring).toBe(1);
  });

  it('ok + empty countries → clear', () => {
    const r: EmergenciesResponse = { status: 'ok', countries: [] };
    const s = summarizeWatch(r);
    expect(s.tone).toBe('clear');
    expect(s.active).toBe(0);
    expect(s.monitoring).toBe(0);
    expect(s.label).toBe('all clear');
  });
});

// ─── safeGetEmergencies — mock fetch ──────────────────────────────────────
afterEach(() => vi.restoreAllMocks());

describe('safeGetEmergencies', () => {
  it('returns the API response on success', async () => {
    const data: EmergenciesResponse = { status: 'ok', countries: [], as_of: '2026-06-28', source: 'live:gdacs' };
    vi.stubGlobal('fetch', vi.fn(async () => ({
      ok: true, status: 200,
      json: async () => data,
      text: async () => JSON.stringify(data),
    })));
    const result = await safeGetEmergencies();
    expect(result.status).toBe('ok');
    expect(result.countries).toEqual([]);
    // verify it called /emergencies
    const calls = (fetch as unknown as { mock: { calls: [string][] } }).mock.calls;
    expect(calls[0][0]).toMatch(/\/emergencies/);
  });

  it('never rejects — returns unavailable when fetch throws', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => { throw new Error('network'); }));
    const result = await safeGetEmergencies();
    expect(result.status).toBe('unavailable');
    expect(result.countries).toEqual([]);
    // no throw — the promise resolves
  });

  it('never rejects — returns unavailable when fetch returns malformed JSON', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => ({
      ok: true, status: 200,
      json: async () => { throw new SyntaxError('bad json'); },
      text: async () => '',
    })));
    const result = await safeGetEmergencies();
    expect(result.status).toBe('unavailable');
  });
});
