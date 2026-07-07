import { test, expect } from '@playwright/test';

// date-required-booking.spec.ts — a held plan with no real trip dates cannot be booked.
//
// Bug: DateRangePicker was advisory-only — it rendered above the itinerary when dates
// were missing, but never gated the Confirm & Book button below it. Fix: the button is
// now disabled (plus an inline note) whenever tripHasDates is false (App.svelte), and the
// backend's commit_plan() independently refuses to charge a dateless plan (defense-in-
// depth against a direct API call bypassing the UI) — see the equivalent backend test in
// society/tests/test_commit_plan_requires_dates.py (this repo's root).

const PLAN_READY_NO_DATES = {
  outcome: 'plan_ready',
  payment_status: 'held',
  booking_ref: null,
  idempotency_key: 'trip-nodate-1',
  package_total_with_fees_cents: 180000,
  total_budget_cents: 400000,
  wallet: { balance_cents: 500000, held_cents: 180000, debited: false },
  // No checkin/checkout anywhere — legs and day_plans both omit real dates.
  legs: [{ leg_id: 'leg-0', city: 'Lisbon' }],
  day_plans: [{
    leg_id: 'leg-0', city: 'Lisbon', num_days: 3,
    days: [{ day_index: 0, bad_weather: false, attractions: [], meals: {} }],
  }],
  risk_signals: { per_leg: [] },
};

const PLAN_READY_WITH_DATES = {
  ...PLAN_READY_NO_DATES,
  idempotency_key: 'trip-withdate-1',
  legs: [{ leg_id: 'leg-0', city: 'Lisbon', checkin: '2026-11-01', checkout: '2026-11-04' }],
};

async function planFor(page, plan: unknown) {
  await page.addInitScript(() => {
    localStorage.setItem('tg_session', JSON.stringify({ user: null, guest: true, savedAt: Date.now() }));
  });
  await page.route('**/negotiate_text', (route) =>
    route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(plan) }));
  await page.route('**/tile.openstreetmap.org/**', (route) => route.abort());
  await page.goto('/');
  await page.getByTestId('chat-input').fill('3 days in lisbon, $1800');
  await page.getByRole('button', { name: 'Send' }).click();
  await expect(page.getByTestId('status-banner')).toContainText('Plan ready', { timeout: 8000 });
}

test('confirm-book is disabled and a note is shown when the plan has no dates', async ({ page }) => {
  await planFor(page, PLAN_READY_NO_DATES);

  const confirmBtn = page.getByTestId('confirm-book');
  await expect(confirmBtn).toBeVisible();
  await expect(confirmBtn).toBeDisabled();
  await expect(page.getByTestId('date-required-note')).toBeVisible();
  await expect(page.getByTestId('date-required-note')).toContainText('Set your trip dates');
});

test('confirm-book is enabled and no note is shown when the plan already has dates', async ({ page }) => {
  await planFor(page, PLAN_READY_WITH_DATES);

  const confirmBtn = page.getByTestId('confirm-book');
  await expect(confirmBtn).toBeVisible();
  await expect(confirmBtn).toBeEnabled();
  await expect(page.getByTestId('date-required-note')).toHaveCount(0);
});

test('setting dates via the picker enables confirm-book (no dates → refine adds dates → button unlocks)', async ({ page }) => {
  await planFor(page, PLAN_READY_NO_DATES);
  await expect(page.getByTestId('confirm-book')).toBeDisabled();

  // DateRangePicker's confirm calls refineCurrentPlan(), which POSTs /refine.
  await page.route('**/refine', (route) =>
    route.fulfill({
      status: 200, contentType: 'application/json',
      body: JSON.stringify({
        outcome: 'plan_ready',
        idempotency_key: 'trip-nodate-1',
        assistant_reply: 'Dates set.',
        plan: PLAN_READY_WITH_DATES,
      }),
    }));

  await page.getByTestId('start-date').fill('2026-11-01');
  await page.getByTestId('end-date').fill('2026-11-04');
  await page.getByTestId('date-confirm').click();

  await expect(page.getByTestId('confirm-book')).toBeEnabled({ timeout: 5000 });
  await expect(page.getByTestId('date-required-note')).toHaveCount(0);
});
