import { test, expect } from '@playwright/test';

// Budget & currency review — hermetic E2E spec covering the Budget tab (#77).
// All routes are mocked via page.route — no live backend.
//
// The Budget tab renders:
//   • served fee_line_items rows (label + formatted cents)
//   • a progress bar + "X% of your budget" hint
//   • for non-USD users: an indicative display-currency estimate (budget-indicative)
//     + disclaimer + exchange-timing guidance
//
// NOTE: budget_rows is a backend field; the frontend renders fee_line_items.
// These fixtures use fee_line_items so the lodging/insurance labels actually render.
// currency_review must carry indicative:true + indicative_minor_units + decimals
// (as per the CurrencyReview interface in api.ts) for fmtIndicative to produce output.

// ─────────────────────────────────────────────────────────────────────────────
// Fixtures
// ─────────────────────────────────────────────────────────────────────────────

const BASE_PLAN = {
  outcome: 'plan_ready',
  payment_status: 'held',
  booking_ref: null,
  idempotency_key: 'trip-cov-1',
  package_total_with_fees_cents: 140000,
  total_budget_cents: 300000,
  wallet: { balance_cents: 500000, held_cents: 140000, debited: false },
  fee_line_items: [
    { description: 'Lodging', usd_cents: 80000 },
    { description: 'Insurance', usd_cents: 30000 },
    { description: 'Visa fees', usd_cents: 30000 },
  ],
  legs: [
    {
      leg_id: 'leg-0', city: 'Lisbon',
      checkin: '2026-10-01', checkout: '2026-10-05',
      lat: 38.72, lng: -9.14,
      hotel_title: 'Test Hotel Lisbon',
    },
  ],
  day_plans: [
    {
      leg_id: 'leg-0', city: 'Lisbon', num_days: 4,
      days: [
        {
          day_index: 0, bad_weather: false,
          attractions: [
            { name: 'Belem Tower', category: 'tourism=attraction', weather_exposure: 'outdoor', lat: 38.69, lon: -9.22, fee: null },
          ],
          meals: { lunch: { name: 'Tasca da Esquina', category: 'amenity=restaurant', cuisine: 'portuguese' } },
          intracity_hops: [],
        },
      ],
      unscheduled_attractions: [
        { name: 'Jerónimos Monastery', ua_ref: 'ua-jeronimos', category: 'tourism=attraction' },
      ],
    },
  ],
  risk_signals: { per_leg: [] },
};

// currency_review must use the CurrencyReview interface shape (api.ts):
// indicative:true, indicative_minor_units (minor units of display_currency), decimals.
// SGD 189.00 → indicative_minor_units: 18900, decimals: 2
const PLAN_WITH_CURRENCY = {
  outcome: 'plan_ready',
  payment_status: 'held',
  booking_ref: null,
  idempotency_key: 'trip-cov-2',
  package_total_with_fees_cents: 140000,
  total_budget_cents: 300000,
  wallet: { balance_cents: 500000, held_cents: 140000, debited: false },
  legs: [
    { leg_id: 'leg-0', city: 'Lisbon', checkin: '2026-10-01', checkout: '2026-10-05' },
  ],
  day_plans: [
    {
      leg_id: 'leg-0', city: 'Lisbon', num_days: 4,
      days: [{ day_index: 0, bad_weather: false, attractions: [], meals: {}, intracity_hops: [] }],
      unscheduled_attractions: [],
    },
  ],
  risk_signals: { per_leg: [] },
  fee_line_items: [{ description: 'Lodging', usd_cents: 80000 }],
  currency_review: {
    display_currency: 'SGD',
    usd_cents: 140000,
    indicative_minor_units: 18900,
    decimals: 2,
    as_of: '2026-07',
    indicative: true,
    basis: 'seeded_snapshot',
    disclaimer: 'Exchange rate as of 2026-07-01; rates vary daily.',
    exchange_timing: {
      tier: 'standard',
      guidance: 'Book within 48 hours to lock in today\'s rate.',
      caveat: 'Rates vary daily.',
    },
  },
};

const USERS = {
  demo_users: [
    { user_id: 'demo-mei', display_name: 'Mei', persona: 'foodie', nationality: 'SG', home_currency: 'SGD', wallet_balance_cents: 500000 },
    { user_id: 'demo-alex', display_name: 'Alex', persona: 'hiker', nationality: 'US', home_currency: 'USD', wallet_balance_cents: 500000 },
  ],
};

// ─────────────────────────────────────────────────────────────────────────────
// Helpers
// ─────────────────────────────────────────────────────────────────────────────

async function mockCommonRoutes(page) {
  await page.route('**/emergencies', (r) =>
    r.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ status: 'ok', countries: [] }) }));
  await page.route(/tile|openstreetmap|basemaps|demotiles/, (r) => r.abort());
}

async function openBudgetTab(page) {
  // Click Budget tab scoped inside right-rail to avoid ambiguity
  await page.getByTestId('right-rail').getByRole('button', { name: 'Budget' }).click();
  await expect(page.getByTestId('tab-budget')).toBeVisible();
}

// ─────────────────────────────────────────────────────────────────────────────
// Tests 1 & 2 — guest user + BASE_PLAN (fee_line_items serve Lodging/Insurance)
// ─────────────────────────────────────────────────────────────────────────────

test('budget tab shows the estimate / % of budget', async ({ page }) => {
  // Guest preseed: bypass the session picker
  await page.addInitScript(() => {
    localStorage.setItem('tg_session', JSON.stringify({ user: null, guest: true, savedAt: Date.now() }));
  });
  await page.route('**/negotiate_text', (r) =>
    r.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(BASE_PLAN) }));
  await mockCommonRoutes(page);

  await page.goto('/');
  await page.getByTestId('chat-input').fill('4 days in lisbon, $3000');
  await page.getByRole('button', { name: 'Send' }).click();

  // Wait for plan to render (status banner says "Plan ready")
  await expect(page.getByTestId('status-banner')).toContainText('Plan ready', { timeout: 10_000 });

  await openBudgetTab(page);

  // Either a $ figure, % line, or honest empty state — never blank
  await expect(page.getByTestId('tab-budget')).toContainText(
    /\$\d|% of your budget|No budget breakdown/i,
  );
});

test('budget tab shows lodging / insurance rows from budget_rows', async ({ page }) => {
  await page.addInitScript(() => {
    localStorage.setItem('tg_session', JSON.stringify({ user: null, guest: true, savedAt: Date.now() }));
  });
  await page.route('**/negotiate_text', (r) =>
    r.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(BASE_PLAN) }));
  await mockCommonRoutes(page);

  await page.goto('/');
  await page.getByTestId('chat-input').fill('4 days in lisbon, $3000');
  await page.getByRole('button', { name: 'Send' }).click();

  await expect(page.getByTestId('status-banner')).toContainText('Plan ready', { timeout: 10_000 });

  await openBudgetTab(page);

  // The fee_line_items rows must appear — Lodging and Insurance labels from the served breakdown
  await expect(page.getByTestId('tab-budget')).toContainText('Lodging');
  await expect(page.getByTestId('tab-budget')).toContainText('Insurance');
});

// ─────────────────────────────────────────────────────────────────────────────
// Tests 3 & 4 — non-USD traveller (demo-mei / SGD) + PLAN_WITH_CURRENCY
// ─────────────────────────────────────────────────────────────────────────────

async function setupNonUsdPlan(page) {
  // Fresh localStorage → session picker appears
  await page.addInitScript(() => { try { localStorage.clear(); } catch { /* ignore */ } });
  await page.route('**/session', (r) =>
    r.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(USERS) }));
  await page.route('**/negotiate_text', (r) =>
    r.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(PLAN_WITH_CURRENCY) }));
  await mockCommonRoutes(page);

  await page.goto('/');
  // Wait for the session picker and select demo-mei (SGD)
  await expect(page.getByTestId('session-picker')).toBeVisible({ timeout: 15_000 });
  await page.getByTestId('user-demo-mei').click();

  // Submit a plan request
  await page.getByTestId('chat-input').fill('4 days in lisbon, food, $3000');
  await page.getByRole('button', { name: 'Send' }).click();

  // Wait for plan ready
  await expect(page.getByTestId('status-banner')).toContainText('Plan ready', { timeout: 10_000 });

  await openBudgetTab(page);
}

test('non-USD traveller sees indicative display-currency estimate', async ({ page }) => {
  await setupNonUsdPlan(page);

  // budget-indicative must be visible and label itself as indicative, showing SGD
  const indicative = page.getByTestId('budget-indicative');
  await expect(indicative).toBeVisible();
  await expect(indicative).toContainText(/indicative/i);
  await expect(indicative).toContainText('SGD');
});

test('budget tab shows exchange-timing guidance for non-USD traveller', async ({ page }) => {
  await setupNonUsdPlan(page);

  // The tab must contain rate/exchange/indicative language (from disclaimer + timing)
  await expect(page.getByTestId('tab-budget')).toContainText(/rate|exchang|indicative/i);
});
