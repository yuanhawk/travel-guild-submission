import { test, expect } from '@playwright/test';

// trip-history.spec.ts — two features layered on the account system:
//
// 1) Same-account relogin restores the held trip. switchUser() snapshots the
//    outgoing session (priorSessionOnLogout) before clearing it; pickUser() checks
//    whether the newly-picked user_id matches that snapshot's user, and if so
//    re-fetches GET /trips/{key} to restore the held plan (App.svelte pickUser()).
//    A genuine switch to a DIFFERENT demo persona must NOT restore it — the new
//    persona lands on the empty guide-panel, same as a first-ever login.
//
// 2) "Show my trips" — a chat-intent (MY_TRIPS_RE) that calls GET /trips?user_id=
//    (listMyTrips) and renders a summary as a 'note' chat message, then fetches
//    the most recent trip's full envelope via GET /trips/{key} so the itinerary
//    view populates too. Honest empty case: "No previous trips found."

const USERS = {
  demo_users: [
    { user_id: 'demo-mei', display_name: 'Mei', persona: 'foodie', nationality: 'SG', home_currency: 'SGD', wallet_balance_cents: 500000 },
    { user_id: 'demo-alex', display_name: 'Alex', persona: 'hiker', nationality: 'US', home_currency: 'USD', wallet_balance_cents: 500000 },
  ],
};

const PLAN_READY = {
  outcome: 'plan_ready',
  payment_status: 'held',
  booking_ref: null,
  idempotency_key: 'trip-th-1',
  package_total_with_fees_cents: 220000,
  total_budget_cents: 400000,
  wallet: { balance_cents: 500000, held_cents: 220000, debited: false },
  legs: [{ leg_id: 'leg-0', city: 'Tokyo', checkin: '2026-10-01', checkout: '2026-10-05' }],
  day_plans: [{
    leg_id: 'leg-0', city: 'Tokyo', num_days: 4,
    days: [{ day_index: 0, bad_weather: false, attractions: [], meals: {} }],
  }],
  risk_signals: { per_leg: [] },
};

async function mockCommon(page) {
  await page.route('**/session', (r) =>
    r.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(USERS) }));
  await page.route('**/emergencies', (r) =>
    r.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ status: 'ok', countries: [] }) }));
  await page.route(/tile|openstreetmap|basemaps|demotiles/, (r) => r.abort());
}

// ── 1) Same-account relogin restores the held trip ──────────────────────────

test('same demo user logs out then back in → held trip is restored', async ({ page }) => {
  await page.addInitScript(() => localStorage.clear());
  await mockCommon(page);
  await page.route('**/negotiate_text', (r) =>
    r.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(PLAN_READY) }));
  // GET /trips/{key} — used both by pickUser()'s restore check.
  await page.route('**/trips/**', (r) =>
    r.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(PLAN_READY) }));

  await page.goto('/');
  await expect(page.getByTestId('session-picker')).toBeVisible();
  await page.getByTestId('user-demo-mei').click();

  // Plan a trip → held plan established for demo-mei.
  await page.getByTestId('chat-input').fill('4 days in tokyo');
  await page.getByRole('button', { name: 'Send' }).click();
  await expect(page.getByTestId('status-banner')).toContainText('Plan ready', { timeout: 8000 });

  // Log out — back to the picker.
  await page.getByTestId('switch-user').click();
  await expect(page.getByTestId('session-picker')).toBeVisible();

  // Log back in as the SAME user (demo-mei).
  await page.getByTestId('user-demo-mei').click();

  // The previously-held trip is restored — status banner + itinerary, not the empty state.
  await expect(page.getByTestId('status-banner')).toContainText('Plan ready', { timeout: 8000 });
  await expect(page.getByTestId('itinerary')).toContainText('Tokyo');
  await expect(page.getByTestId('guide-panel')).toHaveCount(0);

  // Confirmation note mentions the restore. Plan is active → chat is in floating mode,
  // but chat-msgs stays in the DOM (CSS-driven show/hide) so it's queryable either way.
  await expect(page.getByTestId('chat-msgs')).toContainText('Welcome back');
});

test('logging in as a DIFFERENT demo user after logout does NOT restore the trip', async ({ page }) => {
  await page.addInitScript(() => localStorage.clear());
  await mockCommon(page);
  await page.route('**/negotiate_text', (r) =>
    r.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(PLAN_READY) }));
  await page.route('**/trips/**', (r) =>
    r.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(PLAN_READY) }));

  await page.goto('/');
  await page.getByTestId('user-demo-mei').click();

  await page.getByTestId('chat-input').fill('4 days in tokyo');
  await page.getByRole('button', { name: 'Send' }).click();
  await expect(page.getByTestId('status-banner')).toContainText('Plan ready', { timeout: 8000 });

  await page.getByTestId('switch-user').click();
  await expect(page.getByTestId('session-picker')).toBeVisible();

  // Log in as a DIFFERENT persona this time.
  await page.getByTestId('user-demo-alex').click();

  // No restored trip — empty guide-panel state, no banner, no itinerary.
  await expect(page.getByTestId('status-banner')).toHaveCount(0);
  await expect(page.getByTestId('itinerary')).toHaveCount(0);
  await expect(page.getByTestId('guide-panel')).toBeVisible();
  await expect(page.getByTestId('chat-msgs')).not.toContainText('Welcome back');
});

// ── 2) "Show my trips" retrieval ─────────────────────────────────────────────

const TRIPS_SUMMARY = [
  {
    idempotency_key: 'trip-hist-3', user_id: 'demo-mei', status: 'held', outcome: 'plan_ready',
    booking_ref: null, package_total_cents: 210000, created_at: '2026-06-20T10:00:00Z', confirmed_at: null,
  },
  {
    idempotency_key: 'trip-hist-2', user_id: 'demo-mei', status: 'booked', outcome: 'success',
    booking_ref: 'BK-HIST-2', package_total_cents: 340000, created_at: '2026-05-01T10:00:00Z', confirmed_at: '2026-05-01T11:00:00Z',
  },
  {
    idempotency_key: 'trip-hist-1', user_id: 'demo-mei', status: 'cancelled', outcome: 'cancelled',
    booking_ref: null, package_total_cents: 95000, created_at: '2026-03-15T10:00:00Z', confirmed_at: null,
  },
];

function preseedDemoMei(page) {
  return page.addInitScript(() => {
    localStorage.setItem('tg_session', JSON.stringify({
      user: { user_id: 'demo-mei', display_name: 'Mei', persona: 'foodie', nationality: 'SG', home_currency: 'SGD', wallet_balance_cents: 500000 },
      guest: true,
      activePlan: null,
      savedAt: Date.now(),
    }));
  });
}

test('"show my trips" → trip summary in chat + most recent trip\'s itinerary populates', async ({ page }) => {
  await preseedDemoMei(page);
  await page.route('**/emergencies', (r) =>
    r.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ status: 'ok', countries: [] }) }));
  await page.route(/tile|openstreetmap|basemaps|demotiles/, (r) => r.abort());
  await page.route('**/trips?user_id=**', (r) =>
    r.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ trips: TRIPS_SUMMARY }) }));
  // Most recent trip (trips[0]) is trip-hist-3 → its full envelope is fetched via GET /trips/{key}.
  await page.route('**/trips/**', (r) =>
    r.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(PLAN_READY) }));

  await page.goto('/');
  await expect(page.getByTestId('chat-input')).toBeVisible();

  await page.getByTestId('chat-input').fill('what did I book');
  await page.getByRole('button', { name: 'Send' }).click();

  // The trip summary lands in chat as a note ("Found N previous trips: ...").
  const msgs = page.getByTestId('chat-msgs');
  await expect(msgs).toContainText('Found 3 previous trips', { timeout: 5000 });
  await expect(msgs).toContainText('BK-HIST-2');
  await expect(msgs).toContainText('$2,100.00');   // centsToUsd(210000) for the held trip
  await expect(msgs).toContainText('$3,400.00');   // centsToUsd(340000) for the booked trip

  // The most recent trip's itinerary also populates from GET /trips/{key}.
  await expect(page.getByTestId('itinerary')).toContainText('Tokyo', { timeout: 5000 });
  await expect(msgs).toContainText('Showing your most recent trip above.');
});

test('"show my trips" with no history → honest "No previous trips found" message', async ({ page }) => {
  await preseedDemoMei(page);
  await page.route('**/emergencies', (r) =>
    r.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ status: 'ok', countries: [] }) }));
  await page.route(/tile|openstreetmap|basemaps|demotiles/, (r) => r.abort());
  await page.route('**/trips?user_id=**', (r) =>
    r.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ trips: [] }) }));

  await page.goto('/');
  await page.getByTestId('chat-input').fill('show my trips');
  await page.getByRole('button', { name: 'Send' }).click();

  const msgs = page.getByTestId('chat-msgs');
  await expect(msgs).toContainText('No previous trips found for Mei.', { timeout: 5000 });

  // No itinerary was populated — the guide-panel empty state remains.
  await expect(page.getByTestId('itinerary')).toHaveCount(0);
  await expect(page.getByTestId('guide-panel')).toBeVisible();
});
