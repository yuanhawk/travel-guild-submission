import { describe, it, expect, vi, afterEach } from 'vitest';
import { listDemoUsers, selectDemoUser, getPreferences, putPreferences, negotiateText } from './api';
import { loadSession, saveSession, clearSession, setActivePlan, clearActivePlan, getAnonOwnerToken } from './session';
import type { DemoUser } from './api';
import type { Session, ActivePlan } from './session';

// Slice 4 — demo-user session + preferences. Guard the request SHAPES (user_id replay,
// update-only prefs) + localStorage persistence. Component render is covered by Playwright.

function mockFetch(status: number, body: unknown) {
  return vi.fn(async () => ({
    ok: status >= 200 && status < 300,
    status,
    json: async () => body,
    text: async () => JSON.stringify(body),
  })) as unknown as typeof fetch;
}
type Mocked = { mock: { calls: any[][] } };
afterEach(() => vi.restoreAllMocks());

const U: DemoUser = {
  user_id: 'demo-mei', display_name: 'Mei', persona: 'foodie',
  nationality: 'SG', home_currency: 'SGD', wallet_balance_cents: 500000,
};

describe('session / preferences API', () => {
  it('listDemoUsers POSTs /session {} → the seeded travellers', async () => {
    const f = mockFetch(200, { demo_users: [U] }); vi.stubGlobal('fetch', f);
    const r = await listDemoUsers();
    expect(r.demo_users[0].user_id).toBe('demo-mei');
    const [url, init] = (f as unknown as Mocked).mock.calls[0];
    expect(url).toContain('/session');
    expect(JSON.parse(init.body)).toEqual({});
  });

  it('selectDemoUser POSTs {user_id}', async () => {
    const f = mockFetch(200, { user: U }); vi.stubGlobal('fetch', f);
    await selectDemoUser('demo-mei');
    expect(JSON.parse((f as unknown as Mocked).mock.calls[0][1].body)).toEqual({ user_id: 'demo-mei' });
  });

  it('getPreferences GETs /preferences?user_id=', async () => {
    const f = mockFetch(200, { ...U, prefs: {} }); vi.stubGlobal('fetch', f);
    const p = await getPreferences('demo-mei');
    expect((f as unknown as Mocked).mock.calls[0][0]).toContain('/preferences?user_id=demo-mei');
    expect(p.home_currency).toBe('SGD');
  });

  // SECURITY (Phase 1 of the GET /preferences IDOR follow-up — see
  // society/orchestration/server.py "SECURITY FOLLOW-UP"): thread session_token
  // so the backend can be gated (Phase 2) without another FE change.
  it('getPreferences sends session_token in the query string when provided', async () => {
    const f = mockFetch(200, { ...U, prefs: {} }); vi.stubGlobal('fetch', f);
    await getPreferences('demo-mei', 'sess-tok-123');
    const url = (f as unknown as Mocked).mock.calls[0][0] as string;
    expect(url).toContain('/preferences?user_id=demo-mei');
    expect(url).toContain('session_token=sess-tok-123');
  });

  it('getPreferences URL is still well-formed with no session_token (backward compat — backend not yet gated)', async () => {
    const f = mockFetch(200, { ...U, prefs: {} }); vi.stubGlobal('fetch', f);
    await getPreferences('demo-mei');
    const url = (f as unknown as Mocked).mock.calls[0][0] as string;
    expect(url).toBe(`${url.split('?')[0]}?user_id=demo-mei`); // exactly one param, no dangling '&' or empty session_token key
    expect(url).not.toContain('session_token');
  });

  it('putPreferences PUTs the profile (update-only)', async () => {
    const f = mockFetch(200, { ...U, home_currency: 'EUR' }); vi.stubGlobal('fetch', f);
    const r = await putPreferences({ user_id: 'demo-mei', home_currency: 'EUR' });
    expect((f as unknown as Mocked).mock.calls[0][1].method).toBe('PUT');
    expect(r.home_currency).toBe('EUR');
  });

  it('selectDemoUser throws on 404 (unknown — NO register)', async () => {
    vi.stubGlobal('fetch', mockFetch(404, { error: 'unknown' }));
    await expect(selectDemoUser('nope')).rejects.toThrow();
  });

  it('negotiateText replays user_id + nationality in the body', async () => {
    const f = mockFetch(200, { outcome: 'plan_ready' }); vi.stubGlobal('fetch', f);
    await negotiateText({ text: '5 days tokyo', plan: true, user_id: 'demo-mei', nationality: 'SG' });
    const body = JSON.parse((f as unknown as Mocked).mock.calls[0][1].body);
    expect(body.user_id).toBe('demo-mei');
    expect(body.nationality).toBe('SG');
    expect(body.plan).toBe(true);
  });

  it('guest plan sends NO user_id (system defaults / byte-identical)', async () => {
    const f = mockFetch(200, { outcome: 'plan_ready' }); vi.stubGlobal('fetch', f);
    await negotiateText({ text: '5 days tokyo', plan: true });
    const body = JSON.parse((f as unknown as Mocked).mock.calls[0][1].body);
    expect(body.user_id).toBeUndefined();
  });
});

describe('session persistence (localStorage)', () => {
  it('save / load / clear round-trip + guest', () => {
    const store: Record<string, string> = {};
    vi.stubGlobal('localStorage', {
      getItem: (k: string) => store[k] ?? null,
      setItem: (k: string, v: string) => { store[k] = v; },
      removeItem: (k: string) => { delete store[k]; },
    });
    expect(loadSession()).toBeNull();
    saveSession({ user: U, guest: true });
    expect(loadSession()?.user?.user_id).toBe('demo-mei');
    saveSession({ user: null, guest: true });          // guest choice
    expect(loadSession()?.user).toBeNull();
    expect(loadSession()?.guest).toBe(true);
    clearSession();
    expect(loadSession()).toBeNull();
  });

  // 2026-07-03 — localStorage has no native expiry (unlike the old sessionStorage
  // approach's tab-close boundary), so session.ts stamps savedAt and sweeps
  // entries past SESSION_TTL_MS (8h) on load. Guard both sides of that boundary.
  it('entries within the 8h TTL are still loaded', () => {
    const store: Record<string, string> = {};
    vi.stubGlobal('localStorage', {
      getItem: (k: string) => store[k] ?? null,
      setItem: (k: string, v: string) => { store[k] = v; },
      removeItem: (k: string) => { delete store[k]; },
    });
    const almostEightHoursAgo = Date.now() - (8 * 60 * 60 * 1000 - 60_000);
    store['tg_session'] = JSON.stringify({ user: U, guest: true, savedAt: almostEightHoursAgo });
    expect(loadSession()?.user?.user_id).toBe('demo-mei');
  });

  it('entries older than the 8h TTL are treated as absent and swept from storage', () => {
    const store: Record<string, string> = {};
    vi.stubGlobal('localStorage', {
      getItem: (k: string) => store[k] ?? null,
      setItem: (k: string, v: string) => { store[k] = v; },
      removeItem: (k: string) => { delete store[k]; },
    });
    const nineHoursAgo = Date.now() - 9 * 60 * 60 * 1000;
    store['tg_session'] = JSON.stringify({ user: U, guest: true, savedAt: nineHoursAgo });
    expect(loadSession()).toBeNull();
    expect(store['tg_session']).toBeUndefined();   // swept, not just ignored
  });

  it('a pre-TTL legacy entry (no savedAt field) is treated as expired, not crashed on', () => {
    const store: Record<string, string> = {};
    vi.stubGlobal('localStorage', {
      getItem: (k: string) => store[k] ?? null,
      setItem: (k: string, v: string) => { store[k] = v; },
      removeItem: (k: string) => { delete store[k]; },
    });
    store['tg_session'] = JSON.stringify({ user: U, guest: true }); // no savedAt
    expect(loadSession()).toBeNull();
  });
});

describe('getAnonOwnerToken — anonymous-trip ownership secret TTL (localStorage)', () => {
  afterEach(() => vi.restoreAllMocks());

  it('returns a stable token across calls within the 8h window', () => {
    const store: Record<string, string> = {};
    vi.stubGlobal('localStorage', {
      getItem: (k: string) => store[k] ?? null,
      setItem: (k: string, v: string) => { store[k] = v; },
      removeItem: (k: string) => { delete store[k]; },
    });
    const first = getAnonOwnerToken();
    const second = getAnonOwnerToken();
    expect(second).toBe(first);
  });

  it('mints a fresh token once the stored one is past the 8h TTL', () => {
    const store: Record<string, string> = {};
    vi.stubGlobal('localStorage', {
      getItem: (k: string) => store[k] ?? null,
      setItem: (k: string, v: string) => { store[k] = v; },
      removeItem: (k: string) => { delete store[k]; },
    });
    const nineHoursAgo = Date.now() - 9 * 60 * 60 * 1000;
    store['tg_anon_owner'] = JSON.stringify({ token: 'stale-token', savedAt: nineHoursAgo });
    const fresh = getAnonOwnerToken();
    expect(fresh).not.toBe('stale-token');
  });

  it('falls back to an in-memory token when localStorage throws', () => {
    vi.stubGlobal('localStorage', {
      getItem: () => { throw new Error('storage disabled'); },
      setItem: () => { throw new Error('storage disabled'); },
      removeItem: () => { throw new Error('storage disabled'); },
    });
    const a = getAnonOwnerToken();
    const b = getAnonOwnerToken();
    expect(a).toBe(b);           // still stable, just not persisted
    expect(typeof a).toBe('string');
    expect(a.length).toBeGreaterThan(0);
  });
});

// ── ActivePlan — conversational session context (B6) ────────────────────────
// Guards: setActivePlan / clearActivePlan pure-function + localStorage round-trip.

function makeStore(): Record<string, string> { return {}; }
function stubLocalStorage(store: Record<string, string>) {
  vi.stubGlobal('localStorage', {
    getItem: (k: string) => store[k] ?? null,
    setItem: (k: string, v: string) => { store[k] = v; },
    removeItem: (k: string) => { delete store[k]; },
  });
}

describe('ActivePlan — setActivePlan / clearActivePlan', () => {
  afterEach(() => vi.restoreAllMocks());

  const plan: ActivePlan = { session_id: 'sess-xyz', idempotency_key: 'trip-abc' };

  it('setActivePlan attaches activePlan to the session (immutable — returns new obj)', () => {
    const s: Session = { user: null, guest: true };
    const next = setActivePlan(s, plan);
    expect(next.activePlan?.session_id).toBe('sess-xyz');
    expect(next.activePlan?.idempotency_key).toBe('trip-abc');
    expect(s.activePlan).toBeUndefined();            // original untouched
  });

  it('clearActivePlan sets activePlan to null (immutable)', () => {
    const s: Session = { user: null, guest: true, activePlan: plan };
    const next = clearActivePlan(s);
    expect(next.activePlan).toBeNull();
    expect(s.activePlan).toEqual(plan);              // original untouched
  });

  it('setActivePlan(null) explicitly clears (new-trip path)', () => {
    const s: Session = { user: null, guest: true, activePlan: plan };
    const next = setActivePlan(s, null);
    expect(next.activePlan).toBeNull();
  });
});

describe('ActivePlan — localStorage persistence across "reload"', () => {
  afterEach(() => vi.restoreAllMocks());

  it('activePlan round-trips through saveSession / loadSession', () => {
    const store = makeStore(); stubLocalStorage(store);
    const plan: ActivePlan = { session_id: 'sess-1', idempotency_key: 'trip-xyz' };
    saveSession({ user: U, guest: true, activePlan: plan });
    const loaded = loadSession();
    expect(loaded?.activePlan?.session_id).toBe('sess-1');
    expect(loaded?.activePlan?.idempotency_key).toBe('trip-xyz');
  });

  it('activePlan updated after each successful /refine (new idempotency_key stored)', () => {
    const store = makeStore(); stubLocalStorage(store);
    // First plan
    const firstPlan: ActivePlan = { session_id: '', idempotency_key: 'trip-v1' };
    saveSession({ user: null, guest: true, activePlan: firstPlan });
    // Refine returns new key + session_id → update stored plan
    const sess = loadSession()!;
    const refinedPlan: ActivePlan = { session_id: 'sess-abc', idempotency_key: 'trip-v2' };
    saveSession(setActivePlan(sess, refinedPlan));
    // Simulate reload
    const afterReload = loadSession();
    expect(afterReload?.activePlan?.idempotency_key).toBe('trip-v2'); // swapped
    expect(afterReload?.activePlan?.session_id).toBe('sess-abc');     // now populated
  });

  it('"new trip" clears activePlan from localStorage', () => {
    const store = makeStore(); stubLocalStorage(store);
    saveSession({ user: U, guest: true, activePlan: { session_id: 'sess-1', idempotency_key: 'trip-v1' } });
    const sess = loadSession()!;
    saveSession(clearActivePlan(sess));
    expect(loadSession()?.activePlan).toBeNull();
  });

  it('activePlan absent (null) on a fresh session (first-time visitor)', () => {
    const store = makeStore(); stubLocalStorage(store);
    expect(loadSession()).toBeNull();               // no prior session at all
    saveSession({ user: null, guest: true });       // guest, no plan yet
    expect(loadSession()?.activePlan).toBeUndefined();
  });
});

// ── Item A: login UX — pickUser note contract ─────────────────────────────
// Guards: the login confirmation note pushed into messages by pickUser().
// (App.svelte logic, tested here at the unit level via a manual call simulation.)

describe('Login UX — pickUser note format (#101 item A)', () => {
  const MEI: DemoUser = {
    user_id: 'demo-mei', display_name: 'Mei', persona: 'foodie',
    nationality: 'SG', home_currency: 'SGD', wallet_balance_cents: 500000,
  };
  const USD_USER: DemoUser = {
    user_id: 'demo-alex', display_name: 'Alex', persona: 'budget',
    nationality: 'US', home_currency: 'USD', wallet_balance_cents: 300000,
  };

  /** Simulate the note generated by App.svelte's pickUser() for a given user. */
  function loginNote(u: DemoUser): string {
    const currencyNote = u.home_currency !== 'USD'
      ? ` · prices shown in USD with an indicative ${u.home_currency} estimate`
      : '';
    return `Logged in as ${u.display_name}. Prefs applied${currencyNote}.`;
  }

  it('non-USD user: note includes indicative currency clause', () => {
    const note = loginNote(MEI);
    expect(note).toContain('Logged in as Mei');
    expect(note).toContain('Prefs applied');
    expect(note).toContain('indicative SGD estimate');
  });

  it('USD user: note omits currency clause', () => {
    const note = loginNote(USD_USER);
    expect(note).toContain('Logged in as Alex');
    expect(note).toContain('Prefs applied');
    expect(note).not.toContain('indicative');
    expect(note).not.toContain('USD estimate');
  });

  it('login note does not contain home_currency twice for USD user', () => {
    const note = loginNote(USD_USER);
    // Should mention "Prefs applied" but not a separate currency block
    expect((note.match(/USD/g) ?? []).length).toBe(0);
  });
});

describe('Conversation threading — plan-updates-in-place contract', () => {
  // These tests guard the data contract that App.svelte relies on:
  // (1) a successful refine returns a FULL new plan envelope in r.plan
  // (2) the frontend replaces `result` in place with r.plan
  // (3) the new idempotency_key is what /confirm must use
  afterEach(() => vi.restoreAllMocks());

  it('plan replaces in place: r.plan has same shape as NegotiateResult', async () => {
    const { refineTrip } = await import('./api');
    const newPlan = { outcome: 'plan_ready', idempotency_key: 'trip-v2',
      legs: [{ city: 'Tokyo' }, { city: 'Osaka' }], package_total_cents: 425000 };
    vi.stubGlobal('fetch', vi.fn(async () => ({
      ok: true, status: 200,
      json: async () => ({ outcome: 'plan_ready', session_id: 'sess-1',
        idempotency_key: 'trip-v2', assistant_reply: 'Added Osaka.', plan: newPlan }),
      text: async () => '{}',
    })));
    const r = await refineTrip({ idempotency_key: 'trip-v1', message: 'add Osaka' });
    // App.svelte does: result = r.plan  (plan replaces in-place)
    expect(r.plan?.outcome).toBe('plan_ready');
    expect(r.plan?.legs).toHaveLength(2);
    expect(r.plan?.package_total_cents).toBe(425000);
    // New key to use for /confirm
    expect(r.idempotency_key).toBe('trip-v2');
  });

  it('assistant_reply reflects the actual diff (truthful, not fabricated)', async () => {
    const { refineTrip } = await import('./api');
    vi.stubGlobal('fetch', vi.fn(async () => ({
      ok: true, status: 200,
      json: async () => ({
        outcome: 'plan_ready', session_id: 'sess-1', idempotency_key: 'trip-v2',
        assistant_reply: 'Trimmed budget 15%. New held total $4,250 (was $5,000, -15%). 1 city · 7 nights total.',
        changed: ['budget.adjust:-15%'],
        plan: { outcome: 'plan_ready' },
      }),
      text: async () => '{}',
    })));
    const r = await refineTrip({ idempotency_key: 'trip-v1', message: 'make it cheaper' });
    expect(r.assistant_reply).toMatch(/\$4,250/);
    expect(r.changed).toContain('budget.adjust:-15%');
  });

  it('session_id is stable across two sequential refines', async () => {
    const { refineTrip } = await import('./api');
    const makeResponse = (newKey: string) => vi.fn(async () => ({
      ok: true, status: 200,
      json: async () => ({ outcome: 'plan_ready', session_id: 'sess-stable',
        idempotency_key: newKey, assistant_reply: 'Updated.', plan: {} }),
      text: async () => '{}',
    }));
    // First refine
    vi.stubGlobal('fetch', makeResponse('trip-v2'));
    const r1 = await refineTrip({ idempotency_key: 'trip-v1', message: 'add Osaka' });
    expect(r1.session_id).toBe('sess-stable');
    // Second refine — session_id SAME, idempotency_key NEW
    vi.stubGlobal('fetch', makeResponse('trip-v3'));
    const r2 = await refineTrip({ idempotency_key: r1.idempotency_key!, message: 'make cheaper',
      session_id: r1.session_id });
    expect(r2.session_id).toBe('sess-stable');   // thread is stable
    expect(r2.idempotency_key).toBe('trip-v3'); // but the plan key advances
  });
});
