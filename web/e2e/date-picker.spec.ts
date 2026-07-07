import { test, expect } from '@playwright/test';

// date-picker.spec.ts — Conditional date-range picker (UI increment #1).
//
// The DateRangePicker is shown inside <main class="center"> when the plan envelope
// has NO checkin on any leg or day_plan.  It is hidden when at least one leg or
// day_plan carries a checkin date.
//
// Both fixtures are hermetic: /negotiate_text and /refine are mocked via page.route.
// OSM tiles are aborted (offline-safe).  A guest session is pre-seeded so the
// SessionPicker view is bypassed.
//
// Tests:
//   Fixture A — undated plan → picker visible → fill dates → /refine mock returns
//               a dated plan → picker disappears, reply rendered.
//   Fixture B — dated plan   → picker never shown (count === 0).

// ── Undated plan_ready (NO checkin anywhere) ─────────────────────────────────
const UNDATED_PLAN: Record<string, unknown> = {
  outcome: 'plan_ready',
  payment_status: 'held',
  booking_ref: null,
  idempotency_key: 'trip-dp-1',
  package_total_with_fees_cents: 180000,
  total_budget_cents: 400000,
  wallet: { balance_cents: 500000, held_cents: 180000, debited: false },
  // legs WITHOUT checkin/checkout
  legs: [{ leg_id: 'leg-0', city: 'Kyoto' }],
  day_plans: [
    {
      leg_id: 'leg-0',
      city: 'Kyoto',
      num_days: 3,
      days: [
        {
          day_index: 0,
          bad_weather: false,
          attractions: [{ name: 'Fushimi Inari Shrine', category: 'tourism=attraction', lat: 34.967, lon: 135.772 }],
          meals: {},
        },
      ],
    },
  ],
  risk_signals: { per_leg: [] },
};

// ── Dated plan_ready (checkin present on legs) ────────────────────────────────
const DATED_PLAN: Record<string, unknown> = {
  ...UNDATED_PLAN,
  idempotency_key: 'trip-dp-dated',
  legs: [{ leg_id: 'leg-0', city: 'Kyoto', checkin: '2026-11-01', checkout: '2026-11-04' }],
};

// ── /refine response: returns a dated plan so picker should disappear ─────────
const REFINE_RESPONSE: Record<string, unknown> = {
  outcome: 'plan_ready',
  assistant_reply: 'Dates set to 2026-11-10 through 2026-11-17.',
  idempotency_key: 'trip-dp-1',
  session_id: 'sess-dp-1',
  plan: DATED_PLAN,
};

/** Pre-seed guest session and abort OSM tiles. */
async function seedGuestSession(page: import('@playwright/test').Page): Promise<void> {
  await page.addInitScript(() => {
    localStorage.setItem('tg_session', JSON.stringify({ user: null, guest: true, savedAt: Date.now() }));
  });
  await page.route('**/tile.openstreetmap.org/**', (route) => route.abort());
}

// ── Fixture A ────────────────────────────────────────────────────────────────
test('Fixture A: undated plan → picker visible → fill → /refine → picker gone + reply shown', async ({ page }) => {
  await seedGuestSession(page);

  await page.route('**/negotiate_text', (route) =>
    route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify(UNDATED_PLAN),
    })
  );
  await page.route('**/refine', (route) =>
    route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify(REFINE_RESPONSE),
    })
  );

  await page.goto('/');
  await page.getByTestId('chat-input').fill('3 days in kyoto, temples');
  await page.getByRole('button', { name: 'Send' }).click();

  // Itinerary rendered, picker should be visible (no dates in the plan)
  await expect(page.getByTestId('itinerary')).toBeVisible({ timeout: 8000 });
  const picker = page.getByTestId('date-picker-section');
  await expect(picker).toBeVisible();

  // Confirm button should be disabled until both fields are filled
  const confirmBtn = page.getByTestId('date-confirm');
  await expect(confirmBtn).toBeDisabled();

  // Fill start date
  await page.getByTestId('start-date').fill('2026-11-10');
  // Still disabled (end not filled)
  await expect(confirmBtn).toBeDisabled();

  // Fill end date
  await page.getByTestId('end-date').fill('2026-11-17');
  // Now enabled (start <= end)
  await expect(confirmBtn).toBeEnabled();

  // Click Confirm — triggers /refine
  await confirmBtn.click();

  // Picker should disappear (dated plan returned by /refine)
  await expect(page.getByTestId('date-picker-section')).toHaveCount(0, { timeout: 8000 });

  // Bot reply rendered in chat (test-id is 'chat-msgs' per ChatPane.svelte)
  await expect(page.getByTestId('chat-msgs')).toContainText('Dates set to 2026-11-10', { timeout: 8000 });
});

// ── Fixture B ────────────────────────────────────────────────────────────────
test('Fixture B: dated plan → date picker not rendered (count 0)', async ({ page }) => {
  await seedGuestSession(page);

  await page.route('**/negotiate_text', (route) =>
    route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify(DATED_PLAN),
    })
  );

  await page.goto('/');
  await page.getByTestId('chat-input').fill('3 days in kyoto nov 1-4');
  await page.getByRole('button', { name: 'Send' }).click();

  // Itinerary must be visible first (plan rendered)
  await expect(page.getByTestId('itinerary')).toBeVisible({ timeout: 8000 });

  // Picker must NOT be in the DOM at all
  await expect(page.getByTestId('date-picker-section')).toHaveCount(0);
});
