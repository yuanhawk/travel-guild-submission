import { describe, it, expect, vi, afterEach } from 'vitest';
import { centsToUsd, bpToPct, riskSignalList, refineTrip, fmtIndicative,
         replanTrip, replanApplied, isBookedEnvelope, negotiateText, assumptionNotes,
         tripDateRange, forbiddenMessage, isForbidden, NOT_TRIP_OWNER_MESSAGE,
         SESSION_INVALID_MESSAGE, safeHref } from './api';
import type { NegotiateResult, CurrencyReview, ReplanOp, ReplanResponse } from './api';

// ─── forbiddenMessage — #203/#207: session_invalid vs not_trip_owner copy ──
describe('forbiddenMessage', () => {
  it('returns SESSION_INVALID_MESSAGE for reason:"session_invalid" (expired/wiped session — actionable)', () => {
    expect(forbiddenMessage({ reason: 'session_invalid' })).toBe(SESSION_INVALID_MESSAGE);
  });

  it('returns NOT_TRIP_OWNER_MESSAGE for reason:"not_trip_owner" (genuine cross-user violation — unchanged)', () => {
    expect(forbiddenMessage({ reason: 'not_trip_owner' })).toBe(NOT_TRIP_OWNER_MESSAGE);
  });

  it('falls back to NOT_TRIP_OWNER_MESSAGE when reason is absent (old backend, pre-#203 — no crash/undefined)', () => {
    expect(forbiddenMessage({})).toBe(NOT_TRIP_OWNER_MESSAGE);
    expect(forbiddenMessage(null)).toBe(NOT_TRIP_OWNER_MESSAGE);
    expect(forbiddenMessage(undefined)).toBe(NOT_TRIP_OWNER_MESSAGE);
  });

  it('falls back to NOT_TRIP_OWNER_MESSAGE for an unrecognized reason value', () => {
    expect(forbiddenMessage({ reason: 'something_new_the_backend_added' })).toBe(NOT_TRIP_OWNER_MESSAGE);
  });

  it('composes with isForbidden on a full 403 {outcome:"forbidden"} envelope', () => {
    const sessionInvalid = { outcome: 'forbidden', reason: 'session_invalid', idempotency_key: 'k1' };
    const notOwner = { outcome: 'forbidden', reason: 'not_trip_owner', idempotency_key: 'k2' };
    const legacy: { outcome: string; idempotency_key: string; reason?: string } =
      { outcome: 'forbidden', idempotency_key: 'k3' }; // pre-#203 backend: no reason field
    expect(isForbidden(sessionInvalid)).toBe(true);
    expect(forbiddenMessage(sessionInvalid)).toBe(SESSION_INVALID_MESSAGE);
    expect(isForbidden(notOwner)).toBe(true);
    expect(forbiddenMessage(notOwner)).toBe(NOT_TRIP_OWNER_MESSAGE);
    expect(isForbidden(legacy)).toBe(true);
    expect(forbiddenMessage(legacy)).toBe(NOT_TRIP_OWNER_MESSAGE);
  });
});

// ─── assumptionNotes — honest-assumption disclosure aggregator (#188) ──────
describe('assumptionNotes', () => {
  it('returns [] for null', () => {
    expect(assumptionNotes(null)).toEqual([]);
  });

  it('returns [] for a clean envelope with no assumption fields set', () => {
    expect(assumptionNotes({ outcome: 'plan_ready' })).toEqual([]);
  });

  it('returns exactly the non-empty note strings, in stable field order', () => {
    const r: NegotiateResult = {
      outcome: 'plan_ready',
      date_assumption_note: 'I assumed you meant starting tomorrow since no date was given.',
      date_year_assumption_note: 'I assumed 2027 since April 10 has already passed this year.',
      adults_assumption_note: 'I assumed 1 traveler since no party size was given.',
      currency_assumption_note: 'I assumed USD for the $1500 budget since no currency was specified.',
      children_note: 'Children were mentioned but not priced into this plan.',
    };
    expect(assumptionNotes(r)).toEqual([
      'I assumed you meant starting tomorrow since no date was given.',
      'I assumed 2027 since April 10 has already passed this year.',
      'I assumed 1 traveler since no party size was given.',
      'I assumed USD for the $1500 budget since no currency was specified.',
      'Children were mentioned but not priced into this plan.',
    ]);
  });

  it('ignores null/undefined/empty-string fields, keeping only genuinely set notes', () => {
    const r: NegotiateResult = {
      outcome: 'plan_ready',
      date_assumption_note: null,
      date_year_assumption_note: undefined,
      adults_assumption_note: '',
      currency_assumption_note: 'I assumed USD for the $1500 budget since no currency was specified.',
      children_note: null,
    };
    expect(assumptionNotes(r)).toEqual([
      'I assumed USD for the $1500 budget since no currency was specified.',
    ]);
  });
});

// ─── tripDateRange — concrete date range companion to the assumption banner ──
describe('tripDateRange', () => {
  it('returns null for null', () => {
    expect(tripDateRange(null)).toBeNull();
  });

  it('returns null when no leg/day_plan carries checkin/checkout (no fabrication)', () => {
    const r: NegotiateResult = {
      outcome: 'plan_ready',
      legs: [{ leg_id: 'leg-0', city: 'Kyoto' }],
      day_plans: [{ leg_id: 'leg-0', city: 'Kyoto' }],
    };
    expect(tripDateRange(r)).toBeNull();
  });

  it('returns null when legs/day_plans are absent entirely', () => {
    expect(tripDateRange({ outcome: 'plan_ready' })).toBeNull();
  });

  it('computes the range + night count for a single leg', () => {
    const r: NegotiateResult = {
      outcome: 'plan_ready',
      legs: [{ leg_id: 'leg-0', city: 'Kyoto', checkin: '2026-10-01', checkout: '2026-10-08' }],
      day_plans: [{ leg_id: 'leg-0', city: 'Kyoto', checkin: '2026-10-01', checkout: '2026-10-08' }],
    };
    const got = tripDateRange(r);
    expect(got).not.toBeNull();
    expect(got!.startISO).toBe('2026-10-01');
    expect(got!.endISO).toBe('2026-10-08');
    expect(got!.nights).toBe(7);
    expect(got!.label).toBe('Oct 1 – Oct 8, 2026 (7 nights)');
  });

  it('spans the earliest checkin to the latest checkout across MULTIPLE legs (not just leg[0])', () => {
    const r: NegotiateResult = {
      outcome: 'plan_ready',
      legs: [
        { leg_id: 'leg-1', city: 'Osaka', checkin: '2026-10-04', checkout: '2026-10-08' },
        { leg_id: 'leg-0', city: 'Kyoto', checkin: '2026-10-01', checkout: '2026-10-04' },
      ],
    };
    const got = tripDateRange(r);
    expect(got).not.toBeNull();
    expect(got!.startISO).toBe('2026-10-01');
    expect(got!.endISO).toBe('2026-10-08');
    expect(got!.nights).toBe(7);
    expect(got!.label).toBe('Oct 1 – Oct 8, 2026 (7 nights)');
  });

  it('shows the year on both ends when the range crosses a year boundary', () => {
    const r: NegotiateResult = {
      outcome: 'plan_ready',
      legs: [{ leg_id: 'leg-0', city: 'Sydney', checkin: '2026-12-28', checkout: '2027-01-03' }],
    };
    const got = tripDateRange(r);
    expect(got).not.toBeNull();
    expect(got!.nights).toBe(6);
    expect(got!.label).toBe('Dec 28, 2026 – Jan 3, 2027 (6 nights)');
  });

  it('falls back to day_plans when legs carry no dates', () => {
    const r: NegotiateResult = {
      outcome: 'plan_ready',
      legs: [{ leg_id: 'leg-0', city: 'Kyoto' }],
      day_plans: [{ leg_id: 'leg-0', city: 'Kyoto', checkin: '2026-10-01', checkout: '2026-10-08' }],
    };
    const got = tripDateRange(r);
    expect(got).not.toBeNull();
    expect(got!.nights).toBe(7);
  });

  it('returns null for an inverted/non-positive range (defensive, never fabricate)', () => {
    const r: NegotiateResult = {
      outcome: 'plan_ready',
      legs: [{ leg_id: 'leg-0', city: 'Kyoto', checkin: '2026-10-08', checkout: '2026-10-01' }],
    };
    expect(tripDateRange(r)).toBeNull();
  });
});

// ─── negotiateText — #1 AbortSignal forwarding ─────────────────────────────
describe('negotiateText — forwards AbortSignal into fetch init', () => {
  afterEach(() => vi.unstubAllGlobals());
  it('passes its signal arg through to fetch(init.signal)', async () => {
    let captured: AbortSignal | undefined;
    const f = vi.fn(async (_url: string, init?: RequestInit) => {
      captured = init?.signal ?? undefined;
      return { ok: true, status: 200, json: async () => ({ outcome: 'plan_ready' }), text: async () => '' };
    });
    vi.stubGlobal('fetch', f);

    const ac = new AbortController();
    await negotiateText({ text: 'Tokyo 3 nights' }, ac.signal);
    expect(captured).toBe(ac.signal);
  });
});

// ─── refineTrip — /refine contract guard ───────────────────────────────────
// Tests validate the request SHAPE and response parsing; no server needed.

function mockRefine(status: number, body: unknown) {
  return vi.fn(async () => ({
    ok: status >= 200 && status < 300,
    status,
    json: async () => body,
    text: async () => JSON.stringify(body),
  })) as unknown as typeof fetch;
}
type MockedFetch = { mock: { calls: [string, RequestInit][] } };

describe('refineTrip — POST /refine request shape', () => {
  afterEach(() => vi.restoreAllMocks());

  it('POSTs required fields idempotency_key + message to /refine', async () => {
    const f = mockRefine(200, { outcome: 'plan_ready', session_id: 'sid-1',
      idempotency_key: 'trip-new', assistant_reply: 'Done.', plan: {} });
    vi.stubGlobal('fetch', f);
    await refineTrip({ idempotency_key: 'trip-old', message: 'add Osaka' });
    const [url, init] = (f as unknown as MockedFetch).mock.calls[0];
    expect(url).toMatch(/\/refine$/);
    const body = JSON.parse(String(init.body));
    expect(body.idempotency_key).toBe('trip-old');
    expect(body.message).toBe('add Osaka');
    expect(init.method).toBe('POST');
  });

  it('includes optional session_id, user_id, wallet_balance_cents when supplied', async () => {
    const f = mockRefine(200, { outcome: 'plan_ready' }); vi.stubGlobal('fetch', f);
    await refineTrip({
      idempotency_key: 'trip-abc', message: 'cheaper',
      session_id: 'sess-xyz', user_id: 'demo-mei', wallet_balance_cents: 350000,
    });
    const body = JSON.parse(String((f as unknown as MockedFetch).mock.calls[0][1].body));
    expect(body.session_id).toBe('sess-xyz');
    expect(body.user_id).toBe('demo-mei');
    expect(body.wallet_balance_cents).toBe(350000);
  });

  it('returns plan_ready with new idempotency_key + assistant_reply + plan envelope', async () => {
    const fakePlan: NegotiateResult = {
      outcome: 'plan_ready', idempotency_key: 'trip-new',
      legs: [{ leg_id: 'l0', city: 'Tokyo' }, { leg_id: 'l1', city: 'Osaka' }],
      package_total_cents: 425000,
    };
    vi.stubGlobal('fetch', mockRefine(200, {
      outcome: 'plan_ready', session_id: 'sess-1', idempotency_key: 'trip-new',
      assistant_reply: 'Added Osaka (3 nights). New total $4,250.',
      changed: ['legs.add:osaka'],
      plan: fakePlan,
    }));
    const r = await refineTrip({ idempotency_key: 'trip-old', message: 'add Osaka' });
    expect(r.outcome).toBe('plan_ready');
    expect(r.idempotency_key).toBe('trip-new');       // NEW KEY — frontend must swap
    expect(r.session_id).toBe('sess-1');
    expect(r.assistant_reply).toContain('Osaka');
    expect(r.changed).toContain('legs.add:osaka');
    expect(r.plan?.legs).toHaveLength(2);
    expect(r.kept_previous).toBeUndefined();
  });

  it('returns refine_partial when only swap_item ops (no re-plan, same key)', async () => {
    vi.stubGlobal('fetch', mockRefine(200, {
      outcome: 'refine_partial',
      session_id: 'sess-1',
      assistant_reply: "I can't swap individual restaurants yet — I can change budget, dates, cities, length, or interests.",
      kept_previous: undefined,
    }));
    const r = await refineTrip({ idempotency_key: 'trip-old', message: 'swap the ramen place' });
    expect(r.outcome).toBe('refine_partial');
    expect(r.idempotency_key).toBeUndefined();   // no new key — old plan still held
    expect(r.assistant_reply).toContain("can't swap");
  });

  it('returns kept_previous:true when re-plan fails (budget exceeded etc.)', async () => {
    vi.stubGlobal('fetch', mockRefine(200, {
      outcome: 'cannot_satisfy', kept_previous: true,
      assistant_reply: "That change would exceed your budget — kept your current plan.",
    }));
    const r = await refineTrip({ idempotency_key: 'trip-old', message: 'add 10 more cities' });
    expect(r.kept_previous).toBe(true);
    expect(r.assistant_reply).toContain('kept');
  });

  it('returns unknown_plan when the key is not found', async () => {
    vi.stubGlobal('fetch', mockRefine(200, { outcome: 'unknown_plan', idempotency_key: 'trip-ghost' }));
    const r = await refineTrip({ idempotency_key: 'trip-ghost', message: 'anything' });
    expect(r.outcome).toBe('unknown_plan');
  });

  it('returns plan_locked when the plan is already booked', async () => {
    vi.stubGlobal('fetch', mockRefine(200, { outcome: 'plan_locked' }));
    const r = await refineTrip({ idempotency_key: 'trip-booked', message: 'make cheaper' });
    expect(r.outcome).toBe('plan_locked');
  });

  it('throws on non-402/403 HTTP errors (not a structured response)', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => ({
      ok: false, status: 500, text: async () => 'Internal Server Error',
    })));
    await expect(refineTrip({ idempotency_key: 'trip-x', message: 'fail' })).rejects.toThrow();
  });

  it('itinerary_narrative in the plan is an object with .overview (NOT a flat string)', async () => {
    vi.stubGlobal('fetch', mockRefine(200, {
      outcome: 'plan_ready', idempotency_key: 'trip-new',
      plan: {
        outcome: 'plan_ready',
        itinerary_narrative: {
          overview: 'A beautiful journey through Japan.',
          legs: [{ leg_id: 'l0', city: 'Tokyo', summary: 'Bustling metropolis.' }],
        },
      },
    }));
    const r = await refineTrip({ idempotency_key: 'trip-old', message: 'narrate it' });
    // Contract guard: narrative is an object, .overview is the string for the chat bubble
    expect(typeof r.plan?.itinerary_narrative).toBe('object');
    expect(r.plan?.itinerary_narrative?.overview).toContain('Japan');
    expect(r.plan?.itinerary_narrative?.overview?.length ?? 0).toBeGreaterThan(4);
  });

  it('itinerary_narrative carries stale + stale_reason after a structural /replan edit (server.py:2282-2288)', async () => {
    vi.stubGlobal('fetch', mockRefine(200, {
      outcome: 'plan_ready', idempotency_key: 'trip-stale',
      plan: {
        outcome: 'plan_ready',
        itinerary_narrative: {
          overview: 'A beautiful journey through Japan.',
          stale: true,
          stale_reason: "This AI-written summary was generated before your latest edit and may still describe stops that were since removed, added, or reordered.",
        },
      },
    }));
    const r = await refineTrip({ idempotency_key: 'trip-old', message: 'swap day 2' });
    expect(r.plan?.itinerary_narrative?.stale).toBe(true);
    expect(r.plan?.itinerary_narrative?.stale_reason).toContain('generated before your latest edit');
  });

  it('itinerary_narrative without stale (normal case) leaves stale undefined — no fabrication', async () => {
    vi.stubGlobal('fetch', mockRefine(200, {
      outcome: 'plan_ready', idempotency_key: 'trip-fresh',
      plan: { outcome: 'plan_ready', itinerary_narrative: { overview: 'A gentle first day.' } },
    }));
    const r = await refineTrip({ idempotency_key: 'trip-old', message: 'narrate it' });
    expect(r.plan?.itinerary_narrative?.stale).toBeUndefined();
    expect(r.plan?.itinerary_narrative?.stale_reason).toBeUndefined();
  });
});

describe('centsToUsd', () => {
  it('formats cents as USD with 2dp + thousands separators', () => {
    expect(centsToUsd(184000)).toBe('$1,840.00');
    expect(centsToUsd(199540)).toBe('$1,995.40');
    expect(centsToUsd(0)).toBe('$0.00');
  });
  it('renders an em-dash for null/undefined (never a fabricated $0)', () => {
    expect(centsToUsd(null)).toBe('—');
    expect(centsToUsd(undefined)).toBe('—');
  });
});

// ─── safeHref — #214: case-insensitive https:// scheme match ──────────────
// XSS-safe external-href guard for monetized booking deeplinks. Baseline allow/deny
// behavior already gets light indirect coverage in price-overlay.test.ts; this block
// is the dedicated, exhaustive unit-level guard (incl. the #214 regression: URL
// schemes are case-INSENSITIVE per RFC 3986 §3.1, so `Https://`/`HTTPS://`/etc. must
// be allowed — but the deliberate HTTPS-only policy (reject http:// in ANY casing)
// must NOT be loosened by the fix).
describe('safeHref', () => {
  it('allows a lowercase https:// URL (unchanged baseline)', () => {
    expect(safeHref('https://book.example.com/x?aff=1')).toBe('https://book.example.com/x?aff=1');
  });

  it('#214 fix: allows mixed/upper-case https schemes — Https://, HTTPS://, hTTps://', () => {
    expect(safeHref('Https://book.example.com/x')).toBe('Https://book.example.com/x');
    expect(safeHref('HTTPS://book.example.com/x')).toBe('HTTPS://book.example.com/x');
    expect(safeHref('hTTps://book.example.com/x')).toBe('hTTps://book.example.com/x');
  });

  it('returns the href UNMODIFIED when safe — never normalizes/lowercases the returned string', () => {
    const raw = 'HTTPS://Book.Example.COM/Path?Aff=1';
    expect(safeHref(raw)).toBe(raw);
  });

  it('allows a same-origin relative path starting with / (unchanged baseline)', () => {
    expect(safeHref('/trips/abc')).toBe('/trips/abc');
  });

  it('still rejects plain lowercase http:// — deliberate HTTPS-only policy, not loosened by the fix', () => {
    expect(safeHref('http://insecure.example.com')).toBe('');
  });

  it('still rejects http:// in any casing (Http://, HTTP://) — insecure scheme blocked regardless of case', () => {
    expect(safeHref('Http://insecure.example.com')).toBe('');
    expect(safeHref('HTTP://insecure.example.com')).toBe('');
  });

  it('rejects javascript: in any casing', () => {
    expect(safeHref('javascript:alert(1)')).toBe('');
    expect(safeHref('JavaScript:alert(1)')).toBe('');
    expect(safeHref('JAVASCRIPT:alert(1)')).toBe('');
  });

  it('rejects data: URIs', () => {
    expect(safeHref('data:text/html,<script>alert(1)</script>')).toBe('');
    expect(safeHref('DATA:text/html,<script>alert(1)</script>')).toBe('');
  });

  it('rejects protocol-relative //host (case has no bearing — rejected by the leading // check)', () => {
    expect(safeHref('//evil.example.com/phish')).toBe('');
  });

  it('returns "" for null, undefined, and empty-string input', () => {
    expect(safeHref(null)).toBe('');
    expect(safeHref(undefined)).toBe('');
    expect(safeHref('')).toBe('');
  });

  it('trims leading/trailing whitespace around an otherwise-valid https:// URL', () => {
    expect(safeHref('   https://book.example.com/x  ')).toBe('https://book.example.com/x');
    expect(safeHref('  Https://book.example.com/x  ')).toBe('Https://book.example.com/x');
  });
});

describe('bpToPct (basis-points → percent)', () => {
  it('converts bp to 1-dp percent', () => {
    expect(bpToPct(4800)).toBe(48);
    expect(bpToPct(2880)).toBe(28.8);
    expect(bpToPct(500)).toBe(5);
    expect(bpToPct(0)).toBe(0);
  });
  it('returns null for null/undefined', () => {
    expect(bpToPct(null)).toBeNull();
    expect(bpToPct(undefined)).toBeNull();
  });
});

// Cross-repo contract guard: the served shape this frontend depends on.
// risk_signals is a CONSOLIDATED object with per_leg[]; per-leg gate verdict is
// `decisions{}` (object) and `advisory[]` (array) — NOT decision/advisory_level.
describe('contract shape: risk_signals.per_leg + decisions/advisory + *_cents', () => {
  const result: NegotiateResult = {
    outcome: 'success',
    total_booked_cents: 184000,
    package_total_with_fees_cents: 199540,
    booking_ref: 'BK-JP-3',
    risk_signals: {
      consolidator: 'risk_agent',
      any_avoid_window: true,
      flagged_legs: ['leg-1'],
      per_leg: [
        {
          leg_id: 'leg-1',
          alert_tier: 'HIGH',
          cyclone_likelihood_pct: 28.8,
          cyclone_basin: 'typhoon',
          flood_index_bp: 4800,
          decisions: { flag: true, avoid_window: true },
          advisory: [{ type: 'cyclone_window', severity: 'high', detail: 'AVOID this window' }],
          reason_codes: ['natural_disaster', 'flood'],
        },
      ],
    },
  };

  it('reads per-leg risk from the consolidated block', () => {
    const legs = riskSignalList(result.risk_signals);
    expect(legs).toHaveLength(1);
    const lr = legs[0];
    expect(lr.decisions?.avoid_window).toBe(true); // object, NOT lr.decision
    expect(lr.advisory?.[0].severity).toBe('high'); // array, NOT advisory_level
    expect(bpToPct(lr.flood_index_bp)).toBe(48);
    expect(lr.cyclone_basin).toBe('typhoon');
  });

  it('money fields are cents → formatted', () => {
    expect(centsToUsd(result.total_booked_cents)).toBe('$1,840.00');
    expect(centsToUsd(result.package_total_with_fees_cents)).toBe('$1,995.40');
  });

  it('riskSignalList tolerates null/empty risk_signals', () => {
    expect(riskSignalList(null)).toEqual([]);
    expect(riskSignalList({})).toEqual([]);
  });
});

// ─── fmtIndicative (#101) ──────────────────────────────────────────────────
// Guards: pure display formatting of a server-supplied integer (no FX math).

const sgdReview: CurrencyReview = {
  display_currency: 'SGD',
  usd_cents: 324000,
  indicative_minor_units: 1290000,  // 12900.00 SGD in minor units (cents)
  decimals: 2,
  as_of: '2026-06',
  indicative: true,
  basis: 'seeded_snapshot',
  disclaimer: 'Indicative only (seeded snapshot 2026-06); the charge is in USD.',
};

const jpyReview: CurrencyReview = {
  display_currency: 'JPY',
  usd_cents: 100000,
  indicative_minor_units: 16150,   // 16,150 JPY (no decimals)
  decimals: 0,
  as_of: '2026-06',
  indicative: true,
  basis: 'seeded_snapshot',
  disclaimer: 'Indicative only; charge is in USD.',
};

describe('fmtIndicative — display formatter (no client FX math)', () => {
  it('SGD: formats indicative_minor_units / 100 with 2 decimals + currency code', () => {
    const r = fmtIndicative(sgdReview);
    expect(r).toBe('≈ SGD 12,900.00');
  });

  it('JPY: formats with 0 decimal places (no fractional yen)', () => {
    const r = fmtIndicative(jpyReview);
    expect(r).toBe('≈ JPY 16,150');
  });

  it('returns null for null input', () => {
    expect(fmtIndicative(null)).toBeNull();
  });

  it('returns null for undefined input', () => {
    expect(fmtIndicative(undefined)).toBeNull();
  });

  it('returns null when indicative is false (not a live rate)', () => {
    expect(fmtIndicative({ ...sgdReview, indicative: false })).toBeNull();
  });

  it('does not recompute from usd_cents — only formats indicative_minor_units', () => {
    // If the FE were recomputing it would use usd_cents + a rate.
    // Instead, fmtIndicative only formats the pre-converted indicative_minor_units.
    // Changing usd_cents to a wildly different value has no effect on output.
    const mutated: CurrencyReview = { ...sgdReview, usd_cents: 999999999 };
    expect(fmtIndicative(mutated)).toBe('≈ SGD 12,900.00');  // unchanged
  });
});

// ─── replanTrip — POST /replan contract guard (#97/#98) ───────────────────
// Guards the request SHAPE + response parsing for the deterministic edit-lane.
// No server needed — all tests use mocked fetch.

function mockReplan(status: number, body: unknown) {
  return vi.fn(async () => ({
    ok: status >= 200 && status < 300,
    status,
    json: async () => body,
    text: async () => JSON.stringify(body),
  })) as unknown as typeof fetch;
}
type MockFetch = { mock: { calls: [string, RequestInit][] } };

const fakePlanEnvelope: NegotiateResult = {
  outcome: 'plan_ready',
  idempotency_key: 'trip-123',
  day_plans: [
    { leg_id: 'l0', city: 'Tokyo', num_days: 2, days: [
      { day_index: 0, attractions: [{ name: 'Senso-ji', category: 'tourism=temple' }], meals: {} },
    ]},
  ],
  legs: [{ leg_id: 'l0', city: 'Tokyo' }],
  package_total_cents: 280000,
  payment_status: 'held',
};

describe('replanTrip — POST /replan request shape', () => {
  afterEach(() => vi.restoreAllMocks());

  // T1: POST shape
  it('POSTs idempotency_key + ops[] to /replan', async () => {
    const f = mockReplan(200, {
      outcome: 'plan_ready', applied: ['remove:leg0.day0.pos0:senso-ji'], rejected: [], notes: [],
      plan: fakePlanEnvelope,
    });
    vi.stubGlobal('fetch', f);
    const ops: ReplanOp[] = [
      { op: 'remove_place', leg_index: 0, day_index: 0, position: 0, attraction_ref: 'Q1234' },
    ];
    await replanTrip('trip-123', ops);
    const [url, init] = (f as unknown as MockFetch).mock.calls[0];
    expect(url).toMatch(/\/replan$/);
    expect(init.method).toBe('POST');
    const body = JSON.parse(String(init.body));
    expect(body.idempotency_key).toBe('trip-123');
    expect(body.ops).toHaveLength(1);
    expect(body.ops[0].op).toBe('remove_place');
  });

  // T2: user_id optional
  it('includes user_id when supplied; omits when not', async () => {
    const f = mockReplan(200, { outcome: 'plan_ready', applied: [], rejected: [], notes: [], plan: fakePlanEnvelope });
    vi.stubGlobal('fetch', f);
    await replanTrip('trip-abc', [], 'demo-mei');
    const bodyWith = JSON.parse(String((f as unknown as MockFetch).mock.calls[0][1].body));
    expect(bodyWith.user_id).toBe('demo-mei');

    vi.restoreAllMocks();
    const f2 = mockReplan(200, { outcome: 'plan_ready', applied: [], rejected: [], notes: [], plan: fakePlanEnvelope });
    vi.stubGlobal('fetch', f2);
    await replanTrip('trip-abc', []);
    const bodyWithout = JSON.parse(String((f2 as unknown as MockFetch).mock.calls[0][1].body));
    expect(bodyWithout.user_id).toBeUndefined();
  });

  // T3: plan_ready parses correctly
  it('plan_ready: parses applied[], rejected[]{op,reason}, notes[], full plan envelope', async () => {
    const resp: ReplanResponse = {
      outcome: 'plan_ready',
      idempotency_key: 'trip-123',
      applied: ['remove:leg0.day0.pos0:senso-ji'],
      rejected: [{ op: 'add_place', reason: 'not in pool' }],
      notes: ['Day 0 is now at pace-cap'],
      plan: fakePlanEnvelope,
    };
    vi.stubGlobal('fetch', mockReplan(200, resp));
    const r = await replanTrip('trip-123', []);
    expect(r.outcome).toBe('plan_ready');
    expect(r.applied).toEqual(['remove:leg0.day0.pos0:senso-ji']);
    expect(r.rejected[0]).toEqual({ op: 'add_place', reason: 'not in pool' });
    expect(r.notes[0]).toContain('pace-cap');
    expect(r.plan.day_plans).toBeDefined();
    expect(r.plan.payment_status).toBe('held');
  });

  // T4: noop — replanApplied is false; plan unchanged
  it('noop: outcome=noop, rejected has reason, plan is UNCHANGED, replanApplied=false', async () => {
    const originalPlan: NegotiateResult = { ...fakePlanEnvelope, package_total_cents: 280000 };
    vi.stubGlobal('fetch', mockReplan(200, {
      outcome: 'noop', applied: [], rejected: [{ op: 'remove_place', reason: 'ref not found' }],
      notes: [], plan: originalPlan,
    }));
    const r = await replanTrip('trip-123', []);
    expect(r.outcome).toBe('noop');
    expect(r.applied).toHaveLength(0);
    expect(r.rejected[0].reason).toBeTruthy();
    expect(r.plan.package_total_cents).toBe(280000);   // unchanged original
    expect(replanApplied(r)).toBe(false);
  });

  // T5: unknown_plan passthrough
  it('unknown_plan: outcome preserved, plan may be absent', async () => {
    vi.stubGlobal('fetch', mockReplan(200, {
      outcome: 'unknown_plan', applied: [], rejected: [], notes: [], plan: null,
    }));
    const r = await replanTrip('trip-ghost', []);
    expect(r.outcome).toBe('unknown_plan');
  });

  // T6: plan_locked passthrough with reason
  it('plan_locked: outcome preserved, reason present', async () => {
    vi.stubGlobal('fetch', mockReplan(200, {
      outcome: 'plan_locked', reason: 'cancelled', applied: [], rejected: [], notes: [],
      plan: fakePlanEnvelope,
    }));
    const r = await replanTrip('trip-cancelled', []);
    expect(r.outcome).toBe('plan_locked');
    expect(r.reason).toBe('cancelled');
  });

  // T7: cannot_satisfy + money_invariant_violation; plan unchanged; replanApplied false
  it('cannot_satisfy: reason=money_invariant_violation; replanApplied=false', async () => {
    vi.stubGlobal('fetch', mockReplan(200, {
      outcome: 'cannot_satisfy', reason: 'money_invariant_violation',
      applied: [], rejected: [], notes: [], plan: fakePlanEnvelope,
    }));
    const r = await replanTrip('trip-123', []);
    expect(r.outcome).toBe('cannot_satisfy');
    expect(r.reason).toBe('money_invariant_violation');
    expect(replanApplied(r)).toBe(false);
  });

  // T8: throws on non-402/403 HTTP errors
  it('throws on non-402/403 HTTP errors (e.g. 500)', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => ({
      ok: false, status: 500, text: async () => 'Internal Server Error',
    })));
    await expect(replanTrip('trip-x', [])).rejects.toThrow();
  });

  // T9: replanApplied contract
  it('replanApplied: true only for plan_ready with applied.length>0; false otherwise', () => {
    const baseResp: ReplanResponse = { outcome: 'plan_ready', applied: [], rejected: [], notes: [], plan: fakePlanEnvelope };
    expect(replanApplied({ ...baseResp, applied: ['x'] })).toBe(true);
    expect(replanApplied({ ...baseResp, applied: [] })).toBe(false);
    expect(replanApplied({ ...baseResp, outcome: 'noop', applied: ['x'] })).toBe(false);
    expect(replanApplied({ ...baseResp, outcome: 'cannot_satisfy', applied: ['x'] })).toBe(false);
  });

  // T10: isBookedEnvelope contract
  it('isBookedEnvelope: true for payment_status=charged or booking_ref present; false otherwise', () => {
    expect(isBookedEnvelope({ outcome: 'success', payment_status: 'charged' })).toBe(true);
    expect(isBookedEnvelope({ outcome: 'success', booking_ref: 'BK-1' })).toBe(true);
    expect(isBookedEnvelope({ outcome: 'plan_ready' })).toBe(false);
    expect(isBookedEnvelope(null)).toBe(false);
    expect(isBookedEnvelope(undefined)).toBe(false);
  });

  // T11: compile-guard — all 5 op variants type-check; set_meal_cuisine must NOT appear
  it('compile-guard: ReplanOp union covers exactly the 5 supported ops', () => {
    // This test is a compile-time guard; it also verifies the runtime shape at least parses.
    const ops: ReplanOp[] = [
      { op: 'remove_place', leg_index: 0, day_index: 0, position: 0, attraction_ref: 'Q1' },
      { op: 'add_place', leg_index: 0, day_index: 1, position: 0, attraction_ref: 'Q2', from: 'unscheduled' },
      { op: 'swap_place', leg_index: 0, day_index: 0, position: 0, remove_ref: 'Q1', add_ref: 'Q3' },
      { op: 'switch_variant', leg_index: 0, day_index: 0, variant: 'fair' },
      { op: 'reflow_from', leg_index: 0, from_day_index: 0 },
    ];
    expect(ops).toHaveLength(5);
    // TypeScript compile-check: 'set_meal_cuisine' is NOT a valid op — verified by tsc --noEmit
    const opNames = ops.map((o) => o.op);
    expect(opNames).not.toContain('set_meal_cuisine');
  });
});
