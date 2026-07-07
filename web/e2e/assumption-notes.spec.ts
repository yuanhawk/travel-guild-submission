import { test, expect } from '@playwright/test';

// assumption-notes.spec.ts — Honest-assumption disclosure banner (#188).
//
// The backend's parser silently guesses at missing date/year/party-size/currency/
// children-pricing and attaches server-authored notes to the negotiate/plan envelope
// (date_assumption_note, date_year_assumption_note, adults_assumption_note,
// currency_assumption_note, children_note). Live-confirmed the frontend rendered
// NONE of them. This spec locks in the fix: the `.assumption-notes` banner (directly
// above Confirm & Book) surfaces every note the server attached, and disappears once
// /refine returns a plan without them (i.e. the user supplied the missing info).
//
// Both fixtures are hermetic: /negotiate_text and /refine are mocked via page.route.
// OSM tiles are aborted (offline-safe). A guest session is pre-seeded so the
// SessionPicker view is bypassed.
//
// Tests:
//   Fixture A — plan_ready carrying 3 of the 5 notes → banner visible, contains the
//               3 note texts, li count === 3. Then /refine returns a plan WITHOUT
//               notes → banner disappears (self-correction).
//   Fixture B — clean plan_ready with no notes → banner has count 0 (no false positive).

const DATE_NOTE = 'I assumed you meant starting tomorrow since no date was given.';
const YEAR_NOTE = 'I assumed 2027 since April 10 has already passed this year.';
const CURRENCY_NOTE = 'I assumed USD for the 1500 budget since no currency was specified.';

// ── plan_ready carrying 3 of the 5 honest-assumption notes ──────────────────
const PLAN_WITH_ASSUMPTIONS: Record<string, unknown> = {
  outcome: 'plan_ready',
  payment_status: 'held',
  booking_ref: null,
  idempotency_key: 'trip-an-1',
  package_total_with_fees_cents: 180000,
  total_budget_cents: 400000,
  wallet: { balance_cents: 500000, held_cents: 180000, debited: false },
  // legs carry a checkin (the parser's INVENTED date) so the date-picker doesn't
  // also render — this spec is about the assumption banner specifically.
  legs: [{ leg_id: 'leg-0', city: 'Kyoto', checkin: '2027-04-10', checkout: '2027-04-14' }],
  day_plans: [
    {
      leg_id: 'leg-0',
      city: 'Kyoto',
      checkin: '2027-04-10',
      checkout: '2027-04-14',
      num_days: 4,
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
  assumed_start_date: '2027-04-10',
  date_assumption_note: DATE_NOTE,
  assumed_date_year: true,
  date_year_assumption_note: YEAR_NOTE,
  adults_assumption_note: null,
  assumed_currency: 'USD',
  currency_assumption_note: CURRENCY_NOTE,
  ignored_children: null,
  children_note: null,
};

// ── clean plan_ready with NO assumption notes ────────────────────────────────
const CLEAN_PLAN: Record<string, unknown> = {
  ...PLAN_WITH_ASSUMPTIONS,
  idempotency_key: 'trip-an-clean',
  date_assumption_note: null,
  date_year_assumption_note: null,
  adults_assumption_note: null,
  currency_assumption_note: null,
  children_note: null,
};

// ── /refine response: real date/party/currency supplied → notes gone ────────
const REFINE_RESPONSE_NO_NOTES: Record<string, unknown> = {
  outcome: 'plan_ready',
  assistant_reply: 'Dates set to 2026-11-10 through 2026-11-14, budget in USD confirmed.',
  idempotency_key: 'trip-an-1',
  session_id: 'sess-an-1',
  plan: CLEAN_PLAN,
};

/** Pre-seed guest session and abort OSM tiles. */
async function seedGuestSession(page: import('@playwright/test').Page): Promise<void> {
  await page.addInitScript(() => {
    localStorage.setItem('tg_session', JSON.stringify({ user: null, guest: true, savedAt: Date.now() }));
  });
  await page.route('**/tile.openstreetmap.org/**', (route) => route.abort());
}

// ── Fixture A ────────────────────────────────────────────────────────────────
test('Fixture A: plan with 3 notes → banner visible with 3 items → /refine clears it', async ({ page }) => {
  await seedGuestSession(page);

  await page.route('**/negotiate_text', (route) =>
    route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify(PLAN_WITH_ASSUMPTIONS),
    })
  );
  await page.route('**/refine', (route) =>
    route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify(REFINE_RESPONSE_NO_NOTES),
    })
  );

  await page.goto('/');
  await page.getByTestId('chat-input').fill('4 days in Kyoto, budget 1500');
  await page.getByRole('button', { name: 'Send' }).click();

  await expect(page.getByTestId('itinerary')).toBeVisible({ timeout: 8000 });

  const banner = page.getByTestId('assumption-notes');
  await expect(banner).toBeVisible();
  await expect(banner).toContainText(DATE_NOTE);
  await expect(banner).toContainText(YEAR_NOTE);
  await expect(banner).toContainText(CURRENCY_NOTE);
  await expect(banner.locator('li')).toHaveCount(3);

  // The banner narrates WHY a date was assumed; it must also show WHAT that assumption
  // concretely produced — the fixture's leg carries checkin 2027-04-10 / checkout 2027-04-14.
  await expect(banner.getByTestId('assumption-date-range')).toContainText('Apr 10 – Apr 14, 2027 (4 nights)');

  // Also mirrored into the chat thread as a 'note' bubble.
  await expect(page.getByTestId('chat-msgs')).toContainText(DATE_NOTE, { timeout: 8000 });

  // Supply the real date/party/currency via /refine → the returned plan carries
  // no notes → the banner must disappear (self-correction, no stale disclosure).
  // Once a plan is active, chat collapses to the floating 🤖 assistant — open the
  // flyout before the follow-up chat-input is actionable (its Send affordance is
  // an icon button "↑", not a "Send"-labelled one, so submit via Enter).
  await page.getByTestId('assistant-trigger').click();
  await expect(page.getByTestId('assistant-flyout')).toBeVisible({ timeout: 3000 });
  await page.getByTestId('chat-input').fill('Set the dates to Nov 10-14 2026, budget is in USD.');
  await page.getByTestId('chat-input').press('Enter');

  await expect(page.getByTestId('assumption-notes')).toHaveCount(0, { timeout: 8000 });
});

// ── Fixture B ────────────────────────────────────────────────────────────────
test('Fixture B: clean plan with no notes → banner not rendered (count 0, no false positive)', async ({ page }) => {
  await seedGuestSession(page);

  await page.route('**/negotiate_text', (route) =>
    route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify(CLEAN_PLAN),
    })
  );

  await page.goto('/');
  await page.getByTestId('chat-input').fill('4 days in Kyoto, Nov 10-14 2026, budget $1500 USD for 2 adults');
  await page.getByRole('button', { name: 'Send' }).click();

  await expect(page.getByTestId('itinerary')).toBeVisible({ timeout: 8000 });
  await expect(page.getByTestId('assumption-notes')).toHaveCount(0);
});
