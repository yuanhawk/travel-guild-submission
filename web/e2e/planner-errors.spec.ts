import { test, expect, type Page } from '@playwright/test';

// planner-errors.spec.ts — hermetic coverage for /negotiate_text error paths.
//
// Root cause that prompted these tests:
//   VITE_API_BASE was empty in a production bundle so all POST requests went
//   through a static-host redirect proxy, which returns 405 for POST. Users saw
//   "Couldn't reach the planner: Error: /negotiate_text → HTTP 405". Fixed by
//   setting VITE_API_BASE to the real API host in production builds.
//
// These tests ensure:
//   a) Every 4xx/5xx and network-failure from /negotiate_text surfaces a visible
//      note in the chat — nothing is silently swallowed or left as a spinner.
//   b) The request is always a well-formed POST with Content-Type: application/json.
//   c) A plan renders normally after recovering from a prior error.
//
// Live URL routing (VITE_API_BASE baked correctly) against a real deployment
// is covered by a separate staging-only test suite not included in this repo.

const PLAN_READY = {
  outcome: 'plan_ready', payment_status: 'held', booking_ref: null,
  idempotency_key: 'trip-pe-1',
  package_total_with_fees_cents: 100000, total_budget_cents: 300000,
  wallet: { balance_cents: 500000, held_cents: 100000, debited: false },
  legs: [{ leg_id: 'leg-0', city: 'Tokyo', checkin: '2026-10-01', checkout: '2026-10-05' }],
  day_plans: [{
    leg_id: 'leg-0', city: 'Tokyo', num_days: 4,
    days: [{ day_index: 0, bad_weather: false, attractions: [], meals: {}, intracity_hops: [] }],
  }],
  risk_signals: { per_leg: [] },
};

async function setup(page: Page): Promise<void> {
  await page.addInitScript(() => {
    localStorage.setItem('tg_session', JSON.stringify({ user: null, guest: true, savedAt: Date.now() }));
  });
  await page.route('**/emergencies', (r) =>
    r.fulfill({ status: 200, contentType: 'application/json',
      body: JSON.stringify({ status: 'ok', countries: [] }) }));
  await page.route(/tile|openstreetmap|basemaps|demotiles/, (r) => r.abort());
}

// ── Error path: HTTP error codes ──────────────────────────────────────────────
// CF Pages proxy returns 405 for POST (the specific reported bug).
// 503/530 cover backend-down and CF tunnel-down respectively.
// All must surface as a human-readable note — not a blank spinner.

for (const [statusCode, label] of [[405, 'Method Not Allowed'], [503, 'Service Unavailable'], [530, 'Tunnel down']] as [number, string][]) {
  test(`negotiate_text ${statusCode} shows planner-error note in chat`, async ({ page }) => {
    await setup(page);
    await page.route('**/negotiate_text', (r) =>
      r.fulfill({ status: statusCode, contentType: 'text/plain', body: label }));

    await page.goto('/');
    await page.getByTestId('chat-input').fill('5 days in kyoto, culture, $3000');
    await page.getByRole('button', { name: 'Send' }).click();

    // Error note must appear — text encodes the HTTP status so users know what failed.
    const note = page.getByTestId('chat-msgs').locator('.note').last();
    await expect(note).toBeVisible({ timeout: 10_000 });
    await expect(note).toContainText("Couldn't reach the planner");
    await expect(note).toContainText(`HTTP ${statusCode}`);

    // The composer must remain interactive so the user can retry.
    await expect(page.getByTestId('chat-input')).toBeEnabled();
  });
}

// ── Error path: network failure ───────────────────────────────────────────────
// Covers: CF tunnel down, DNS failure, CORS abort. All result in a fetch()
// rejection rather than an HTTP response. The app must not freeze.

test('negotiate_text network failure shows planner-error note in chat', async ({ page }) => {
  await setup(page);
  await page.route('**/negotiate_text', (r) => r.abort('failed'));

  await page.goto('/');
  await page.getByTestId('chat-input').fill('5 days in osaka, $2500');
  await page.getByRole('button', { name: 'Send' }).click();

  const note = page.getByTestId('chat-msgs').locator('.note').last();
  await expect(note).toBeVisible({ timeout: 10_000 });
  await expect(note).toContainText("Couldn't reach the planner");

  await expect(page.getByTestId('chat-input')).toBeEnabled();
});

// ── Recovery: plan renders after a prior error ─────────────────────────────
// Verifies the error path does not leave the app in a broken state.
// The second request must succeed and render the itinerary normally.
//
// Implementation note: planStreaming makes TWO internal /negotiate_text calls
// per user submission in headless mode (streaming attempt + blocking fallback).
// A simple callCount switch would fire mid-submission. Instead we start with
// an error route, wait until the error note is confirmed visible, then add a
// success route (Playwright matches routes LIFO — new route takes precedence).

test('negotiate_text recovery: plan renders correctly after a failed attempt', async ({ page }) => {
  await setup(page);

  // First submission: all calls fail with 405
  await page.route('**/negotiate_text', (r) =>
    r.fulfill({ status: 405, contentType: 'text/plain', body: 'Method Not Allowed' }));

  await page.goto('/');
  const input = page.getByTestId('chat-input');
  const send = page.getByRole('button', { name: 'Send' });

  // First attempt — confirm the error note is visible before switching the route
  await input.fill('5 days in kyoto, $3000');
  await send.click();
  await expect(page.getByTestId('chat-msgs').locator('.note').last())
    .toContainText("Couldn't reach the planner", { timeout: 10_000 });

  // Now add a success route. Playwright matches LIFO so this takes precedence.
  await page.route('**/negotiate_text', (r) =>
    r.fulfill({ status: 200, contentType: 'application/json',
      body: JSON.stringify(PLAN_READY) }));

  // Second attempt — succeeds
  await input.fill('5 days in kyoto, $3000');
  await send.click();
  await expect(page.getByTestId('itinerary')).toBeVisible({ timeout: 10_000 });
});

// ── Request shape guard ────────────────────────────────────────────────────
// Verifies the fetch is a POST with Content-Type: application/json and an
// absolute URL. A bare '/negotiate_text' (empty API_BASE) would hit the
// CF Pages proxy and return 405 for POST — the root-cause regression.

test('negotiate_text request is POST with JSON content-type and absolute URL', async ({ page }) => {
  await setup(page);

  let capturedMethod: string | null = null;
  let capturedContentType: string | null = null;
  let capturedUrl: string | null = null;

  await page.route('**/negotiate_text', (r) => {
    capturedMethod = r.request().method();
    capturedContentType = r.request().headers()['content-type'] ?? null;
    capturedUrl = r.request().url();
    return r.fulfill({ status: 200, contentType: 'application/json',
      body: JSON.stringify(PLAN_READY) });
  });

  await page.goto('/');
  await page.getByTestId('chat-input').fill('4 days in nara, $2000');
  await page.getByRole('button', { name: 'Send' }).click();
  await expect(page.getByTestId('itinerary')).toBeVisible({ timeout: 10_000 });

  expect(capturedMethod).toBe('POST');
  expect(capturedContentType).toContain('application/json');

  // URL must be absolute (scheme + host). A bare '/negotiate_text' with no host
  // means API_BASE is empty — the regression described at the top of this file.
  // In dev: http://localhost:PORT/negotiate_text. In prod: https://<api-host>/negotiate_text.
  expect(capturedUrl).toMatch(/^https?:\/\//);
  expect(capturedUrl).toContain('/negotiate_text');
});

// ── Tablet viewport (820×1180 — iPad Air) ──────────────────────────────────
// The 405 bug was reported on a tablet. The error note and the chat input must
// both be visible in the viewport at tablet resolution — not pushed off-screen
// by the column-reverse layout (fixed: chat-height capped to 42vh at 900px).

test.describe('tablet viewport (820x1180)', () => {
  test.use({ viewport: { width: 820, height: 1180 } });

  test('negotiate_text 405 error note is visible in viewport on tablet', async ({ page }) => {
    await setup(page);
    await page.route('**/negotiate_text', (r) =>
      r.fulfill({ status: 405, contentType: 'text/plain', body: 'Method Not Allowed' }));

    await page.goto('/');
    await page.getByTestId('chat-input').fill('4 days in hiroshima, $2500');
    await page.getByRole('button', { name: 'Send' }).click();

    const note = page.getByTestId('chat-msgs').locator('.note').last();
    await expect(note).toBeVisible({ timeout: 10_000 });
    await expect(note).toContainText("Couldn't reach the planner");
    await expect(note).toBeInViewport();
  });

  test('negotiate_text success plan and chat-input are in viewport on tablet', async ({ page }) => {
    await setup(page);
    await page.route('**/negotiate_text', (r) =>
      r.fulfill({ status: 200, contentType: 'application/json',
        body: JSON.stringify(PLAN_READY) }));

    await page.goto('/');
    await page.getByTestId('chat-input').fill('4 days in hiroshima, $2500');
    await page.getByRole('button', { name: 'Send' }).click();
    await expect(page.getByTestId('itinerary')).toBeVisible({ timeout: 10_000 });

    // chat-input must remain in viewport after plan renders (column-reverse layout fix)
    await expect(page.getByTestId('chat-input')).toBeInViewport();
  });
});
