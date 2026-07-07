import { test, expect } from '@playwright/test';

// Dashboard E2E (hermetic): /negotiate_text + /confirm are MOCKED via page.route — no
// live backend. OSM tiles aborted (offline + fast). The app sends plan:true, so the first
// response is a HELD plan_ready; the user confirms to book (the consent split / THE MOAT).

const PLAN_READY = {
  outcome: 'plan_ready',
  payment_status: 'held',
  booking_ref: null,
  idempotency_key: 'trip-hok-1',
  package_total_with_fees_cents: 324000,
  total_budget_cents: 400000,
  wallet: { balance_cents: 500000, held_cents: 324000, debited: false },
  fee_line_items: [{ description: 'Platform & FX fees', usd_cents: 9400 }],
  legs: [
    { leg_id: 'leg-0', city: 'Sapporo', checkin: '2026-10-01', checkout: '2026-10-04', lat: 43.06, lng: 141.35 },
    { leg_id: 'leg-1', city: 'Hakodate', checkin: '2026-10-07', checkout: '2026-10-10', lat: 41.77, lng: 140.73,
      unverified_lodging: true, note: 'exact hotel location unavailable' },
  ],
  day_plans: [
    {
      leg_id: 'leg-0', city: 'Sapporo', num_days: 3,
      days: [{
        day_index: 1, bad_weather: false,
        attractions: [{ name: 'Mt. Moiwa Trail', category: 'tourism=viewpoint', weather_exposure: 'outdoor', lat: 43.04, lon: 141.32, fee: null }],
        meals: { breakfast: { name: 'Kissaten', category: 'amenity=cafe', cuisine: 'coffee' }, lunch: { name: 'Ramen Counter', cuisine: 'ramen' } },
        intracity_hops: [{ label: 'Hotel → Mt. Moiwa', mode: 'tram', minutes: 18 }],
      }],
      unscheduled_attractions: [{ name: 'Otaru Canal', category: 'tourism=attraction', lat: 43.2, lon: 141.0 }],
    },
    { leg_id: 'leg-1', city: 'Hakodate', num_days: 3, days: [{ day_index: 1, attractions: [], meals: {} }] },
  ],
  risk_signals: {
    consolidator: 'risk_agent',
    per_leg: [
      { leg_id: 'leg-0', alert_tier: 'MED', cyclone_likelihood_pct: 22, cyclone_basin: 'typhoon', flood_index_bp: 3100, decisions: { flag: true } },
      // categorical advisory, NO numeric likelihood — must render the advisory TEXT, not "No notable hazards"
      { leg_id: 'leg-1', alert_tier: 'HIGH', decisions: { flag: true },
        advisory: [{ type: 'civil_unrest', severity: 'high', detail: 'Localized unrest reported; monitor advisories.' }] },
    ],
  },
  active_emergencies: [{ leg_id: 'leg-0', city: 'Sapporo', status: 'clear' }],
};

const BOOKED = {
  ...PLAN_READY, outcome: 'success', payment_status: 'charged', booking_ref: 'BK-HOK-7',
  wallet: { balance_cents: 176000, debited: true, debit_cents: 324000 },
};

async function planTrip(page, planBody, confirmBody) {
  // Pre-seed a guest session so the SessionPicker is bypassed (slice 4 picker only
  // appears on a fresh page with no stored choice). Storage key = 'tg_session'.
  await page.addInitScript(() => {
    localStorage.setItem('tg_session', JSON.stringify({ user: null, guest: true, savedAt: Date.now() }));
  });
  await page.route('**/negotiate_text', (route) =>
    route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(planBody) }));
  if (confirmBody) {
    await page.route('**/confirm', (route) =>
      route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(confirmBody) }));
  }
  await page.route('**/tile.openstreetmap.org/**', (route) => route.abort());
  await page.goto('/');
  await page.getByTestId('chat-input').fill('10 days around hokkaido, hiking, $4000');
  await page.getByRole('button', { name: 'Send' }).click();
}

test('plan-only renders the held dashboard + Confirm & Book (no fake booked)', async ({ page }) => {
  await planTrip(page, PLAN_READY);
  const banner = page.getByTestId('status-banner');
  await expect(banner).toContainText('Plan ready');
  await expect(banner).toContainText('held');
  await expect(banner).toContainText('$3,240.00');
  await expect(page.getByTestId('confirm-book')).toBeVisible();   // the one consent click
  await expect(banner).not.toContainText('Booked');               // nothing booked yet (honest)

  // multi-city itinerary + meal-anchored slots (no clock times)
  await expect(page.getByTestId('itinerary')).toContainText('Sapporo');
  await expect(page.getByTestId('itinerary')).toContainText('Hakodate');
  await expect(page.getByTestId('itinerary')).toContainText('Breakfast');
  await expect(page.getByTestId('itinerary')).toContainText('🌤 outdoor');
  // unverified-lodging honesty flag surfaced (never silently booked)
  await expect(page.getByTestId('unverified-lodging')).toContainText('Lodging unverified');

  // Safety: numeric hazards (typhoon + flood) AND the categorical advisory TEXT render.
  // Scope to right-rail to avoid the global SafetyWatch badge which also contains "Safety".
  await page.getByTestId('right-rail').getByRole('button', { name: 'Safety' }).click();
  await expect(page.getByTestId('tab-safety')).toContainText('Typhoon');
  await expect(page.getByTestId('tab-safety')).toContainText('Flood');
  // Finding #6 (map/mobile UX sweep): advisory.type is a raw served snake_case key —
  // this must render the humanized "Civil Unrest" (prettyCategory()), never the raw
  // "civil_unrest" string, per the #201 user-facing message-register bar.
  await expect(page.getByTestId('advisory').first()).toContainText('Civil Unrest');
  await expect(page.getByTestId('advisory').first()).not.toContainText('civil_unrest');
});

test('Confirm & Book → /confirm → booked (the consent moment)', async ({ page }) => {
  await planTrip(page, PLAN_READY, BOOKED);
  await page.getByTestId('confirm-book').click();
  const banner = page.getByTestId('status-banner');
  await expect(banner).toContainText('Booked');
  await expect(banner).toContainText('BK-HOK-7');
  await expect(banner).toContainText('$3,240.00');     // charged
});

test('insufficient_funds at confirm shows the amount from total_cents (not —)', async ({ page }) => {
  await planTrip(page, PLAN_READY, {
    outcome: 'cannot_satisfy', reason: 'insufficient_funds',
    total_cents: 324000, wallet_balance_cents: 40000,
  });
  await page.getByTestId('confirm-book').click();
  // the note carries the real amounts (the package_total_cents → total_cents fix)
  await expect(page.getByTestId('state-card')).toContainText('$3,240.00');
  await expect(page.getByTestId('state-card')).toContainText('$400.00');
});
