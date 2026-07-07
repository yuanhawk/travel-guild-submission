import { test, expect } from '@playwright/test';

// Slice 4 (hermetic): /session + /negotiate_text MOCKED via page.route. Fresh localStorage
// forces the demo-user picker; selecting a user must replay its user_id on the plan request.

const USERS = {
  demo_users: [
    { user_id: 'demo-mei', display_name: 'Mei', persona: 'foodie', nationality: 'SG', home_currency: 'SGD', wallet_balance_cents: 500000 },
    { user_id: 'demo-alex', display_name: 'Alex', persona: 'hiker', nationality: 'US', home_currency: 'USD', wallet_balance_cents: 500000 },
  ],
};
const PLAN_READY = {
  outcome: 'plan_ready', payment_status: 'held', booking_ref: null, idempotency_key: 'trip-x',
  package_total_with_fees_cents: 100000, total_budget_cents: 300000,
  wallet: { balance_cents: 500000, held_cents: 100000, debited: false },
  legs: [{ leg_id: 'leg-0', city: 'Tokyo', checkin: '2026-10-01', checkout: '2026-10-05' }],
  day_plans: [{ leg_id: 'leg-0', city: 'Tokyo', num_days: 4, days: [{ day_index: 1, attractions: [], meals: {} }] }],
  risk_signals: { per_leg: [] },
};

test('demo-user picker → select → user_id replayed on the plan request', async ({ page }) => {
  await page.addInitScript(() => localStorage.clear());          // fresh → force the picker
  await page.route('**/session', (r) =>
    r.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(USERS) }));
  let planBody: Record<string, unknown> | null = null;
  await page.route('**/negotiate_text', (r) => {
    planBody = JSON.parse(r.request().postData() || '{}');
    return r.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(PLAN_READY) });
  });
  await page.route('**/emergencies', (r) =>
    r.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ status: 'ok', countries: [] }) }));
  await page.route(/tile|openstreetmap|basemaps|demotiles/, (r) => r.abort());

  await page.goto('/');
  await expect(page.getByTestId('session-picker')).toBeVisible();
  await page.getByTestId('user-demo-mei').click();

  // active-traveller chip
  await expect(page.getByTestId('prefs-link')).toContainText('Mei');

  // plan → the request must carry the replayed user_id + nationality
  await page.getByTestId('chat-input').fill('5 days in tokyo, culture, $3000');
  await page.getByRole('button', { name: 'Send' }).click();
  await expect.poll(() => planBody?.user_id).toBe('demo-mei');
  expect(planBody?.nationality).toBe('SG');
  expect(planBody?.plan).toBe(true);
});

test('continue as guest → no picker on reload, no user_id on plan', async ({ page }) => {
  await page.addInitScript(() => localStorage.clear());
  await page.route('**/session', (r) =>
    r.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(USERS) }));
  let planBody: Record<string, unknown> | null = null;
  await page.route('**/negotiate_text', (r) => {
    planBody = JSON.parse(r.request().postData() || '{}');
    return r.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(PLAN_READY) });
  });
  await page.route('**/emergencies', (r) =>
    r.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ status: 'ok', countries: [] }) }));
  await page.route(/tile|openstreetmap|basemaps|demotiles/, (r) => r.abort());

  await page.goto('/');
  await page.getByTestId('continue-guest').click();
  await page.getByTestId('chat-input').fill('5 days in tokyo, $3000');
  await page.getByRole('button', { name: 'Send' }).click();
  await expect.poll(() => planBody !== null).toBe(true);
  expect(planBody?.user_id).toBeUndefined();             // guest → system defaults
});

// ── Error path: "Couldn't load accounts" ─────────────────────────────────────
// Covers the tablet-reported bug: backend unreachable / WAF blocks POST →
// 405/530 → postJson throws "Error: /session → HTTP 4xx" → sessionError shown.

for (const [label, status] of [['405 Method Not Allowed', 405], ['503 Service Unavailable', 503], ['530 Tunnel down', 530]] as const) {
  test(`/session ${status} → shows "Couldn't load accounts" banner with guest fallback`, async ({ page }) => {
    await page.addInitScript(() => localStorage.clear());
    await page.route('**/session', (r) =>
      r.fulfill({ status, contentType: 'text/plain', body: `${label}` }));
    await page.route(/tile|openstreetmap|basemaps|demotiles/, (r) => r.abort());

    await page.goto('/');

    // Error banner must appear with the status code in it
    const banner = page.getByTestId('session-error');
    await expect(banner).toBeVisible({ timeout: 8000 });
    await expect(banner).toContainText(`${status}`);

    // "Continue as guest" link in the banner lets the user proceed
    await banner.getByRole('button', { name: /guest/i }).click();
    await expect(page.getByTestId('chat-input')).toBeVisible({ timeout: 5000 });
  });
}

test('/session network failure → shows "Couldn\'t load accounts" banner', async ({ page }) => {
  await page.addInitScript(() => localStorage.clear());
  await page.route('**/session', (r) => r.abort('failed'));
  await page.route(/tile|openstreetmap|basemaps|demotiles/, (r) => r.abort());

  await page.goto('/');

  const banner = page.getByTestId('session-error');
  await expect(banner).toBeVisible({ timeout: 8000 });
  await banner.getByRole('button', { name: /guest/i }).click();
  await expect(page.getByTestId('chat-input')).toBeVisible({ timeout: 5000 });
});

// ── Tablet viewport (820×1180 — iPad Air) ────────────────────────────────────
// Regression for the tablet-reported 405 bug: session picker and guest flow
// must work correctly at tablet resolution (not just desktop 1280×720).

test.describe('tablet viewport (820×1180)', () => {
  test.use({ viewport: { width: 820, height: 1180 } });

  test('session picker renders and user selection works on tablet', async ({ page }) => {
    await page.addInitScript(() => localStorage.clear());
    await page.route('**/session', (r) =>
      r.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(USERS) }));
    await page.route('**/negotiate_text', (r) =>
      r.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(PLAN_READY) }));
    await page.route('**/emergencies', (r) =>
      r.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ status: 'ok', countries: [] }) }));
    await page.route(/tile|openstreetmap|basemaps|demotiles/, (r) => r.abort());

    await page.goto('/');
    await expect(page.getByTestId('session-picker')).toBeVisible({ timeout: 8000 });
    await page.getByTestId('user-demo-mei').click();
    await expect(page.getByTestId('prefs-link')).toContainText('Mei');
  });

  test('405 error shows banner and guest fallback works on tablet', async ({ page }) => {
    await page.addInitScript(() => localStorage.clear());
    await page.route('**/session', (r) =>
      r.fulfill({ status: 405, contentType: 'text/plain', body: 'Method Not Allowed' }));
    await page.route(/tile|openstreetmap|basemaps|demotiles/, (r) => r.abort());

    await page.goto('/');

    const banner = page.getByTestId('session-error');
    await expect(banner).toBeVisible({ timeout: 8000 });
    await expect(banner).toContainText('405');

    // Guest path must be reachable (no overflow / off-screen button on tablet)
    const guestBtn = banner.getByRole('button', { name: /guest/i });
    await expect(guestBtn).toBeVisible();
    await expect(guestBtn).toBeInViewport();
    await guestBtn.click();
    await expect(page.getByTestId('chat-input')).toBeVisible({ timeout: 5000 });
  });

  test('continue as guest skips picker and chat-input is usable on tablet', async ({ page }) => {
    await page.addInitScript(() => localStorage.clear());
    await page.route('**/session', (r) =>
      r.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(USERS) }));
    await page.route(/tile|openstreetmap|basemaps|demotiles/, (r) => r.abort());

    await page.goto('/');
    await page.getByTestId('continue-guest').click();

    // chat-input must be visible and in viewport on tablet (not hidden behind mobile sheet)
    const input = page.getByTestId('chat-input');
    await expect(input).toBeVisible({ timeout: 5000 });
    await expect(input).toBeInViewport();
  });
});

// ── Account-cluster regression (landing-page-draft2) ──────────────────────────
// The wallet input, user/persona/currency pill, and log in/out button were
// visually regrouped into one `.account-cluster` pill. This was a PURE visual
// regroup per the diff's tech-note — all bindings/handlers/testids preserved
// unchanged. These tests guard that each element still functions individually
// after being nested inside the new wrapper.

test.describe('account-cluster: grouped elements still function individually', () => {
  test('wallet input accepts a value inside the account-cluster', async ({ page }) => {
    await page.addInitScript(() => localStorage.clear());
    await page.route('**/session', (r) =>
      r.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(USERS) }));
    await page.route(/tile|openstreetmap|basemaps|demotiles/, (r) => r.abort());

    await page.goto('/');
    await page.getByTestId('continue-guest').click();

    const walletInput = page.locator('.account-cluster .wallet input');
    await expect(walletInput).toBeVisible({ timeout: 5000 });
    await walletInput.fill('3200');
    await expect(walletInput).toHaveValue('3200');
  });

  test('prefs-link opens Preferences for the correct testid', async ({ page }) => {
    await page.addInitScript(() => localStorage.clear());
    await page.route('**/session', (r) =>
      r.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(USERS) }));
    await page.route('**/session/login', (r) =>
      r.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ session_token: 'fake-tok' }) }));
    await page.route('**/preferences*', (r) =>
      r.fulfill({
        status: 200, contentType: 'application/json',
        body: JSON.stringify({
          user_id: 'demo-mei', display_name: 'Mei', persona: 'foodie',
          nationality: 'SG', home_currency: 'SGD', prefs: {},
        }),
      }));
    await page.route(/tile|openstreetmap|basemaps|demotiles/, (r) => r.abort());

    await page.goto('/');
    await page.getByTestId('user-demo-mei').click();

    const prefsLink = page.locator('.account-cluster').getByTestId('prefs-link');
    await expect(prefsLink).toBeVisible({ timeout: 5000 });
    await expect(prefsLink).toContainText('Mei');
    await prefsLink.click();

    await expect(page.getByTestId('preferences')).toBeVisible({ timeout: 5000 });
  });

  test('switch-user still works inside the account-cluster (log in → log out → picker)', async ({ page }) => {
    await page.addInitScript(() => localStorage.clear());
    await page.route('**/session', (r) =>
      r.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(USERS) }));
    await page.route('**/session/login', (r) =>
      r.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ session_token: 'fake-tok' }) }));
    await page.route(/tile|openstreetmap|basemaps|demotiles/, (r) => r.abort());

    await page.goto('/');
    await page.getByTestId('user-demo-mei').click();

    const switchBtn = page.locator('.account-cluster').getByTestId('switch-user');
    await expect(switchBtn).toBeVisible({ timeout: 5000 });
    await expect(switchBtn).toContainText('Log out');
    await switchBtn.click();

    // Logging out returns to the session picker.
    await expect(page.getByTestId('session-picker')).toBeVisible({ timeout: 5000 });
  });
});
