import { test, expect } from '@playwright/test';

// assistant-flyout.spec.ts — UI increment #2: floating 🤖 emoji assistant.
//
// Strategy:
//   • Before a plan renders → chat is EMBEDDED (no bot trigger visible).
//   • After a plan renders → chat collapses to fixed 🤖 emoji (assistant-trigger).
//   • Hover assistant-trigger → flyout opens.
//   • 📌 pin button keeps flyout open on mouse-leave.
//   • Planning flow works end-to-end (chat-input pre-plan, confirm button post-plan).

const PLAN_READY = {
  outcome: 'plan_ready',
  payment_status: 'held',
  booking_ref: null,
  idempotency_key: 'trip-af-1',
  package_total_with_fees_cents: 180000,
  total_budget_cents: 400000,
  wallet: { balance_cents: 500000, held_cents: 180000, debited: false },
  legs: [{ leg_id: 'leg-0', city: 'Kyoto', checkin: '2026-11-01', checkout: '2026-11-04' }],
  day_plans: [{
    leg_id: 'leg-0', city: 'Kyoto', num_days: 3,
    days: [{ day_index: 0, bad_weather: false, attractions: [], meals: {} }],
  }],
  risk_signals: { per_leg: [] },
};

async function setupMocks(page) {
  await page.addInitScript(() => {
    localStorage.setItem('tg_session', JSON.stringify({ user: null, guest: true, savedAt: Date.now() }));
  });
  await page.route('**/negotiate_text', (route) =>
    route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(PLAN_READY) }));
  await page.route('**/tile.openstreetmap.org/**', (route) => route.abort());
  await page.route('**/emergencies', (route) =>
    route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ status: 'ok', countries: [] }) }));
}

test('initial load: no bot trigger visible, chat-input is actionable (pre-plan)', async ({ page }) => {
  await setupMocks(page);
  await page.goto('/');

  // Before a plan: embedded chat is shown — bot trigger should NOT be in the DOM
  await expect(page.getByTestId('assistant-trigger')).toHaveCount(0);

  // chat-input must be fully actionable (visible, not disabled) so specs can .fill()
  await expect(page.getByTestId('chat-input')).toBeVisible();
  await expect(page.getByTestId('chat-input')).toBeEnabled();
});

test('after plan renders: bot emoji visible, flyout opens on hover', async ({ page }) => {
  await setupMocks(page);
  await page.goto('/');

  // Submit a request to trigger plan rendering
  await page.getByTestId('chat-input').fill('3 days in kyoto');
  await page.getByRole('button', { name: 'Send' }).click();

  // Wait for the plan to render
  await expect(page.getByTestId('status-banner')).toContainText('Plan ready', { timeout: 10_000 });

  // Bot trigger now appears (floating emoji)
  const trigger = page.getByTestId('assistant-trigger');
  await expect(trigger).toBeVisible();

  // Flyout is hidden by default (not open)
  const flyout = page.locator('[data-testid="assistant-flyout"]');
  if (await flyout.count() > 0) {
    await expect(flyout).not.toHaveClass(/\bopen\b/);
  }

  // Hover over trigger → flyout opens
  await trigger.hover();
  // Give the CSS transition time to complete
  await page.waitForTimeout(400);
  // chat-input inside flyout becomes visible
  await expect(page.getByTestId('chat-input')).toBeVisible({ timeout: 5_000 });
});

test('pin button keeps flyout open on mouse-leave', async ({ page }) => {
  await setupMocks(page);
  await page.goto('/');

  await page.getByTestId('chat-input').fill('3 days in kyoto');
  await page.getByRole('button', { name: 'Send' }).click();
  await expect(page.getByTestId('status-banner')).toContainText('Plan ready', { timeout: 10_000 });

  // Hover to open flyout
  const trigger = page.getByTestId('assistant-trigger');
  await trigger.hover();
  await page.waitForTimeout(400);
  await expect(page.getByTestId('chat-input')).toBeVisible({ timeout: 5_000 });

  // Click the pin button
  const pinBtn = page.getByTestId('assistant-pin');
  await expect(pinBtn).toBeVisible();
  await pinBtn.click();

  // Move mouse away from both trigger and flyout
  await page.mouse.move(800, 300);

  // Flyout should stay open (pinned)
  await page.waitForTimeout(500);
  await expect(page.getByTestId('chat-input')).toBeVisible();

  // Unpin: click pin again → flyout closes after delay
  await pinBtn.click();
  await page.mouse.move(800, 300);
  await page.waitForTimeout(600);
  // chat-input inside closed flyout is not visible
  await expect(page.getByTestId('chat-input')).not.toBeVisible();
});

test('close button: a single click closes the flyout immediately (no double-tap needed)', async ({ page }) => {
  await setupMocks(page);
  await page.goto('/');

  await page.getByTestId('chat-input').fill('3 days in kyoto');
  await page.getByRole('button', { name: 'Send' }).click();
  await expect(page.getByTestId('status-banner')).toContainText('Plan ready', { timeout: 10_000 });

  // Hover to open flyout
  const trigger = page.getByTestId('assistant-trigger');
  await trigger.hover();
  await page.waitForTimeout(400);
  await expect(page.getByTestId('chat-input')).toBeVisible({ timeout: 5_000 });

  const flyout = page.locator('[data-testid="assistant-flyout"]');
  await expect(flyout).toHaveClass(/\bopen\b/);

  // ONE click on the close affordance — this is the touch-safe path (there's no
  // mouseleave on tap, so this must not depend on any hover-based timer).
  const closeBtn = page.getByTestId('assistant-close');
  await expect(closeBtn).toBeVisible();
  await closeBtn.click();

  // Closed after exactly one click — no second interaction, no waiting out the
  // 350ms scheduleClose hover-leave timer.
  await expect(flyout).not.toHaveClass(/\bopen\b/);
  await expect(page.getByTestId('chat-input')).not.toBeVisible();
});

test('close button closes the flyout even while pinned — independent of Pin', async ({ page }) => {
  await setupMocks(page);
  await page.goto('/');

  await page.getByTestId('chat-input').fill('3 days in kyoto');
  await page.getByRole('button', { name: 'Send' }).click();
  await expect(page.getByTestId('status-banner')).toContainText('Plan ready', { timeout: 10_000 });

  const trigger = page.getByTestId('assistant-trigger');
  await trigger.hover();
  await page.waitForTimeout(400);
  await expect(page.getByTestId('chat-input')).toBeVisible({ timeout: 5_000 });

  // Pin it open
  const pinBtn = page.getByTestId('assistant-pin');
  await pinBtn.click();
  await expect(pinBtn).toHaveClass(/\bpinned\b/);

  // Move the mouse away — Pin does its job, keeping the flyout open with no hover.
  // (This is Pin's contract, exercised independently of Close.)
  await page.mouse.move(800, 300);
  await page.waitForTimeout(500);
  await expect(page.getByTestId('chat-input')).toBeVisible();

  // Close must still win over Pin, in a single click.
  const flyout = page.locator('[data-testid="assistant-flyout"]');
  await page.getByTestId('assistant-close').click();
  await expect(flyout).not.toHaveClass(/\bopen\b/);

  // Close also unpins, so a later re-open reverts to default hover-follow behavior.
  await expect(pinBtn).not.toHaveClass(/\bpinned\b/);
  await expect(pinBtn).toContainText('Pin');
});

test('planning still works end-to-end: fill pre-plan, plan renders, confirm available', async ({ page }) => {
  await setupMocks(page);
  await page.goto('/');

  // Pre-plan: chat-input is in embedded mode (actionable)
  await expect(page.getByTestId('chat-input')).toBeVisible();
  await page.getByTestId('chat-input').fill('3 days in kyoto');
  await page.getByRole('button', { name: 'Send' }).click();

  // Plan renders — confirm button and itinerary appear
  await expect(page.getByTestId('status-banner')).toContainText('Plan ready', { timeout: 10_000 });
  await expect(page.getByTestId('confirm-book')).toBeVisible();
  await expect(page.getByTestId('itinerary')).toBeVisible();

  // Bot emoji trigger is now present
  await expect(page.getByTestId('assistant-trigger')).toBeVisible();
});

test('tablet tier (601-1100px): flyout is width-capped with a visible right margin, coordinated with App.svelte grid breakpoint', async ({ page }) => {
  await page.setViewportSize({ width: 800, height: 900 });
  await setupMocks(page);
  await page.goto('/');

  await page.getByTestId('chat-input').fill('3 days in kyoto');
  await page.getByRole('button', { name: 'Send' }).click();
  await expect(page.getByTestId('status-banner')).toContainText('Plan ready', { timeout: 10_000 });

  const trigger = page.getByTestId('assistant-trigger');
  await trigger.hover();
  await page.waitForTimeout(400);
  await expect(page.getByTestId('chat-input')).toBeVisible({ timeout: 5_000 });

  const flyout = page.locator('[data-testid="assistant-flyout"]');
  const box = await flyout.boundingBox();
  expect(box).not.toBeNull();
  // Width must be capped well below the desktop 300px+82px-offset footprint, and
  // must leave a real gap before the viewport's right edge (the "peek" margin) —
  // NOT flush/full-bleed the way the ≤600px phone tier is.
  // +2px tolerance for the 1px border on each side (content-box sizing).
  expect(box!.width).toBeLessThanOrEqual(382);
  expect(800 - (box!.x + box!.width)).toBeGreaterThanOrEqual(70);
});

test('auto-surface: a post-plan assistant message pops the flyout open (no hover)', async ({ page }) => {
  await setupMocks(page);
  // reconcile on /confirm → App pushes a "tap Confirm again" note AFTER the plan rendered
  await page.route('**/confirm', (route) =>
    route.fulfill({ status: 200, contentType: 'application/json',
      body: JSON.stringify({ outcome: 'cannot_satisfy', reason: 'commit_errored', needs_reconciliation: true }) }));
  await page.goto('/');

  await page.getByTestId('chat-input').fill('3 days in kyoto');
  await page.getByRole('button', { name: 'Send' }).click();
  await expect(page.getByTestId('status-banner')).toContainText('Plan ready', { timeout: 10_000 });

  // After plan render the flyout is CLOSED — the plan-summary message must NOT auto-pop it.
  const flyout = page.locator('[data-testid="assistant-flyout"]');
  await expect(flyout).not.toHaveClass(/\bopen\b/);

  // Trigger a NEW post-plan assistant message WITHOUT hovering the trigger.
  await page.getByTestId('confirm-book').click();

  // The flyout must AUTO-OPEN to surface the critical note (no hover happened).
  await expect(flyout).toHaveClass(/\bopen\b/);
  await expect(page.getByText(/Tap Confirm again/i)).toBeVisible();
});

test('desktop-wide (>768px): clicking the trigger then moving the mouse away still closes the flyout', async ({ page }) => {
  // Regression for the width-gate added after a review found the trigger-hide
  // oscillation fix (suppressNextTriggerLeave) was applied regardless of viewport
  // width, which also swallowed a REAL desktop click-then-leave close — .mob-open-hide
  // only ever applies display:none at <=768px, so the guard must not fire above it.
  await page.setViewportSize({ width: 1280, height: 800 });
  await setupMocks(page);
  await page.goto('/');

  await page.getByTestId('chat-input').fill('3 days in kyoto');
  await page.getByRole('button', { name: 'Send' }).click();
  await expect(page.getByTestId('status-banner')).toContainText('Plan ready', { timeout: 10_000 });

  const trigger = page.getByTestId('assistant-trigger');
  const flyout = page.locator('[data-testid="assistant-flyout"]');
  await trigger.click();
  await expect(flyout).toHaveClass(/\bopen\b/);

  // Move the mouse off the trigger (a genuine leave, not synthetic — trigger keeps
  // display:block at this width, so no synthetic mouseleave/mouseenter pair fires).
  await page.mouse.move(10, 10);
  await page.waitForTimeout(500); // scheduleClose's 350ms timer + margin
  await expect(flyout).not.toHaveClass(/\bopen\b/);
});

test('mobile-narrow (<=768px): clicking the trigger keeps the flyout open despite the synthetic mouseleave/mouseenter pair', async ({ page }) => {
  // The oscillation this guards against only happens at <=768px, where
  // userOpenedFlyout=true synchronously applies .mob-open-hide (display:none) to
  // the trigger under a real mouse cursor. Below 768px, pre-plan chat is the
  // .mob-bubble/.mob-sheet flow (not the embedded chat-input) — see mobile-chat.spec.ts.
  await page.setViewportSize({ width: 600, height: 800 });
  await setupMocks(page);
  await page.goto('/');

  await page.locator('[aria-label="Open assistant"]').click();
  await page.locator('.mob-sheet-box textarea').fill('3 days in kyoto');
  await page.locator('.mob-sheet-box .send').click();
  await expect(page.getByTestId('status-banner')).toContainText('Plan ready', { timeout: 10_000 });

  const trigger = page.getByTestId('assistant-trigger');
  const flyout = page.locator('[data-testid="assistant-flyout"]');
  await trigger.click();
  await expect(flyout).toHaveClass(/\bopen\b/);

  // Give the synthetic mouseleave/mouseenter pair (from the trigger going
  // display:none under the stationary cursor) time to fire and settle.
  await page.waitForTimeout(500);
  await expect(flyout).toHaveClass(/\bopen\b/);
  await expect(trigger).toBeHidden();
});
