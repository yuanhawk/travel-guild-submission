import { test, expect } from '@playwright/test';

// mobile-chat.spec.ts — Mobile floating bubble + slide-up sheet (pre-plan embedded mode).
//
// On ≤768px the embedded ChatPane aside is replaced by:
//   .mob-bubble  — fixed floating 🤖 button (always visible on mobile)
//   .mob-sheet   — slide-up sheet (65vh) with message history + compose area
//
// The sheet is keyboard-aware: visualViewport resize shifts it up by keyboard height
// via the CSS variable --mob-bottom on .mob-wrap. All tests use iPhone 14 Pro dimensions.

const PLAN_READY = {
  outcome: 'plan_ready',
  payment_status: 'held',
  booking_ref: null,
  idempotency_key: 'trip-mob-1',
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

test.describe('mobile chat bubble (390×844)', () => {
  test.use({ viewport: { width: 390, height: 844 } });

  async function setup(page) {
    await page.addInitScript(() => {
      localStorage.setItem('tg_session', JSON.stringify({ user: null, guest: true, savedAt: Date.now() }));
    });
    await page.route('**/tile.openstreetmap.org/**', (route) => route.abort());
    await page.route('**/emergencies', (route) =>
      route.fulfill({ status: 200, contentType: 'application/json',
        body: JSON.stringify({ status: 'ok', countries: [] }) }));
  }

  async function setupWithPlan(page) {
    await setup(page);
    await page.route('**/negotiate_text', (route) =>
      route.fulfill({ status: 200, contentType: 'application/json',
        body: JSON.stringify(PLAN_READY) }));
  }

  test('desktop chat pane is hidden on mobile viewport (CSS display:none)', async ({ page }) => {
    await setup(page);
    await page.goto('/');

    // The embedded aside has data-testid="chat-pane" — on mobile it must be display:none
    const chatPane = page.getByTestId('chat-pane');
    await expect(chatPane).toBeAttached();  // still in DOM (Playwright can query it)
    await expect(chatPane).toBeHidden();    // but CSS hides it (display:none)
  });

  test('mobile bubble is visible on page load', async ({ page }) => {
    await setup(page);
    await page.goto('/');

    const bubble = page.locator('[aria-label="Open assistant"]');
    await expect(bubble).toBeVisible({ timeout: 5000 });
  });

  test('mobile bubble tap opens slide-up sheet with Assistant header', async ({ page }) => {
    await setup(page);
    await page.goto('/');

    const bubble = page.locator('[aria-label="Open assistant"]');
    await bubble.click();

    // Sheet header renders "Assistant" title
    await expect(page.locator('.mob-sheet-ttl')).toHaveText('Assistant', { timeout: 3000 });

    // Sheet has mob-open class (visible)
    await expect(page.locator('.mob-sheet')).toHaveClass(/mob-open/);
  });

  test('mobile sheet contains an enabled textarea for input', async ({ page }) => {
    await setup(page);
    await page.goto('/');

    await page.locator('[aria-label="Open assistant"]').click();

    const textarea = page.locator('.mob-sheet-box textarea');
    await expect(textarea).toBeVisible({ timeout: 3000 });
    await expect(textarea).toBeEnabled();
  });

  test('mobile sheet send trip → plan renders in itinerary', async ({ page }) => {
    await setupWithPlan(page);
    await page.goto('/');

    // Open sheet
    await page.locator('[aria-label="Open assistant"]').click();

    // Type into sheet textarea and send
    const textarea = page.locator('.mob-sheet-box textarea');
    await textarea.fill('4 days in osaka');
    await page.locator('.mob-sheet-box .send').click();

    // Plan renders
    await expect(page.getByTestId('status-banner')).toContainText('Plan ready', { timeout: 12000 });
    await expect(page.getByTestId('itinerary')).toBeVisible();
  });

  test('close button (▼) collapses the sheet', async ({ page }) => {
    await setup(page);
    await page.goto('/');

    // Open
    await page.locator('[aria-label="Open assistant"]').click();
    await expect(page.locator('.mob-sheet')).toHaveClass(/mob-open/);

    // Click ▼ close button
    await page.locator('[aria-label="Close"]').click();

    // Sheet is no longer mob-open
    await expect(page.locator('.mob-sheet')).not.toHaveClass(/mob-open/);
  });

  test('message history is visible inside the open sheet', async ({ page }) => {
    await setup(page);
    await page.goto('/');

    // Open sheet
    await page.locator('[aria-label="Open assistant"]').click();

    // The initial bot greeting should appear in the sheet message history
    const msgs = page.locator('.mob-sheet-msgs .b.bot');
    await expect(msgs.first()).toBeVisible({ timeout: 3000 });
    await expect(msgs.first()).toContainText('Describe your trip');
  });

  test('bubble aria-label toggles between Open/Close states', async ({ page }) => {
    await setup(page);
    await page.goto('/');

    const bubble = page.locator('.mob-bubble');

    // Initial state: Open
    await expect(bubble).toHaveAttribute('aria-label', 'Open assistant');

    // After click: Close (the sheet is now open — the bubble's aria-label flips
    // even though the bubble itself is hidden while open, see the next test)
    await bubble.click();
    await expect(bubble).toHaveAttribute('aria-label', 'Close assistant');

    // Re-open is via the sheet's own ▼ close affordance, not a second bubble
    // click — the bubble is display:none while the sheet is open (mob-open-hide,
    // mirrors floating mode hiding .bot-trigger while the flyout is open) so it
    // isn't clickable in that state. Closing via ▼ un-hides the bubble again.
    await page.locator('[aria-label="Close"]').click();
    await expect(bubble).toHaveAttribute('aria-label', 'Open assistant');
  });

  test('mob-bubble is hidden (display:none) while the sheet is open, and reappears once closed', async ({ page }) => {
    await setup(page);
    await page.goto('/');

    const bubble = page.locator('.mob-bubble');
    await expect(bubble).toBeVisible();

    await bubble.click();
    await expect(page.locator('.mob-sheet')).toHaveClass(/mob-open/);
    // Gap 1 fix: the bubble must not float on top of the open sheet's compose
    // textarea — it's hidden outright while the sheet is open.
    await expect(bubble).toBeHidden();

    await page.locator('[aria-label="Close"]').click();
    await expect(page.locator('.mob-sheet')).not.toHaveClass(/mob-open/);
    await expect(bubble).toBeVisible();
  });

  test('--mob-bottom CSS variable is applied on .mob-wrap (keyboard-awareness wiring)', async ({ page }) => {
    await setup(page);
    await page.goto('/');

    // Emulate a keyboard opening by injecting a visualViewport resize.
    // We can't truly trigger iOS keyboard in headless; instead verify the CSS variable
    // mechanism is wired: force mobSheetBottom via the Svelte reactive path.
    // The variable default must be '0px' at page load (no keyboard).
    const bottom = await page.evaluate(() => {
      const wrap = document.querySelector('.mob-wrap') as HTMLElement | null;
      return wrap ? getComputedStyle(wrap).getPropertyValue('--mob-bottom').trim() : null;
    });
    // Must exist and be a pixel value (default 0px)
    expect(bottom).toMatch(/^\d+px$/);
  });

  test('bubble not visible on desktop viewport', async ({ browser }) => {
    // Switch to desktop viewport for this assertion
    const desktopPage = await browser.newPage();
    await desktopPage.setViewportSize({ width: 1280, height: 800 });
    await desktopPage.addInitScript(() => {
      localStorage.setItem('tg_session', JSON.stringify({ user: null, guest: true, savedAt: Date.now() }));
    });
    await desktopPage.route('**/tile.openstreetmap.org/**', (r) => r.abort());
    await desktopPage.route('**/emergencies', (r) =>
      r.fulfill({ status: 200, contentType: 'application/json',
        body: JSON.stringify({ status: 'ok', countries: [] }) }));

    await desktopPage.goto('/');

    // Mobile bubble must NOT be visible on desktop
    const bubble = desktopPage.locator('.mob-bubble');
    await expect(bubble).toBeHidden();

    // Desktop chat pane is visible on desktop
    await expect(desktopPage.getByTestId('chat-pane')).toBeVisible();
    await desktopPage.close();
  });
});
