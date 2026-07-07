import { test, expect } from '@playwright/test';

// cancel.spec.ts — Cancel/refund: POST /cancel → refund note + booking state cleared.
//
// The cancel button (data-testid="cancel-booking") appears only in booked state
// (result.booking_ref set, state === 'success'). It calls cancelTrip() → POST /cancel.
// On 'cancelled': refunded note in chat, result cleared.
// On 'already_cancelled': "already cancelled" note, result still cleared.
// The chat is collapsed after confirm, so we expand it (chat-toggle) to read the note.

const PLAN_READY = {
  outcome: 'plan_ready',
  payment_status: 'held',
  booking_ref: null,
  idempotency_key: 'trip-cn-1',
  package_total_with_fees_cents: 240000,
  total_budget_cents: 400000,
  wallet: { balance_cents: 500000, held_cents: 240000, debited: false },
  legs: [{ leg_id: 'leg-0', city: 'Osaka', checkin: '2026-12-01', checkout: '2026-12-04' }],
  day_plans: [{
    leg_id: 'leg-0', city: 'Osaka', num_days: 3,
    days: [{ day_index: 0, bad_weather: false, attractions: [], meals: {} }],
  }],
  risk_signals: { per_leg: [] },
};

const BOOKED = {
  ...PLAN_READY,
  outcome: 'success',
  payment_status: 'charged',
  booking_ref: 'BK-CN-1',
  wallet: { balance_cents: 260000, held_cents: 0, debited: true, debit_cents: 240000 },
};

async function planAndConfirm(page) {
  await page.addInitScript(() => {
    localStorage.setItem('tg_session', JSON.stringify({ user: null, guest: true, savedAt: Date.now() }));
  });
  await page.route('**/negotiate_text', (route) =>
    route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(PLAN_READY) }));
  await page.route('**/confirm', (route) =>
    route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(BOOKED) }));
  await page.route('**/tile.openstreetmap.org/**', (route) => route.abort());

  await page.goto('/');
  await page.getByTestId('chat-input').fill('3 days in osaka');
  await page.getByRole('button', { name: 'Send' }).click();
  await expect(page.getByTestId('confirm-book')).toBeVisible({ timeout: 8000 });
  await page.getByTestId('confirm-book').click();
  // Booked state: banner shows "Booked" and the cancel button appears
  await expect(page.getByTestId('status-banner')).toContainText('Booked', { timeout: 5000 });
  await expect(page.getByTestId('cancel-booking')).toBeVisible();
}

test('cancel booking → refund note in chat + booked state cleared', async ({ page }) => {
  await page.route('**/cancel', (route) =>
    route.fulfill({
      status: 200, contentType: 'application/json',
      body: JSON.stringify({
        outcome: 'cancelled',
        refunded_cents: 240000,         // full refund
        wallet_balance_cents: 500000,   // wallet restored
      }),
    }));

  await planAndConfirm(page);

  await page.getByTestId('cancel-booking').click();

  // Booked state cleared: banner + cancel button disappear (result=null → state='empty')
  await expect(page.getByTestId('status-banner')).toHaveCount(0, { timeout: 5000 });
  await expect(page.getByTestId('cancel-booking')).toHaveCount(0);

  // Expand the collapsed chat to read the refund note
  await page.getByTestId('chat-toggle').click();
  const msgs = page.getByTestId('chat-msgs');
  await expect(msgs).toContainText('refunded', { timeout: 5000 });
  await expect(msgs).toContainText('$2,400.00');   // centsToUsd(240000)
});

test('confirm & book → header wallet balance is debited (not left at the pre-booking amount)', async ({ page }) => {
  await planAndConfirm(page);

  // BOOKED mock carries wallet.balance_cents: 260000 (500000 held-plan balance − 240000 charged).
  // The header wallet input is bound to walletUsd and must reflect the post-charge balance.
  await expect(page.locator('.wallet input')).toHaveValue('2600');
});

test('cancel booking → already_cancelled → "already cancelled" note + state cleared', async ({ page }) => {
  await page.route('**/cancel', (route) =>
    route.fulfill({
      status: 200, contentType: 'application/json',
      body: JSON.stringify({ outcome: 'already_cancelled' }),
    }));

  await planAndConfirm(page);

  await page.getByTestId('cancel-booking').click();

  // Booked state cleared regardless of already_cancelled
  await expect(page.getByTestId('status-banner')).toHaveCount(0, { timeout: 5000 });

  // Expand chat to read the note
  await page.getByTestId('chat-toggle').click();
  await expect(page.getByTestId('chat-msgs')).toContainText('already cancelled', { timeout: 5000 });
});
