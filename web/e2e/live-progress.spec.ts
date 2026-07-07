import { test, expect } from '@playwright/test';

// live-progress.spec.ts — SSE live agent-stream / LiveProgress board.
//
// DEGRADE-FALLBACK IN USE: full EventSource/SSE mocking is brittle in Playwright
// (the browser constructor can't be reliably intercepted at the transport level in
// headless Chromium without a service worker). Instead we mock /negotiate_text so
// that the first call ({stream:true}) returns an empty body with NO stream_id →
// planStreaming degrades to a blocking /negotiate_text ({stream:false}) per its
// documented fallback contract. A 400ms delay on the degrade call keeps loading=true
// long enough for LiveProgress to mount and be visible in the DOM. This tests both:
//   (a) LiveProgress mounts whenever loading=true and progress!=null (the board renders)
//   (b) the plan renders correctly from the degrade path

const PLAN_READY = {
  outcome: 'plan_ready',
  payment_status: 'held',
  booking_ref: null,
  idempotency_key: 'trip-lp-1',
  package_total_with_fees_cents: 120000,
  total_budget_cents: 300000,
  wallet: { balance_cents: 500000, held_cents: 120000, debited: false },
  legs: [{ leg_id: 'leg-0', city: 'Kyoto', checkin: '2026-11-01', checkout: '2026-11-04' }],
  day_plans: [{
    leg_id: 'leg-0', city: 'Kyoto', num_days: 3,
    days: [{
      day_index: 0, bad_weather: false,
      attractions: [{ name: 'Kinkaku-ji', category: 'tourism=attraction', lat: 35.039, lon: 135.729, fee: null }],
      meals: { breakfast: { name: 'Ippodo Tea', category: 'amenity=cafe', cuisine: 'tea' } },
    }],
  }],
  risk_signals: { per_leg: [{ leg_id: 'leg-0', alert_tier: 'LOW', decisions: {} }] },
};

test.describe('SSE live agent-stream / LiveProgress', () => {
  test.beforeEach(async ({ page }) => {
    await page.addInitScript(() => {
      localStorage.setItem('tg_session', JSON.stringify({ user: null, guest: true, savedAt: Date.now() }));
    });
    await page.route('**/tile.openstreetmap.org/**', (r) => r.abort());
  });

  test('LiveProgress board mounts while loading (degrade-path fallback)', async ({ page }) => {
    // First call ({stream:true}) → no stream_id → triggers degrade.
    // Second call ({stream:false}) → delayed 400ms so LiveProgress is observable in DOM.
    let callCount = 0;
    await page.route('**/negotiate_text', async (route) => {
      callCount++;
      if (callCount === 1) {
        // No stream_id → planStreaming degrades
        await route.fulfill({
          status: 200, contentType: 'application/json',
          body: JSON.stringify({}),
        });
      } else {
        // Degrade call: delay so LiveProgress has time to render
        await new Promise<void>((resolve) => setTimeout(resolve, 400));
        await route.fulfill({
          status: 200, contentType: 'application/json',
          body: JSON.stringify(PLAN_READY),
        });
      }
    });

    await page.goto('/');
    await page.getByTestId('chat-input').fill('5 days in kyoto, temples');
    await page.getByRole('button', { name: 'Send' }).click();

    // LiveProgress must mount while the degrade call is pending (loading=true, progress!=null)
    await expect(page.getByTestId('live-progress')).toBeVisible({ timeout: 5000 });

    // Phase text is rendered from initProgress()
    const phase = page.getByTestId('live-phase');
    await expect(phase).toBeVisible();
    await expect(phase).not.toBeEmpty();

    // All 12 agent rows from the roster are rendered as pending/running
    // (no events — all remain in initial state from initProgress)
    await expect(page.locator('[data-testid^="agent-row-"]')).toHaveCount(12);

    // After the degrade resolves, LiveProgress disappears and the plan renders
    await expect(page.getByTestId('itinerary')).toBeVisible({ timeout: 8000 });
    await expect(page.getByTestId('live-progress')).toHaveCount(0);
  });

  test('plan renders correctly after degrade (all agents board → itinerary)', async ({ page }) => {
    // Degrade without artificial delay — just verify the final plan renders correctly
    let callCount = 0;
    await page.route('**/negotiate_text', async (route) => {
      callCount++;
      if (callCount === 1) {
        await route.fulfill({
          status: 200, contentType: 'application/json',
          body: JSON.stringify({}),         // no stream_id → degrade
        });
      } else {
        await route.fulfill({
          status: 200, contentType: 'application/json',
          body: JSON.stringify(PLAN_READY),
        });
      }
    });

    await page.goto('/');
    await page.getByTestId('chat-input').fill('5 days in kyoto');
    await page.getByRole('button', { name: 'Send' }).click();

    // Plan renders correctly from the degrade path
    await expect(page.getByTestId('status-banner')).toContainText('Plan ready', { timeout: 8000 });
    await expect(page.getByTestId('itinerary')).toContainText('Kyoto');
    await expect(page.getByTestId('itinerary')).toContainText('Kinkaku-ji');

    // confirm-book present — degrade path preserves the full plan contract
    await expect(page.getByTestId('confirm-book')).toBeVisible();
  });
});
