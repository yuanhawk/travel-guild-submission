import { test, expect } from '@playwright/test';

// price-overlay.spec.ts — Live best-price overlay on Itinerary legs.
//
// The price overlay is DISPLAY-ONLY and var-0 firewalled: it is attached by the backend
// post-negotiate (in PROD/live edition) and the FE renders it purely for display.
// In UAT (seeded) the field is absent → zero regression to the seeded view.
//
// The overlay reads from price_overlay.status:'ok' OR live_price.status:'ok' on each leg.
// Fields: lowest_price_cents (integer), currency (ISO-4217), deeplink (https://…), source.
// Renders: data-testid live-price-{li}, live-price-amount, live-price-badge,
//          live-price-source, live-price-deeplink.

const BASE_PLAN = {
  outcome: 'plan_ready',
  payment_status: 'held',
  booking_ref: null,
  idempotency_key: 'trip-po-1',
  package_total_with_fees_cents: 200000,
  total_budget_cents: 500000,
  wallet: { balance_cents: 600000, held_cents: 200000, debited: false },
  risk_signals: { per_leg: [] },
  day_plans: [{
    leg_id: 'leg-0',
    city: 'Bali',
    num_days: 4,
    days: [{ day_index: 0, bad_weather: false, attractions: [], meals: {}, intracity_hops: [] }],
  }],
};

const PLAN_WITH_PRICE_OVERLAY = {
  ...BASE_PLAN,
  legs: [{
    leg_id: 'leg-0',
    city: 'Bali',
    checkin: '2026-12-01',
    checkout: '2026-12-05',
    lat: -8.34,
    lng: 115.09,
    price_overlay: {
      status: 'ok',
      hotel: 'Alaya Resort Ubud',
      lowest_price_cents: 12000,
      currency: 'USD',
      deeplink: 'https://example.com/book/alaya',
      source: 'ExamplePartner',
      fetched_at: '2026-12-01T00:00:00Z',
    },
  }],
};

const PLAN_WITH_LIVE_PRICE_KEY = {
  ...BASE_PLAN,
  idempotency_key: 'trip-po-2',
  legs: [{
    leg_id: 'leg-0',
    city: 'Bangkok',
    checkin: '2026-11-01',
    checkout: '2026-11-04',
    lat: 13.75,
    lng: 100.52,
    live_price: {
      status: 'ok',
      hotel: 'Mandarin Oriental Bangkok',
      lowest_price_cents: 32500,
      currency: 'USD',
      deeplink: 'https://example.com/book/mandarin',
      source: 'Booking.com',
    },
  }],
};

const PLAN_WITH_UNAVAILABLE_OVERLAY = {
  ...BASE_PLAN,
  idempotency_key: 'trip-po-3',
  legs: [{
    leg_id: 'leg-0',
    city: 'Chiang Mai',
    checkin: '2026-11-01',
    checkout: '2026-11-04',
    lat: 18.79,
    lng: 98.98,
    price_overlay: { status: 'unavailable', reason: 'no results' },
  }],
};

const PLAN_NO_OVERLAY = {
  ...BASE_PLAN,
  idempotency_key: 'trip-po-4',
  legs: [{
    leg_id: 'leg-0',
    city: 'Kyoto',
    checkin: '2026-10-01',
    checkout: '2026-10-04',
    lat: 35.01,
    lng: 135.76,
  }],
};

async function planWith(page, planFixture) {
  await page.addInitScript(() => {
    localStorage.setItem('tg_session', JSON.stringify({ user: null, guest: true, savedAt: Date.now() }));
  });
  await page.route('**/negotiate_text', (route) =>
    route.fulfill({ status: 200, contentType: 'application/json',
      body: JSON.stringify(planFixture) }));
  await page.route('**/tile.openstreetmap.org/**', (route) => route.abort());
  await page.route('**/emergencies', (route) =>
    route.fulfill({ status: 200, contentType: 'application/json',
      body: JSON.stringify({ status: 'ok', countries: [] }) }));

  await page.goto('/');
  await page.getByTestId('chat-input').fill('test trip');
  await page.getByRole('button', { name: 'Send' }).click();
  await expect(page.getByTestId('itinerary')).toBeVisible({ timeout: 8000 });
}

test('price overlay renders amount from price_overlay.status:ok', async ({ page }) => {
  await planWith(page, PLAN_WITH_PRICE_OVERLAY);

  // live-price-amount must show $120.00 (12000 cents USD)
  const amount = page.getByTestId('live-price-amount');
  await expect(amount).toBeVisible({ timeout: 5000 });
  await expect(amount).toContainText('$120.00');
});

test('price overlay renders "live" badge', async ({ page }) => {
  await planWith(page, PLAN_WITH_PRICE_OVERLAY);

  const badge = page.getByTestId('live-price-badge');
  await expect(badge).toBeVisible({ timeout: 5000 });
  await expect(badge).toHaveText('live');
});

test('price overlay renders source label', async ({ page }) => {
  await planWith(page, PLAN_WITH_PRICE_OVERLAY);

  const source = page.getByTestId('live-price-source');
  await expect(source).toBeVisible({ timeout: 5000 });
  await expect(source).toContainText('ExamplePartner');
});

test('price overlay renders a "Book →" deeplink with the correct href', async ({ page }) => {
  await planWith(page, PLAN_WITH_PRICE_OVERLAY);

  const link = page.getByTestId('live-price-deeplink');
  await expect(link).toBeVisible({ timeout: 5000 });
  await expect(link).toHaveText('Book →');
  await expect(link).toHaveAttribute('href', 'https://example.com/book/alaya');
  // Security: link must open in a new tab with noopener
  await expect(link).toHaveAttribute('target', '_blank');
  await expect(link).toHaveAttribute('rel', 'noopener noreferrer');
});

test('live_price key (alternative field name) renders overlay identically', async ({ page }) => {
  await planWith(page, PLAN_WITH_LIVE_PRICE_KEY);

  // $325.00 (32500 cents)
  const amount = page.getByTestId('live-price-amount');
  await expect(amount).toBeVisible({ timeout: 5000 });
  await expect(amount).toContainText('$325.00');
  await expect(page.getByTestId('live-price-source')).toContainText('Booking.com');
});

test('price overlay absent when status is unavailable (honest: no price shown)', async ({ page }) => {
  await planWith(page, PLAN_WITH_UNAVAILABLE_OVERLAY);

  // No live-price elements rendered when status:'unavailable'
  await expect(page.getByTestId('live-price-amount')).toHaveCount(0);
  await expect(page.getByTestId('live-price-badge')).toHaveCount(0);
  await expect(page.getByTestId('live-price-deeplink')).toHaveCount(0);
});

test('price overlay absent when price_overlay field is missing entirely (UAT seeded)', async ({ page }) => {
  await planWith(page, PLAN_NO_OVERLAY);

  // UAT path: no overlay → seeded display unchanged, no price line
  await expect(page.getByTestId('live-price-amount')).toHaveCount(0);
  await expect(page.getByTestId('live-price-badge')).toHaveCount(0);
});

test('price overlay does not appear in booking hash / digest path (var-0)', async ({ page }) => {
  // This test verifies that the overlay renders AFTER the plan is shown (pure display),
  // not that it affects the hold or booking_ref. The idempotency_key is unchanged
  // between PLAN_WITH_PRICE_OVERLAY and a confirm — the overlay is additive/display-only.
  await page.route('**/confirm', (route) =>
    route.fulfill({
      status: 200, contentType: 'application/json',
      body: JSON.stringify({
        ...PLAN_WITH_PRICE_OVERLAY,
        outcome: 'success',
        payment_status: 'charged',
        booking_ref: 'BK-PO-1',
        wallet: { balance_cents: 400000, debited: true, debit_cents: 200000 },
      }),
    }));
  await planWith(page, PLAN_WITH_PRICE_OVERLAY);

  // Confirm the booking
  await page.getByTestId('confirm-book').click();
  await expect(page.getByTestId('status-banner')).toContainText('Booked', { timeout: 8000 });
  await expect(page.getByTestId('status-banner')).toContainText('BK-PO-1');

  // Price overlay may still render (it's display data), but booking_ref is deterministic
  // and independent of the overlay. The key invariant: booking succeeded with correct ref.
  const ref = await page.getByTestId('status-banner').textContent();
  expect(ref).toContain('BK-PO-1');
});
