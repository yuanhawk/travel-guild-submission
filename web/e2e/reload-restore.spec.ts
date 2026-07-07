import { test, expect } from '@playwright/test';

// reload-restore.spec.ts — Page reload restores the active itinerary from GET /trips/{key}.
//
// On mount, App.svelte reads loadSession() from localStorage. If a session exists with
// an activePlan.idempotency_key, it calls getTrip(key) → GET /trips/{key}. On 200,
// result is set and the itinerary renders. On 404, activePlan is cleared gracefully
// and the app shows the empty placeholder.
//
// We preseed localStorage via addInitScript (runs before page scripts) so the app
// behaves like a returning visitor with a persisted plan, without ever going through
// the plan → confirm flow.

const STORED_PLAN = {
  outcome: 'plan_ready',
  payment_status: 'held',
  booking_ref: null,
  idempotency_key: 'trip-rr-1',
  package_total_with_fees_cents: 150000,
  total_budget_cents: 300000,
  wallet: { balance_cents: 500000, held_cents: 150000, debited: false },
  legs: [{ leg_id: 'leg-0', city: 'Bangkok', checkin: '2026-09-10', checkout: '2026-09-14' }],
  day_plans: [{
    leg_id: 'leg-0', city: 'Bangkok', num_days: 4,
    days: [{
      day_index: 0, bad_weather: false,
      attractions: [{ name: 'Wat Pho', category: 'tourism=attraction', lat: 13.747, lon: 100.492, fee: null }],
      meals: { breakfast: { name: 'Noodle Cart', category: 'amenity=restaurant', cuisine: 'thai' } },
    }],
  }],
  risk_signals: { per_leg: [] },
};

test('reload with persisted activePlan → /trips/{key} → itinerary restores', async ({ page }) => {
  // Preseed: returning guest with a persisted held plan
  await page.addInitScript(() => {
    localStorage.setItem('tg_session', JSON.stringify({
      user: null,
      guest: true,
      activePlan: { session_id: '', idempotency_key: 'trip-rr-1' },
      savedAt: Date.now(),
    }));
  });

  // Mock GET /trips/trip-rr-1 → the stored plan envelope
  await page.route('**/trips/**', (route) =>
    route.fulfill({
      status: 200, contentType: 'application/json',
      body: JSON.stringify(STORED_PLAN),
    }));
  await page.route('**/tile.openstreetmap.org/**', (route) => route.abort());

  await page.goto('/');

  // Itinerary restores without user having to re-submit a query
  await expect(page.getByTestId('itinerary')).toBeVisible({ timeout: 8000 });
  await expect(page.getByTestId('itinerary')).toContainText('Bangkok');
  await expect(page.getByTestId('itinerary')).toContainText('Wat Pho');
  await expect(page.getByTestId('itinerary')).toContainText('Day 1');

  // Banner shows the plan-ready state (held, awaiting confirm)
  await expect(page.getByTestId('status-banner')).toContainText('Plan ready');
  await expect(page.getByTestId('confirm-book')).toBeVisible();
});

test('reload → /trips/{key} 404 → stale key cleared, empty placeholder shown', async ({ page }) => {
  // Preseed: returning guest with a stale plan key that no longer exists
  await page.addInitScript(() => {
    localStorage.setItem('tg_session', JSON.stringify({
      user: null,
      guest: true,
      activePlan: { session_id: '', idempotency_key: 'trip-rr-stale' },
      savedAt: Date.now(),
    }));
  });

  // 404 → getTrip() returns null → activePlan cleared
  await page.route('**/trips/**', (route) =>
    route.fulfill({ status: 404, body: 'Not Found' }));
  await page.route('**/tile.openstreetmap.org/**', (route) => route.abort());

  await page.goto('/');

  // App still loads (view = 'app') — no crash on 404
  // The itinerary is NOT restored (stale key cleared gracefully)
  await expect(page.getByTestId('itinerary')).toHaveCount(0, { timeout: 8000 });

  // No banner (state = 'empty' with result=null)
  await expect(page.getByTestId('status-banner')).toHaveCount(0);

  // The chat input is available — user can plan a fresh trip
  await expect(page.getByTestId('chat-input')).toBeVisible();
});
