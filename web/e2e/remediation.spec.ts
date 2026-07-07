import { test, expect } from '@playwright/test';

// E2E regressions from the catch-up audit (hermetic mocks; chromium runs in CI).
// Covers: the Day-0 off-by-one fix, booked-state hides Confirm, and the
// reconcile/server_error retry-safe path (held plan kept, same key reused).

const PLAN_READY = {
  outcome: 'plan_ready', payment_status: 'held', booking_ref: null,
  idempotency_key: 'trip-r-1',
  package_total_with_fees_cents: 324000, total_budget_cents: 400000,
  wallet: { balance_cents: 500000, held_cents: 324000, debited: false },
  legs: [{ leg_id: 'leg-0', city: 'Sapporo', checkin: '2026-10-01', checkout: '2026-10-04' }],
  day_plans: [{
    leg_id: 'leg-0', city: 'Sapporo', num_days: 1,
    // 0-based day_index from the backend — the UI must render "Day 1", not "Day 0"
    days: [{ day_index: 0, bad_weather: false, attractions: [], meals: {} }],
  }],
};
const BOOKED = {
  ...PLAN_READY, outcome: 'success', payment_status: 'charged', booking_ref: 'BK-R-1',
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
  await page.getByTestId('chat-input').fill('5 days sapporo');
  await page.getByRole('button', { name: 'Send' }).click();
}

test('0-based day_index renders "Day 1", never "Day 0"', async ({ page }) => {
  await planTrip(page, PLAN_READY);
  const itin = page.getByTestId('itinerary');
  await expect(itin).toContainText('Day 1');
  await expect(itin).not.toContainText('Day 0');
});

test('booked state HIDES the Confirm & Book button (cannot re-confirm)', async ({ page }) => {
  await planTrip(page, PLAN_READY, BOOKED);
  await expect(page.getByTestId('confirm-book')).toBeVisible();
  await page.getByTestId('confirm-book').click();
  await expect(page.getByTestId('status-banner')).toContainText('Booked');
  await expect(page.getByTestId('confirm-book')).toHaveCount(0);
});

test('confirm RECONCILE keeps the held plan + retry-safe copy (no re-book)', async ({ page }) => {
  await planTrip(page, PLAN_READY, { outcome: 'cannot_satisfy', reason: 'commit_errored', needs_reconciliation: true });
  await page.getByTestId('confirm-book').click();
  await expect(page.getByTestId('status-banner')).toContainText('Plan ready');
  await expect(page.getByTestId('confirm-book')).toBeVisible();
  await expect(page.getByText(/Tap Confirm again/i)).toBeVisible();
});

test('confirm SERVER_ERROR also keeps the held plan + retry-safe copy', async ({ page }) => {
  await planTrip(page, PLAN_READY, { outcome: 'cannot_satisfy', reason: 'server_error' });
  await page.getByTestId('confirm-book').click();
  await expect(page.getByTestId('status-banner')).toContainText('Plan ready');
  await expect(page.getByTestId('confirm-book')).toBeVisible();
  await expect(page.getByText(/Tap Confirm again/i)).toBeVisible();
});
