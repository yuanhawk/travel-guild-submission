import { test, expect } from '@playwright/test';

// mobile-overlap-keyboard.spec.ts — mobile-UX review, Gaps 1-3:
//   Gap 1: .mob-bubble must not sit on top of the open sheet's compose textarea.
//   Gap 2: the plan-active .flyout compose must shift above the iOS soft keyboard,
//          same as the pre-plan .mob-sheet already does (visualViewport --mob-bottom).
//   Gap 3: no mobile text input may render below 16px (iOS auto-zooms under that,
//          which also re-triggers Gap 2's layout-shift problem).

test.use({ viewport: { width: 375, height: 667 }, hasTouch: true, isMobile: true });

// Mock visualViewport BEFORE app scripts run so we can simulate the soft keyboard.
async function setupWithKeyboardMock(page, planBody?: unknown) {
  await page.addInitScript(() => {
    localStorage.setItem('tg_session', JSON.stringify({ user: null, guest: true, savedAt: Date.now() }));
    const listeners: Array<() => void> = [];
    // @ts-expect-error test-only global
    window.__vv = {
      height: window.innerHeight, offsetTop: 0,
      addEventListener: (_t: string, fn: () => void) => listeners.push(fn),
      removeEventListener: () => {},
    };
    // @ts-expect-error test-only global
    window.__openKeyboard = (kb: number) => {
      // @ts-expect-error test-only global
      window.__vv.height = window.innerHeight - kb;
      listeners.forEach((fn) => fn());
    };
    Object.defineProperty(window, 'visualViewport', {
      // @ts-expect-error test-only global
      get: () => window.__vv,
    });
  });
  await page.route(/tile|openstreetmap|basemaps|demotiles/, (r) => r.abort());
  await page.route('**/emergencies', (r) => r.fulfill({ status: 200,
    contentType: 'application/json', body: JSON.stringify({ status: 'ok', countries: [] }) }));
  if (planBody) {
    await page.route('**/negotiate_text', (r) => r.fulfill({ status: 200,
      contentType: 'application/json', body: JSON.stringify(planBody) }));
  }
}

// Reused verbatim from e2e/mobile-chat.spec.ts (kept in sync there).
const PLAN_READY = {
  outcome: 'plan_ready',
  payment_status: 'held',
  booking_ref: null,
  idempotency_key: 'trip-mob-overlap-1',
  package_total_with_fees_cents: 120000,
  total_budget_cents: 300000,
  wallet: { balance_cents: 400000, held_cents: 120000, debited: false },
  legs: [{ leg_id: 'leg-0', city: 'Osaka', checkin: '2026-11-01', checkout: '2026-11-04' }],
  day_plans: [{
    leg_id: 'leg-0', city: 'Osaka', num_days: 3,
    days: [{ day_index: 0, bad_weather: false, attractions: [], meals: {} }],
  }],
  risk_signals: { per_leg: [] },
};

async function planViaSheet(page) {
  await page.locator('.mob-bubble').tap();
  await page.locator('.mob-sheet-box textarea').fill('3 days osaka $1200');
  await page.locator('.mob-sheet-box .send').tap();
  await expect(page.getByTestId('status-banner')).toContainText('Plan ready', { timeout: 12000 });
  await expect(page.getByTestId('assistant-trigger')).toBeVisible();
}

// Gap 1: bubble must not cover the open sheet's compose textarea.
test('mob-bubble does not overlap the open sheet compose textarea', async ({ page }) => {
  await setupWithKeyboardMock(page);
  await page.goto('/');
  await page.locator('.mob-bubble').tap();
  await expect(page.locator('.mob-sheet')).toHaveClass(/mob-open/);

  const bubble = page.locator('.mob-bubble');
  const ta = page.locator('.mob-sheet-box textarea');
  await expect(ta).toBeVisible();
  const bubbleBox = await bubble.boundingBox();
  const taBox = await ta.boundingBox();

  // The fix hides the bubble outright while the sheet is open (mirroring the
  // floating-mode .mob-open-hide mechanism) — boundingBox() is null when hidden.
  // If some future change makes it visible-but-shifted instead, still assert
  // no overlap rather than assuming either strategy.
  if (bubbleBox === null) {
    expect(bubbleBox).toBeNull();
  } else {
    const overlaps = bubbleBox.x < taBox!.x + taBox!.width && bubbleBox.x + bubbleBox.width > taBox!.x &&
                     bubbleBox.y < taBox!.y + taBox!.height && bubbleBox.y + bubbleBox.height > taBox!.y;
    expect(overlaps, 'floating bubble covers the sheet textarea').toBe(false);
  }
});

// Gap 2: plan-active flyout compose must shift above the soft keyboard.
test('plan-active flyout compose stays above the soft keyboard (iOS)', async ({ page }) => {
  await setupWithKeyboardMock(page, PLAN_READY);
  await page.goto('/');
  await planViaSheet(page);
  await page.getByTestId('assistant-trigger').tap();
  await expect(page.locator('.flyout')).toHaveClass(/open/);

  const KB = 300;
  await page.evaluate((kb) => (window as any).__openKeyboard(kb), KB);
  await page.waitForTimeout(300);

  const input = await page.locator('.flyout [data-testid="chat-input"]').boundingBox();
  expect(input).not.toBeNull();
  const visibleBottom = 667 - KB;
  expect(input!.y + input!.height,
    'flyout compose box is under the keyboard'
  ).toBeLessThanOrEqual(visibleBottom);
});

// Gap 3: no text input rendered on mobile may be <16px (iOS zoom-on-focus).
test('every visible mobile text input has font-size >= 16px', async ({ page }) => {
  await setupWithKeyboardMock(page, PLAN_READY);
  await page.goto('/');
  await planViaSheet(page);
  await page.getByTestId('assistant-trigger').tap();
  await expect(page.locator('.flyout')).toHaveClass(/open/);
  const small = await page.evaluate(() =>
    [...document.querySelectorAll('textarea, input[type="text"], input:not([type])')]
      .filter((el) => (el as HTMLElement).offsetParent !== null)
      .map((el) => ({ cls: String(el.className).slice(0, 40),
        fs: parseFloat(getComputedStyle(el).fontSize) }))
      .filter((r) => r.fs < 16));
  expect(small, `inputs under 16px trigger iOS auto-zoom on focus`).toEqual([]);
});
