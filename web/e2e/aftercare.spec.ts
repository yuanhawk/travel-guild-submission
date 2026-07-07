import { test, expect } from '@playwright/test';

// aftercare.spec.ts — Aftercare Monitor tab: POST /aftercare/check → alerts render.
//
// The "Monitor" tab in RightRail only appears when isBooked=true (payment_status='charged'
// or booking_ref set). Clicking it renders Aftercare.svelte. The "Check for new risks"
// button calls aftercareCheck() → POST /aftercare/check. Alerts render with a severity
// chip and summary. Suggested actions must route through the WEBPAGE (an <a> link),
// never call a transaction endpoint (/confirm, /cancel, /replan) from the panel.
//
// CHANNEL-SECURITY BOUNDARY: assert that reconsider_leg + resuggest_area_lodging
// suggested actions render as <a href> links pointing to a webpage path, not as
// fetch/POST calls.

const PLAN_READY = {
  outcome: 'plan_ready',
  payment_status: 'held',
  booking_ref: null,
  idempotency_key: 'trip-ac-1',
  package_total_with_fees_cents: 200000,
  total_budget_cents: 400000,
  wallet: { balance_cents: 500000, held_cents: 200000, debited: false },
  legs: [{ leg_id: 'leg-0', city: 'Chiang Mai', checkin: '2027-01-10', checkout: '2027-01-14' }],
  day_plans: [{
    leg_id: 'leg-0', city: 'Chiang Mai', num_days: 4,
    days: [{ day_index: 0, bad_weather: false, attractions: [], meals: {} }],
  }],
  risk_signals: { per_leg: [] },
};

const BOOKED = {
  ...PLAN_READY,
  outcome: 'success',
  payment_status: 'charged',
  booking_ref: 'BK-AC-7',
  wallet: { balance_cents: 300000, debited: true, debit_cents: 200000 },
};

const AFTERCARE_RESULT = {
  outcome: 'ok',
  idempotency_key: 'trip-ac-1',
  monitoring: { status: 'ok', as_of: '2027-01-05', source: 'GDACS', checked_legs: 1 },
  alerts: [
    {
      leg_id: 'leg-0',
      city: 'Chiang Mai',
      risk_type: 'air_quality',
      severity_tier: 'medium',
      summary: 'Seasonal haze expected. Consider indoor activities on affected days.',
      advice: 'Bring an N95 mask. Check AQI before outdoor excursions.',
      suggested_action: 'reconsider_leg',
      action_target: {
        kind: 'webpage_review',
        webpage_path: '/trips/trip-ac-1/review',
      },
      source: 'IQAir',
      as_of: '2027-01-05',
      beta: false,
    },
  ],
  beta_note: null,
};

async function planConfirmAndOpenMonitor(page) {
  await page.addInitScript(() => {
    localStorage.setItem('tg_session', JSON.stringify({ user: null, guest: true, savedAt: Date.now() }));
  });
  await page.route('**/negotiate_text', (route) =>
    route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(PLAN_READY) }));
  await page.route('**/confirm', (route) =>
    route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(BOOKED) }));
  await page.route('**/tile.openstreetmap.org/**', (route) => route.abort());

  await page.goto('/');
  await page.getByTestId('chat-input').fill('4 days in chiang mai, temples, food');
  await page.getByRole('button', { name: 'Send' }).click();
  await expect(page.getByTestId('confirm-book')).toBeVisible({ timeout: 8000 });
  await page.getByTestId('confirm-book').click();
  await expect(page.getByTestId('status-banner')).toContainText('Booked', { timeout: 5000 });

  // Monitor tab only appears post-booking
  const monitorBtn = page.getByTestId('tab-aftercare-btn');
  await expect(monitorBtn).toBeVisible();
  await monitorBtn.click();

  // Aftercare panel renders inside the Monitor pane
  await expect(page.getByTestId('tab-aftercare')).toBeVisible();
  await expect(page.getByTestId('aftercare-panel')).toBeVisible();
}

test('Monitor tab appears only after booking, aftercare-check-btn is present', async ({ page }) => {
  await planConfirmAndOpenMonitor(page);

  // Check button is present (not yet clicked — no alerts shown)
  const checkBtn = page.getByTestId('aftercare-check-btn');
  await expect(checkBtn).toBeVisible();
  await expect(checkBtn).toContainText('Check for new risks');
  await expect(checkBtn).not.toBeDisabled();

  // No alerts yet (not checked)
  await expect(page.getByTestId('aftercare-alert')).toHaveCount(0);
});

test('aftercare check → alerts render with severity chip and summary', async ({ page }) => {
  await page.route('**/aftercare/check', (route) =>
    route.fulfill({
      status: 200, contentType: 'application/json',
      body: JSON.stringify(AFTERCARE_RESULT),
    }));

  await planConfirmAndOpenMonitor(page);

  // Click "Check for new risks"
  await page.getByTestId('aftercare-check-btn').click();

  // Alert card renders
  await expect(page.getByTestId('aftercare-alert')).toBeVisible({ timeout: 5000 });

  // Severity chip shows 'MED' (medium tier)
  const chip = page.getByTestId('severity-chip');
  await expect(chip).toBeVisible();
  await expect(chip).toContainText('MED');

  // Alert summary text from server (not fabricated)
  const summary = page.getByTestId('alert-summary');
  await expect(summary).toBeVisible();
  await expect(summary).toContainText('Seasonal haze');

  // Monitoring status line renders
  await expect(page.getByTestId('aftercare-panel')).toContainText('Monitoring active');
  await expect(page.getByTestId('aftercare-panel')).toContainText('GDACS');
});

test('reconsider_leg → suggested action is a WEBPAGE link, not a transaction call', async ({ page }) => {
  await page.route('**/aftercare/check', (route) =>
    route.fulfill({
      status: 200, contentType: 'application/json',
      body: JSON.stringify(AFTERCARE_RESULT),
    }));

  await planConfirmAndOpenMonitor(page);
  await page.getByTestId('aftercare-check-btn').click();
  await expect(page.getByTestId('aftercare-alert')).toBeVisible({ timeout: 5000 });

  // The reconsider_leg action renders as an <a> link, NOT a <button> that POSTs to /confirm etc.
  const actionLink = page.getByTestId('reconsider-leg-btn');
  await expect(actionLink).toBeVisible();

  // It must have an href (webpage route), not trigger a transactional API call
  const href = await actionLink.getAttribute('href');
  expect(href).toBeTruthy();
  expect(href).toContain('/trips/');   // webpage path
  expect(href).not.toContain('/confirm');
  expect(href).not.toContain('/cancel');
  expect(href).not.toContain('/replan');

  // Opens in a new tab (target=_blank) — cannot navigate away from the monitor panel
  const target = await actionLink.getAttribute('target');
  expect(target).toBe('_blank');
});

test('Monitor tab NOT shown for plan_ready (pre-booking)', async ({ page }) => {
  // Only a plan_ready state — NOT confirmed/booked
  await page.addInitScript(() => {
    localStorage.setItem('tg_session', JSON.stringify({ user: null, guest: true, savedAt: Date.now() }));
  });
  await page.route('**/negotiate_text', (route) =>
    route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(PLAN_READY) }));
  await page.route('**/tile.openstreetmap.org/**', (route) => route.abort());

  await page.goto('/');
  await page.getByTestId('chat-input').fill('4 days chiang mai');
  await page.getByRole('button', { name: 'Send' }).click();
  await expect(page.getByTestId('status-banner')).toContainText('Plan ready', { timeout: 8000 });

  // Monitor tab must NOT appear before booking (isBooked=false)
  await expect(page.getByTestId('tab-aftercare-btn')).toHaveCount(0);
});
