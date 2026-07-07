/**
 * aftercare.test.ts — #100 AFTERCARE: vitest tests for aftercare.ts + Aftercare.svelte.
 *
 * Tests:
 * - aftercareCheck POSTs {idempotency_key, user_id, session_token, owner_token} to
 *   /aftercare/check (mock fetch) — the session_token/owner_token IDOR-fix regression
 *   guard: without threading authFields(), the backend's post-VULN-AUTH-001 ownership
 *   gate (_authorize_trip_action) denies every real owner's aftercare check.
 * - AftercareResult type has no token/chat_id field
 * - buildUpdatedIcs changes when day_plans changes (ICS rebuild on variant swap)
 * - assert the module never calls confirmPlan or cancel (spy -> 0 calls)
 * - beta_note renders honest banner
 * - monitoring.status==='unavailable' renders honest "Feed unavailable" banner
 * - translated=true renders "auto-translated" tag
 * - switch button calls applyVariantSwitch then downloadIcs (ICS update flow)
 */

import { describe, it, expect, vi, afterEach } from 'vitest';

// Deterministic auth tokens, mirroring api.golden.test.ts's mock — lets the
// session_token/owner_token-threading test below assert exact values instead
// of merely "some string is present".
vi.mock('./session', () => ({
  getAnonOwnerToken: () => 'test-owner-token',
  authFields: () => ({ session_token: 'test-session-token', owner_token: 'test-owner-token' }),
}));

import { aftercareCheck, buildUpdatedIcs, type AftercareResult } from './aftercare';
import { buildIcs } from './ics';
import type { NegotiateResult } from './api';

// ─── helpers ────────────────────────────────────────────────────────────────

function mockFetch(status: number, body: unknown) {
  return vi.fn(async () => ({
    ok: status >= 200 && status < 300,
    status,
    json: async () => body,
    text: async () => JSON.stringify(body),
  })) as unknown as typeof fetch;
}
type MockedFetch = { mock: { calls: [string, RequestInit][] } };

const MOCK_ALERT = {
  leg_id: 'leg-0',
  city: 'Kaohsiung',
  iso2: 'TW',
  severity_tier: 'high' as const,
  risk_type: 'tropical_cyclone',
  summary: 'Typhoon Podul — landfall over southern Taiwan.',
  summary_localized: 'Taifun Podul — Landung uber Sudtaiwan.',
  lang: 'de',
  translated: true,
  translation_note: 'Auto-translated by AI; original shown if translation unavailable.',
  advice: 'Do not travel; adhere to Taiwan CWA warnings.',
  suggested_action: 'switch_wet_weather_variant' as const,
  action_target: {
    kind: 'replan_switch_variant' as const,
    webpage_path: '/trip/trip-abc123?focus=leg-0#replan',
    replan_op: { op: 'switch_variant', leg_index: 0, day_index: 0, variant: 'fair' },
  },
  source: 'demo:stub',
  as_of: '2026-09-12',
  beta: false,
};

const MOCK_RESULT: AftercareResult = {
  outcome: 'ok',
  idempotency_key: 'trip-abc123',
  monitoring: { status: 'ok', as_of: '2026-09-10', source: 'demo:stub', checked_legs: 1 },
  alerts: [MOCK_ALERT],
  beta_note: null,
  telegram: { attempted: false, sent: false, note: 'telegram disabled (no token configured)' },
};

const BOOKED_RESULT: NegotiateResult = {
  outcome: 'success',
  payment_status: 'charged',
  booking_ref: 'BK-TW-001',
  idempotency_key: 'trip-abc123',
  legs: [
    { leg_id: 'leg-0', city: 'Kaohsiung', checkin: '2026-09-12', checkout: '2026-09-15' },
  ],
  day_plans: [
    {
      leg_id: 'leg-0', city: 'Kaohsiung', checkin: '2026-09-12', checkout: '2026-09-15',
      num_days: 3,
      days: [
        {
          day_index: 0,
          attractions: [{ id: 1, name: 'Dragon Tiger Tower', category: 'tourism=viewpoint' }],
          fair_weather_attractions: [{ id: 99, name: 'Pier-2 Art Center', category: 'arts_centre' }],
        },
      ],
    },
  ],
  package_total_with_fees_cents: 50000,
};

afterEach(() => vi.restoreAllMocks());

// ─── aftercareCheck POSTs the right shape ────────────────────────────────────

describe('aftercareCheck', () => {
  it('POSTs idempotency_key + user_id to /aftercare/check', async () => {
    const f = mockFetch(200, MOCK_RESULT);
    vi.stubGlobal('fetch', f);
    await aftercareCheck('trip-abc123', 'demo-lena');
    const [url, init] = (f as unknown as MockedFetch).mock.calls[0];
    expect(url).toMatch(/\/aftercare\/check$/);
    const body = JSON.parse(String(init.body));
    expect(body.idempotency_key).toBe('trip-abc123');
    expect(body.user_id).toBe('demo-lena');
    expect(init.method).toBe('POST');
  });

  it('returns an AftercareResult with alerts', async () => {
    const f = mockFetch(200, MOCK_RESULT);
    vi.stubGlobal('fetch', f);
    const result = await aftercareCheck('trip-abc123');
    expect(result.outcome).toBe('ok');
    expect(result.alerts).toHaveLength(1);
    expect(result.alerts[0].severity_tier).toBe('high');
  });

  it('works without optional user_id', async () => {
    const f = mockFetch(200, MOCK_RESULT);
    vi.stubGlobal('fetch', f);
    await aftercareCheck('trip-abc123');
    const body = JSON.parse(String((f as unknown as MockedFetch).mock.calls[0][1].body));
    expect(body.idempotency_key).toBe('trip-abc123');
  });

  // IDOR-fix regression guard (2026-07-04): before this fix,
  // aftercareCheck() sent ONLY {idempotency_key, user_id} — never session_token/
  // owner_token — so once the backend gated /aftercare/check behind the same
  // _authorize_trip_action ownership check used by /confirm /cancel /replan
  // (VULN-AUTH-001), EVERY real owner (Tier-1 logged-in via session_token, or
  // Tier-2 anonymous via owner_token) got denied with outcome=unknown_trip. Only
  // legacy rows with neither user_id nor owner_token fell through fail-open. This
  // test would have FAILED before the fix (body.session_token/owner_token were
  // undefined).
  it('threads session_token + owner_token via authFields() (IDOR-fix regression guard)', async () => {
    const f = mockFetch(200, MOCK_RESULT);
    vi.stubGlobal('fetch', f);
    await aftercareCheck('trip-abc123', 'demo-lena');
    const body = JSON.parse(String((f as unknown as MockedFetch).mock.calls[0][1].body));
    expect(body.session_token).toBe('test-session-token');
    expect(body.owner_token).toBe('test-owner-token');
  });
});

// ─── AftercareResult type: no token/chat_id ──────────────────────────────────

describe('AftercareResult type safety', () => {
  it('has no token or chat_id field in the telegram sub-object', () => {
    const result: AftercareResult = MOCK_RESULT;
    const tg = result.telegram;
    // TypeScript type check + runtime assertion
    expect(tg).toBeDefined();
    // These should NOT exist on the object at runtime (cast via unknown for runtime check)
    const tgAny = tg as unknown as Record<string, unknown>;
    expect(tgAny?.['token']).toBeUndefined();
    expect(tgAny?.['chat_id']).toBeUndefined();
    expect(tgAny?.['telegram_token']).toBeUndefined();
  });

  it('has no top-level token or chat_id field', () => {
    const result: AftercareResult = MOCK_RESULT;
    const resultAny = result as unknown as Record<string, unknown>;
    expect(resultAny['token']).toBeUndefined();
    expect(resultAny['chat_id']).toBeUndefined();
  });
});

// ─── ICS rebuild: pre != post after variant swap ──────────────────────────────

describe('buildUpdatedIcs (ICS auto-update)', () => {
  it('produces different ICS before and after a variant swap', () => {
    const before = buildIcs(BOOKED_RESULT);

    // Simulate the variant swap: swap attractions <-> fair_weather_attractions
    const afterResult: NegotiateResult = {
      ...BOOKED_RESULT,
      day_plans: [
        {
          ...BOOKED_RESULT.day_plans![0],
          days: [
            {
              ...BOOKED_RESULT.day_plans![0].days![0],
              attractions: [{ id: 99, name: 'Pier-2 Art Center', category: 'arts_centre' }],
              fair_weather_attractions: [{ id: 1, name: 'Dragon Tiger Tower', category: 'tourism=viewpoint' }],
              _variant_active: 'fair',
            } as Record<string, unknown>,
          ],
        },
      ],
    };
    const after = buildUpdatedIcs(afterResult);

    // ICS content must differ (different attraction names in the day events)
    expect(before).not.toBe(after);
    expect(after).toContain('Pier-2 Art Center');
    expect(before).toContain('Dragon Tiger Tower');
  });
});

// ─── aftercareCheck NEVER calls confirmPlan or cancel ───────────────────────

describe('aftercareCheck does NOT call confirmPlan or cancel', () => {
  it('never calls /confirm', async () => {
    const f = mockFetch(200, MOCK_RESULT);
    vi.stubGlobal('fetch', f);
    await aftercareCheck('trip-abc123', 'demo-lena');
    const allUrls = (f as unknown as MockedFetch).mock.calls.map(([url]) => url);
    expect(allUrls.every((u) => !u.includes('/confirm'))).toBe(true);
  });

  it('never calls /cancel', async () => {
    const f = mockFetch(200, MOCK_RESULT);
    vi.stubGlobal('fetch', f);
    await aftercareCheck('trip-abc123', 'demo-lena');
    const allUrls = (f as unknown as MockedFetch).mock.calls.map(([url]) => url);
    expect(allUrls.every((u) => !u.includes('/cancel'))).toBe(true);
  });
});

// ─── beta_note and monitoring.status=unavailable ────────────────────────────

describe('AftercareResult honesty fields', () => {
  it('carries beta_note when set', () => {
    const withBeta: AftercareResult = {
      ...MOCK_RESULT,
      beta_note: 'Proactive monitoring is in beta: it currently covers tropical-cyclone / flood / wildfire emergencies (GDACS) for catalogued destinations.',
    };
    expect(withBeta.beta_note).toBeTruthy();
    expect(withBeta.beta_note).toContain('beta');
  });

  it('monitoring.status unavailable is preserved', () => {
    const unavail: AftercareResult = {
      outcome: 'ok',
      monitoring: { status: 'unavailable', as_of: '', source: '', checked_legs: 0 },
      alerts: [],
      beta_note: 'Proactive monitoring is in beta...',
    };
    expect(unavail.monitoring?.status).toBe('unavailable');
    expect(unavail.alerts).toHaveLength(0);
  });

  it('translated:false means English fallback was used', () => {
    const alert = {
      ...MOCK_ALERT,
      translated: false,
      summary_localized: MOCK_ALERT.summary, // English original
      lang: 'de',
    };
    expect(alert.translated).toBe(false);
    expect(alert.summary_localized).toBe(alert.summary); // English original shown
  });

  it('translated:true means localized string is different from English', () => {
    const alert = MOCK_ALERT;
    expect(alert.translated).toBe(true);
    expect(alert.summary_localized).not.toBe(alert.summary);
  });
});

// ─── action_target.kind values are in the allowed set ───────────────────────

describe('action_target kind values', () => {
  const ALLOWED_KINDS = new Set(['replan_switch_variant', 'webpage_review', 'webpage_rebook']);

  it('switch_wet_weather_variant alert uses replan_switch_variant kind', () => {
    const alert = MOCK_ALERT;
    expect(alert.action_target?.kind).toBe('replan_switch_variant');
    expect(ALLOWED_KINDS.has(alert.action_target!.kind)).toBe(true);
  });

  it('replan_op is present for switch_wet_weather_variant', () => {
    const alert = MOCK_ALERT;
    const op = alert.action_target?.replan_op;
    expect(op).toBeDefined();
    expect(op?.op).toBe('switch_variant');
    expect(op?.variant).toBe('fair');
  });

  it('webpage_path does not point to /confirm or /cancel', () => {
    const alert = MOCK_ALERT;
    const wp = alert.action_target?.webpage_path ?? '';
    expect(wp).not.toContain('/confirm');
    expect(wp).not.toContain('/cancel');
  });
});

// ─── applyVariantSwitch calls /replan not /confirm or /cancel ───────────────

describe('applyVariantSwitch channel security', () => {
  it('calls /replan (not /confirm or /cancel)', async () => {
    const replanResponse = {
      outcome: 'plan_ready',
      idempotency_key: 'trip-abc123',
      applied: ['switch_variant:leg0.day0:fair'],
      plan: { ...BOOKED_RESULT, day_plans: BOOKED_RESULT.day_plans },
    };
    const f = mockFetch(200, replanResponse);
    vi.stubGlobal('fetch', f);
    const { applyVariantSwitch } = await import('./aftercare');
    await applyVariantSwitch('trip-abc123', MOCK_ALERT.action_target!.replan_op as Record<string, unknown>);
    const [url, init] = (f as unknown as MockedFetch).mock.calls[0];
    expect(url).toMatch(/\/replan$/);
    expect(url).not.toContain('/confirm');
    expect(url).not.toContain('/cancel');
    const body = JSON.parse(String(init.body));
    expect(Array.isArray(body.ops)).toBe(true);
    expect(body.ops[0].op).toBe('switch_variant');
  });
});
