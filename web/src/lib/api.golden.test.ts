/**
 * api.golden.test.ts — #167 golden-REQUEST contract (frontend half).
 *
 * For every enumerated api.ts / aftercare.ts HTTP call, invoke the REAL function
 * with fetch mocked, capture the ACTUAL constructed request, and deep-equal it
 * against the SHARED golden_requests/<fn>.json fixture that the backend
 * (society/tests/test_golden_request_contract.py) also pins. Drift on either
 * side breaks a test.
 *
 * Fixtures here (src/lib/__fixtures__/golden_requests/) are a git-tracked COPY
 * synced from society/tests/fixtures/golden_requests/ (this repo's root -- source of
 * truth — the two repos are separate, so there is no cross-repo import). Keep
 * both copies byte-identical when adding/editing a fixture.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';

// Deterministic auth tokens so captured bodies deep-equal the static fixtures.
vi.mock('./session', () => ({
  getAnonOwnerToken: () => 'gr-owner-token',
  authFields: () => ({ session_token: 'gr-session-token', owner_token: 'gr-owner-token' }),
}));

import * as api from './api';
import { aftercareCheck } from './aftercare';
import { API_BASE } from './api';

// Shared fixtures (synced from society/tests/fixtures/golden_requests, this repo's root).
import negotiate_text from './__fixtures__/golden_requests/negotiate_text.json';
import confirm from './__fixtures__/golden_requests/confirm.json';
import cancel_trip from './__fixtures__/golden_requests/cancel_trip.json';
import refine_trip from './__fixtures__/golden_requests/refine_trip.json';
import replan_trip from './__fixtures__/golden_requests/replan_trip.json';
import place_card from './__fixtures__/golden_requests/place_card.json';
import aftercare_check from './__fixtures__/golden_requests/aftercare_check.json';
import login_session from './__fixtures__/golden_requests/login_session.json';
import list_demo_users from './__fixtures__/golden_requests/list_demo_users.json';
import select_demo_user from './__fixtures__/golden_requests/select_demo_user.json';
import put_preferences from './__fixtures__/golden_requests/put_preferences.json';
import list_my_trips from './__fixtures__/golden_requests/list_my_trips.json';
import get_preferences from './__fixtures__/golden_requests/get_preferences.json';
import get_telegram_link_token from './__fixtures__/golden_requests/get_telegram_link_token.json';
import get_emergencies from './__fixtures__/golden_requests/get_emergencies.json';
import get_trip from './__fixtures__/golden_requests/get_trip.json';
import place_photo_url from './__fixtures__/golden_requests/place_photo_url.json';

// All 17 fixtures — regression lock mirroring the backend's
// test_all_17_endpoints_have_a_fixture (the enumerated FE HTTP surface can't
// silently grow without a fixture being added on both sides).
const ALL_FIXTURES = [
  negotiate_text, confirm, cancel_trip, refine_trip, replan_trip, place_card,
  aftercare_check, login_session, list_demo_users, select_demo_user,
  put_preferences, list_my_trips, get_preferences, get_telegram_link_token,
  get_emergencies, get_trip, place_photo_url,
];

type MockedFetch = { mock: { calls: [string, RequestInit | undefined][] } };
let f: ReturnType<typeof vi.fn>;

function mockFetch(body: unknown = { outcome: 'plan_ready' }, status = 200) {
  f = vi.fn(async () => ({
    ok: status >= 200 && status < 300,
    status,
    json: async () => body,
    text: async () => JSON.stringify(body),
  }));
  vi.stubGlobal('fetch', f as unknown as typeof fetch);
}
const capturedBody = () => JSON.parse(String((f as unknown as MockedFetch).mock.calls[0][1]!.body));
// Fixed dummy base (NOT API_BASE): API_BASE is '' in this repo's .env.local (relative-URL
// mode), so the actually-fetched URL string may itself be relative (e.g. "/trips?user_id=…").
// A fixed absolute base just lets URL parse pathname/search — it plays no role in the
// contract being asserted (path + query only).
function capturedQuery(): { path: string; query: Record<string, string> } {
  const url = new URL((f as unknown as MockedFetch).mock.calls[0][0], 'http://golden-fixture.invalid');
  return { path: url.pathname, query: Object.fromEntries(url.searchParams.entries()) };
}

beforeEach(() => { api.__clearPlaceCardCache(); });
afterEach(() => { vi.unstubAllGlobals(); vi.restoreAllMocks(); });

describe('golden-request fixture inventory', () => {
  it('pins exactly 17 endpoints (add a fixture on BOTH sides to grow this)', () => {
    expect(ALL_FIXTURES).toHaveLength(17);
  });
});

// ── POST-body endpoints: captured JSON body deep-equals fixture.body ──────────
describe('golden request bodies (POST/PUT)', () => {
  it('negotiateText → /negotiate_text', async () => {
    mockFetch();
    const { owner_token, session_token, ...input } = negotiate_text.body as Record<string, unknown>;
    void owner_token; void session_token; // FE injects both via authFields() — not part of the caller's input
    await api.negotiateText(input as unknown as api.NegotiateBody);
    expect(capturedQuery().path).toBe(negotiate_text.path);
    expect(capturedBody()).toEqual(negotiate_text.body);
  });

  it('confirmPlan → /confirm', async () => {
    mockFetch();
    await api.confirmPlan(confirm.body.idempotency_key, confirm.body.user_id);
    expect(capturedBody()).toEqual(confirm.body);
  });

  it('cancelTrip → /cancel', async () => {
    mockFetch({ outcome: 'cancelled' });
    await api.cancelTrip(cancel_trip.body.idempotency_key, cancel_trip.body.user_id);
    expect(capturedBody()).toEqual(cancel_trip.body);
  });

  it('refineTrip → /refine', async () => {
    mockFetch();
    const { session_token, owner_token, ...input } = refine_trip.body as Record<string, unknown>;
    void session_token; void owner_token; // FE injects both via authFields()
    await api.refineTrip(input as unknown as api.RefineBody);
    expect(capturedBody()).toEqual(refine_trip.body);
  });

  it('replanTrip → /replan', async () => {
    mockFetch({ outcome: 'plan_ready', applied: [], rejected: [], notes: [], plan: {} });
    await api.replanTrip(
      replan_trip.body.idempotency_key,
      replan_trip.body.ops as unknown as api.ReplanOp[],
      replan_trip.body.user_id,
    );
    expect(capturedBody()).toEqual(replan_trip.body);
  });

  it('placeCard → /place_card (pins {place} not {name})', async () => {
    mockFetch({ status: 'unavailable' });
    await api.placeCard(place_card.body.place, place_card.body.city);
    expect(capturedBody()).toEqual(place_card.body); // { mode:'detail', place, city }
  });

  it('aftercareCheck → /aftercare/check', async () => {
    mockFetch({ outcome: 'ok', alerts: [] });
    await aftercareCheck(aftercare_check.body.idempotency_key, aftercare_check.body.user_id);
    expect(capturedBody()).toEqual(aftercare_check.body);
  });

  it('loginSession → /session/login', async () => {
    mockFetch({ session_token: 'x' });
    await api.loginSession(login_session.body.user_id);
    expect(capturedBody()).toEqual(login_session.body);
  });

  it('listDemoUsers → /session (empty body)', async () => {
    mockFetch({ demo_users: [] });
    await api.listDemoUsers();
    expect(capturedBody()).toEqual(list_demo_users.body); // {}
  });

  it('selectDemoUser → /session', async () => {
    mockFetch({ user: {} });
    await api.selectDemoUser(select_demo_user.body.user_id);
    expect(capturedBody()).toEqual(select_demo_user.body);
  });

  it('putPreferences → PUT /preferences', async () => {
    mockFetch({ user_id: 'demo-lena', updated: true });
    const { session_token, ...p } = put_preferences.body as Record<string, unknown> & { user_id: string };
    await api.putPreferences(p as unknown as Partial<api.Preferences> & { user_id: string }, session_token as string);
    expect((f as unknown as MockedFetch).mock.calls[0][1]!.method).toBe('PUT');
    expect(capturedBody()).toEqual(put_preferences.body);
  });
});

// ── GET query/path endpoints: captured URL deep-equals fixture query/path ─────
describe('golden request query params (GET)', () => {
  it('listMyTrips → /trips?user_id=', async () => {
    mockFetch({ trips: [] });
    await api.listMyTrips(list_my_trips.query.user_id);
    const c = capturedQuery();
    expect(c.path).toBe(list_my_trips.path);
    expect(c.query).toEqual(list_my_trips.query);
  });

  it('getPreferences → /preferences?user_id=', async () => {
    mockFetch({ user_id: 'demo-lena' });
    await api.getPreferences(get_preferences.query.user_id);
    expect(capturedQuery().query).toEqual(get_preferences.query);
  });

  it('getTelegramLinkToken → /telegram/link?user_id=&session_token=', async () => {
    mockFetch({ available: false });
    await api.getTelegramLinkToken(
      get_telegram_link_token.query.user_id,
      get_telegram_link_token.query.session_token,
    );
    expect(capturedQuery().query).toEqual(get_telegram_link_token.query);
  });

  it('getEmergencies → /emergencies (no params)', async () => {
    mockFetch({ status: 'ok', countries: [] });
    await api.getEmergencies();
    const c = capturedQuery();
    expect(c.path).toBe(get_emergencies.path);
    expect(c.query).toEqual({}); // fixture query is {}
  });

  it('getTrip → /trips/{key}?owner_token=&session_token=', async () => {
    mockFetch({ outcome: 'plan_ready' });
    await api.getTrip(get_trip.path_params.idempotency_key);
    const c = capturedQuery();
    expect(c.path).toBe(`/trips/${encodeURIComponent(get_trip.path_params.idempotency_key)}`);
    expect(c.query).toEqual(get_trip.query); // { owner_token, session_token }
  });
});

// ── URL-builder only: no fetch is issued ─────────────────────────────────────
describe('golden request URL construction (no fetch)', () => {
  it('placePhotoUrl builds /place_photo?ref=<encoded> and issues no request', () => {
    mockFetch();
    const url = api.placePhotoUrl(place_photo_url.query.ref);
    expect(url).toBe(`${API_BASE}/place_photo?ref=${encodeURIComponent(place_photo_url.query.ref)}`);
    expect((f as unknown as MockedFetch).mock.calls).toHaveLength(0); // proves no body/fetch
  });
});
