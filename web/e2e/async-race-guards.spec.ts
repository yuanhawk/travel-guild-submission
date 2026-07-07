import { test, expect } from '@playwright/test';

// async-race-guards.spec.ts — regression coverage for the `sessionSeq` race-condition
// guard added to App.svelte (pickUser / openPrefs / switchUser / continueGuest all bump
// or check a shared `sessionSeq` generation counter). Without the guard, a slow
// in-flight session/prefs fetch that resolves AFTER a newer session op has already won
// would silently overwrite the newer state with stale data belonging to the wrong user —
// a cross-persona leak.
//
// Playwright can't literally "click twice in 50ms" reliably, so both tests use the house
// pattern: mock the slow endpoint with an artificial delay to open a real race window,
// fire the slow action, then — WITHOUT awaiting its UI settling — immediately fire the
// racing action, then assert the FINAL state is the racer's result, not the stale one.

const DEMO_A = {
  user_id: 'demo-mei', display_name: 'Mei', persona: 'foodie',
  nationality: 'SG', home_currency: 'SGD', wallet_balance_cents: 500000,
};
const DEMO_B = {
  user_id: 'demo-alex', display_name: 'Alex', persona: 'hiker',
  nationality: 'US', home_currency: 'USD', wallet_balance_cents: 500000,
};

async function mockCommon(page): Promise<void> {
  await page.route('**/emergencies', (r) =>
    r.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ status: 'ok', countries: [] }) }));
  await page.route(/tile|openstreetmap|basemaps|demotiles/, (r) => r.abort());
}

// ── 1) openPrefs vs switchUser — the originally-reported bug ────────────────
//
// openPrefs() captures mySeq = ++sessionSeq, awaits GET /preferences, then only
// applies the result + flips view='prefs' if mySeq === sessionSeq. switchUser()
// bumps sessionSeq (and reqSeq) synchronously and flips view='picker' immediately.
// A slow /preferences response landing after logout must NOT drag the app back
// into user A's Preferences screen.

test('open Preferences (slow fetch) then immediately log out → lands on session picker, not stale Preferences', async ({ page }) => {
  await page.addInitScript((u) => {
    localStorage.setItem('tg_session', JSON.stringify({ user: u, guest: true, savedAt: Date.now() }));
  }, DEMO_A);
  await mockCommon(page);

  // GET /preferences?user_id=demo-mei — artificially slow to open the race window.
  await page.route('**/preferences**', async (route) => {
    await new Promise((r) => setTimeout(r, 500));
    await route.fulfill({
      status: 200, contentType: 'application/json',
      body: JSON.stringify({ ...DEMO_A, prefs: { dietary: 'vegetarian' } }),
    });
  });
  // switchUser() re-fetches the demo user list (POST /session) on the way back to the picker.
  await page.route('**/session', (r) =>
    r.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ demo_users: [DEMO_A, DEMO_B] }) }));

  await page.goto('/');
  await expect(page.getByTestId('prefs-link')).toBeVisible();

  // Fire the slow action (opens Preferences → in-flight GET /preferences)...
  await page.getByTestId('prefs-link').click();
  // ...then immediately race it with logout, WITHOUT waiting for the fetch to settle.
  await page.getByTestId('switch-user').click();

  // The race loser must never win: session picker now, and stays that way
  // once the slow /preferences response lands ~500ms later.
  await expect(page.getByTestId('session-picker')).toBeVisible({ timeout: 2000 });
  await page.waitForTimeout(700);   // let the delayed /preferences response resolve
  await expect(page.getByTestId('session-picker')).toBeVisible();
  await expect(page.getByTestId('preferences')).toHaveCount(0);
});

// ── 2) pickUser vs pickUser — double-clicking two different demo users ──────
//
// A same-account relogin restores its held plan via a GET /trips/{key} fetch inside
// pickUser(); that fetch is guarded by mySeq === sessionSeq before it's applied.
// Racing it with a second pickUser() for a DIFFERENT persona (no restore, so it
// resolves synchronously) must leave the app on persona B's clean state, not have
// persona A's restored trip clobber it in when the slow fetch finally lands.

test('log in as A (slow held-trip restore) then immediately log in as a different user B → ends on B, A\'s stale restore never lands', async ({ page }) => {
  const HELD_TRIP = {
    outcome: 'plan_ready',
    payment_status: 'held',
    booking_ref: null,
    idempotency_key: 'trip-race-1',
    package_total_with_fees_cents: 200000,
    total_budget_cents: 400000,
    wallet: { balance_cents: 500000, held_cents: 200000, debited: false },
    legs: [{ leg_id: 'leg-0', city: 'Hanoi', checkin: '2026-10-01', checkout: '2026-10-04' }],
    day_plans: [{
      leg_id: 'leg-0', city: 'Hanoi', num_days: 3,
      days: [{ day_index: 0, bad_weather: false, attractions: [], meals: {} }],
    }],
    risk_signals: { per_leg: [] },
  };

  // Preseed a session for A that already has a held plan, so switchUser() snapshots
  // it into priorSessionOnLogout and a same-account relogin has something to restore.
  await page.addInitScript((args) => {
    localStorage.setItem('tg_session', JSON.stringify({
      user: args.a, guest: true,
      activePlan: { session_id: 'sess-race-1', idempotency_key: 'trip-race-1' },
      savedAt: Date.now(),
    }));
  }, { a: DEMO_A });
  await mockCommon(page);

  await page.route('**/session', (r) =>
    r.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ demo_users: [DEMO_A, DEMO_B] }) }));
  // GET /trips/trip-race-1 — used by BOTH onMount's reload-restore and pickUser(A)'s
  // same-account restore. Artificially slow so pickUser(A)'s restore is still in
  // flight when we race it with pickUser(B).
  await page.route('**/trips/**', async (route) => {
    await new Promise((r) => setTimeout(r, 500));
    await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(HELD_TRIP) });
  });

  await page.goto('/');
  // onMount's reload-restore also hits the (slow) /trips/{key} route — wait it out.
  await expect(page.getByTestId('prefs-link')).toBeVisible({ timeout: 3000 });
  await expect(page.getByTestId('status-banner')).toContainText('Plan ready', { timeout: 3000 });

  // Log out (captures priorSessionOnLogout = A's session incl. the held plan) → picker.
  await page.getByTestId('switch-user').click();
  await expect(page.getByTestId('session-picker')).toBeVisible();

  // Log back in as A → same-account relogin → triggers the slow GET /trips/{key} restore.
  await page.getByTestId(`user-${DEMO_A.user_id}`).click();
  // Immediately race it by logging in as a DIFFERENT persona (B has no priorPlan match,
  // so pickUser(B) resolves synchronously with no network wait).
  await page.getByTestId(`user-${DEMO_B.user_id}`).click();

  // Final state must be B's clean login, not A's restored trip.
  await expect(page.getByTestId('prefs-link')).toContainText(DEMO_B.user_id, { timeout: 2000 });
  await expect(page.getByTestId('guide-panel')).toBeVisible();
  await expect(page.getByTestId('status-banner')).toHaveCount(0);
  await expect(page.getByTestId('chat-msgs')).not.toContainText('Welcome back');

  // Let A's slow restore resolve in the background — it must be dropped (stale
  // sessionSeq), not silently reinstate A's booked banner/itinerary under B's login.
  await page.waitForTimeout(700);
  await expect(page.getByTestId('prefs-link')).toContainText(DEMO_B.user_id);
  await expect(page.getByTestId('status-banner')).toHaveCount(0);
  await expect(page.getByTestId('itinerary')).toHaveCount(0);
});

// ── 3) confirmBook vs "New trip" — the money/booking-state race ────────────
//
// confirmBook() captures mySeq = reqSeq (NOT incremented — `confirming` already
// blocks self-reentry; this capture only catches a CONCURRENT logout/new-plan),
// awaits POST /confirm, then only applies the booked envelope + pushes the
// "Booked ✓" message if mySeq === reqSeq. newTrip() bumps reqSeq synchronously
// and clears result/messages immediately. A slow /confirm response landing after
// "Start new trip" must NOT resurrect the booked banner or silently charge/re-book
// the UI out from under the user who already moved on.

const PLAN_READY_CONFIRM = {
  outcome: 'plan_ready',
  payment_status: 'held',
  booking_ref: null,
  idempotency_key: 'trip-confirm-race-1',
  package_total_with_fees_cents: 200000,
  total_budget_cents: 400000,
  wallet: { balance_cents: 500000, held_cents: 200000, debited: false },
  legs: [{ leg_id: 'leg-0', city: 'Hanoi', checkin: '2026-10-01', checkout: '2026-10-04' }],
  day_plans: [{
    leg_id: 'leg-0', city: 'Hanoi', num_days: 3,
    days: [{ day_index: 0, bad_weather: false, attractions: [], meals: {} }],
  }],
  risk_signals: { per_leg: [] },
};

const BOOKED_STALE = {
  ...PLAN_READY_CONFIRM,
  outcome: 'success',
  payment_status: 'charged',
  booking_ref: 'BK-STALE-RACE-1',
  wallet: { balance_cents: 300000, held_cents: 0, debited: true, debit_cents: 200000 },
};

test('confirm booking (slow response) then immediately "Start new trip" → stale confirm never re-books the UI', async ({ page }) => {
  await page.addInitScript((args) => {
    localStorage.setItem('tg_session', JSON.stringify({
      user: args.a, guest: true,
      activePlan: { session_id: 'sess-confirm-race-1', idempotency_key: 'trip-confirm-race-1' },
      savedAt: Date.now(),
    }));
  }, { a: DEMO_A });
  await mockCommon(page);

  // onMount's restore fetch — fast, not part of the race under test.
  await page.route('**/trips/**', (r) =>
    r.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(PLAN_READY_CONFIRM) }));
  // POST /confirm — artificially slow to open the race window.
  await page.route('**/confirm', async (route) => {
    await new Promise((r) => setTimeout(r, 500));
    await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(BOOKED_STALE) });
  });

  await page.goto('/');
  await expect(page.getByTestId('confirm-book')).toBeVisible({ timeout: 3000 });
  await expect(page.getByTestId('status-banner')).toContainText('Plan ready', { timeout: 3000 });

  // Fire the slow confirm (opens Confirm & Book → in-flight POST /confirm)...
  await page.getByTestId('confirm-book').click();
  // ...then immediately race it with "Start new trip", WITHOUT waiting for /confirm to settle.
  await page.getByTestId('new-trip').click();

  // The race loser must never win: back to the empty/pre-plan guide state now, and
  // stays that way once the slow /confirm response lands ~500ms later — no
  // resurrected "Booked ✓" banner, no phantom charge message.
  await expect(page.getByTestId('guide-panel')).toBeVisible({ timeout: 2000 });
  await expect(page.getByTestId('status-banner')).toHaveCount(0);
  await page.waitForTimeout(700);   // let the delayed /confirm response resolve
  await expect(page.getByTestId('status-banner')).toHaveCount(0);
  await expect(page.getByTestId('chat-msgs')).not.toContainText('Booked ✓');
});

// ── 4) cancelBooking vs logout — the money/booking-state race ──────────────
//
// cancelBooking() captures mySeq = reqSeq (capture only — `cancelling` already
// blocks self-reentry; this catches a CONCURRENT logout/new-plan instead), awaits
// POST /cancel, then only applies the refund + pushes the "Booking cancelled"
// message if mySeq === reqSeq. switchUser() (logout) is the only reqSeq-bumping
// action reachable from the booked view (no "New trip" button on a booked trip).
// A slow /cancel response landing after logout → continue as guest must NOT
// silently credit a stale refund into the NEW guest session's wallet/chat.

const BOOKED_CANCEL = {
  outcome: 'success',
  payment_status: 'charged',
  booking_ref: 'BK-CANCEL-RACE-1',
  idempotency_key: 'trip-cancel-race-1',
  package_total_with_fees_cents: 200000,
  total_budget_cents: 400000,
  wallet: { balance_cents: 300000, held_cents: 0, debited: true, debit_cents: 200000 },
  legs: [{ leg_id: 'leg-0', city: 'Hanoi', checkin: '2026-10-01', checkout: '2026-10-04' }],
  day_plans: [{
    leg_id: 'leg-0', city: 'Hanoi', num_days: 3,
    days: [{ day_index: 0, bad_weather: false, attractions: [], meals: {} }],
  }],
  risk_signals: { per_leg: [] },
};

test('cancel booking (slow response) then immediately log out → continue as guest → stale refund never lands on the new session', async ({ page }) => {
  await page.addInitScript((args) => {
    localStorage.setItem('tg_session', JSON.stringify({
      user: args.a, guest: true,
      activePlan: { session_id: 'sess-cancel-race-1', idempotency_key: 'trip-cancel-race-1' },
      savedAt: Date.now(),
    }));
  }, { a: DEMO_A });
  await mockCommon(page);

  // onMount's restore fetch — fast, not part of the race under test.
  await page.route('**/trips/**', (r) =>
    r.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(BOOKED_CANCEL) }));
  // switchUser() re-fetches the demo user list on the way back to the picker.
  await page.route('**/session', (r) =>
    r.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ demo_users: [DEMO_A, DEMO_B] }) }));
  // POST /cancel — artificially slow to open the race window. Refund balance is
  // deliberately DIFFERENT from the guest-default $5000 wallet so a leak is visible.
  await page.route('**/cancel', async (route) => {
    await new Promise((r) => setTimeout(r, 500));
    await route.fulfill({
      status: 200, contentType: 'application/json',
      body: JSON.stringify({ outcome: 'cancelled', refunded_cents: 200000, wallet_credit_cents: 200000, wallet_balance_cents: 470000 }),
    });
  });

  await page.goto('/');
  await expect(page.getByTestId('cancel-booking')).toBeVisible({ timeout: 3000 });
  await expect(page.getByTestId('status-banner')).toContainText('Booked', { timeout: 3000 });

  // Fire the slow cancel (in-flight POST /cancel)...
  await page.getByTestId('cancel-booking').click();
  // ...then immediately race it with logout, WITHOUT waiting for /cancel to settle.
  await page.getByTestId('switch-user').click();
  await expect(page.getByTestId('session-picker')).toBeVisible({ timeout: 2000 });

  // Move on to a brand-new guest session before the stale cancel lands.
  await page.getByTestId('continue-guest').click();
  await expect(page.getByTestId('guide-panel')).toBeVisible();

  // Let the delayed /cancel response resolve in the background — it must be
  // dropped (stale reqSeq), not silently credit A's refund into the guest's
  // fresh wallet or leak a "Booking cancelled" note into the guest's chat.
  await page.waitForTimeout(700);
  await expect(page.locator('.wallet input')).toHaveValue('5000');
  await expect(page.getByTestId('chat-msgs')).not.toContainText('Booking cancelled');
  await expect(page.getByTestId('status-banner')).toHaveCount(0);
});
