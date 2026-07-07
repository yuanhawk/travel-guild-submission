import { test, expect, type Page } from '@playwright/test';

// guide-cards.spec.ts — hermetic E2E coverage for guide card flows.
//
// Test surface (all missed by prior manual-only testing):
//   A. Card inventory  — every region tab has the right count + correct prompts
//   B. Plan success    — guide card → plan_ready → itinerary/budget/safety render
//   C. Budget shortfall — cannot_satisfy + shortfall hint → correct stateNote text
//   D. Context-loss fix — follow-up after failure prepends original prompt
//   E. Declined / advisory — cannot_satisfy without shortfall → "I won't book" note
//   F. Insufficient funds — wallet too low → dollar-amount comparison in note
//   G. Persona recs    — all 6 persona types render their section + correct cards
//   H. Nationality defaults — US → americas, EG → africa-me pre-selected
//   I. Wallet passthrough — wallet_balance_cents sent correctly in every request
//   J. Sequence replan  — second guide card after existing plan starts fresh
//   K. Advisory safety  — plan_ready + risk_signals surfaced in Safety tab
//   P. Destination photos — each region tab shows the right .guide-photo vs
//      emoji-fallback (.guide-icon) count for what actually shipped
//
// All routes are mocked — no live backend.

// ─────────────────────────────────────────────────────────────────────────────
// Fixtures
// ─────────────────────────────────────────────────────────────────────────────

const PLAN_READY_JAPAN = {
  outcome: 'plan_ready',
  payment_status: 'held',
  booking_ref: null,
  idempotency_key: 'trip-jp-1',
  package_total_with_fees_cents: 220000,
  total_budget_cents: 250000,
  wallet: { balance_cents: 500000, held_cents: 220000, debited: false },
  fee_line_items: [
    { description: 'Lodging', usd_cents: 180000 },
    { description: 'Insurance', usd_cents: 40000 },
  ],
  legs: [
    { leg_id: 'leg-0', city: 'Tokyo',  checkin: '2026-09-01', checkout: '2026-09-05', lat: 35.68, lng: 139.69 },
    { leg_id: 'leg-1', city: 'Kyoto',  checkin: '2026-09-05', checkout: '2026-09-08', lat: 35.01, lng: 135.77 },
    { leg_id: 'leg-2', city: 'Osaka',  checkin: '2026-09-08', checkout: '2026-09-11', lat: 34.69, lng: 135.50 },
  ],
  day_plans: [
    { leg_id: 'leg-0', city: 'Tokyo', num_days: 4, days: [{ day_index: 0, bad_weather: false, attractions: [{ name: 'Senso-ji Temple', category: 'tourism=attraction', weather_exposure: 'outdoor', lat: 35.71, lon: 139.79, fee: null }], meals: {}, intracity_hops: [] }], unscheduled_attractions: [] },
    { leg_id: 'leg-1', city: 'Kyoto', num_days: 3, days: [{ day_index: 0, bad_weather: false, attractions: [], meals: {}, intracity_hops: [] }], unscheduled_attractions: [] },
    { leg_id: 'leg-2', city: 'Osaka', num_days: 3, days: [{ day_index: 0, bad_weather: false, attractions: [], meals: {}, intracity_hops: [] }], unscheduled_attractions: [] },
  ],
  risk_signals: { per_leg: [] },
};

const PLAN_READY_VIETNAM = {
  outcome: 'plan_ready',
  payment_status: 'held',
  booking_ref: null,
  idempotency_key: 'trip-vn-1',
  package_total_with_fees_cents: 130000,
  total_budget_cents: 150000,
  wallet: { balance_cents: 500000, held_cents: 130000, debited: false },
  fee_line_items: [{ description: 'Lodging', usd_cents: 130000 }],
  legs: [
    { leg_id: 'leg-0', city: 'Hanoi',    checkin: '2026-10-01', checkout: '2026-10-05', lat: 21.03, lng: 105.85 },
    { leg_id: 'leg-1', city: 'Hoi An',   checkin: '2026-10-05', checkout: '2026-10-09', lat: 15.88, lng: 108.34 },
    { leg_id: 'leg-2', city: 'Ho Chi Minh City', checkin: '2026-10-09', checkout: '2026-10-13', lat: 10.82, lng: 106.63 },
  ],
  day_plans: [
    { leg_id: 'leg-0', city: 'Hanoi', num_days: 4, days: [{ day_index: 0, bad_weather: false, attractions: [], meals: {}, intracity_hops: [] }], unscheduled_attractions: [] },
    { leg_id: 'leg-1', city: 'Hoi An', num_days: 4, days: [{ day_index: 0, bad_weather: false, attractions: [], meals: {}, intracity_hops: [] }], unscheduled_attractions: [] },
    { leg_id: 'leg-2', city: 'Ho Chi Minh City', num_days: 4, days: [{ day_index: 0, bad_weather: false, attractions: [], meals: {}, intracity_hops: [] }], unscheduled_attractions: [] },
  ],
  risk_signals: { per_leg: [] },
};

const PLAN_READY_FRANCE = {
  outcome: 'plan_ready',
  payment_status: 'held',
  booking_ref: null,
  idempotency_key: 'trip-fr-1',
  package_total_with_fees_cents: 200000,
  total_budget_cents: 250000,
  wallet: { balance_cents: 500000, held_cents: 200000, debited: false },
  fee_line_items: [{ description: 'Lodging', usd_cents: 200000 }],
  legs: [
    { leg_id: 'leg-0', city: 'Paris', checkin: '2026-09-01', checkout: '2026-09-08', lat: 48.85, lng: 2.35 },
  ],
  day_plans: [
    { leg_id: 'leg-0', city: 'Paris', num_days: 7, days: [{ day_index: 0, bad_weather: false, attractions: [{ name: 'Eiffel Tower', category: 'tourism=attraction', weather_exposure: 'outdoor', lat: 48.86, lon: 2.29, fee: null }], meals: {}, intracity_hops: [] }], unscheduled_attractions: [] },
  ],
  risk_signals: { per_leg: [] },
};

// Plan with safety flags — used to verify advisory surfacing
const PLAN_READY_WITH_ADVISORY = {
  ...PLAN_READY_FRANCE,
  legs: [{ ...PLAN_READY_FRANCE.legs[0], city: 'Amman' }],
  day_plans: [{ ...PLAN_READY_FRANCE.day_plans[0], city: 'Amman' }],
  risk_signals: {
    per_leg: [{
      leg_id: 'leg-0',
      alert_tier: 'HIGH',
      decisions: { flag: true },
      advisory: [{ type: 'civil_unrest', severity: 'high', detail: 'Monitor local advisories closely.' }],
    }],
  },
};

// Budget shortfall — outcome:cannot_satisfy + budget_shortfall_cents present
// → uiState = 'budget_shortfall'
const BUDGET_SHORTFALL_VIETNAM = {
  outcome: 'cannot_satisfy',
  reason: 'Cannot re-plan any leg to fit within budget 150000¢. All options exhausted.',
  budget_shortfall_cents: 12000,      // $120 short
  min_feasible_total_cents: 162000,   // min feasible = $1,620
  closest_package_total_cents: 162000,
};

// Declined — outcome:cannot_satisfy with NO budget_shortfall_cents
// → uiState = 'declined'
const DECLINED_NO_BOOK = {
  outcome: 'cannot_satisfy',
  reason: 'Destination flagged as do-not-recommend: EXC-ADVISORY region.',
};

// Insufficient funds — outcome:cannot_satisfy, reason contains 'insufficient_funds'
// → uiState = 'insufficient_funds'
const INSUFFICIENT_FUNDS = {
  outcome: 'cannot_satisfy',
  reason: 'insufficient_funds',
  total_cents: 250000,        // trip cost $2,500
  wallet_balance_cents: 80000, // wallet only $800
};

// After budget shortfall follow-up — a successful plan
const PLAN_AFTER_RAISE = {
  outcome: 'plan_ready',
  payment_status: 'held',
  booking_ref: null,
  idempotency_key: 'trip-vn-2',
  package_total_with_fees_cents: 160000,
  total_budget_cents: 200000,
  wallet: { balance_cents: 500000, held_cents: 160000, debited: false },
  fee_line_items: [{ description: 'Lodging', usd_cents: 160000 }],
  legs: [{ leg_id: 'leg-0', city: 'Hanoi', checkin: '2026-10-01', checkout: '2026-10-13', lat: 21.03, lng: 105.85 }],
  day_plans: [{ leg_id: 'leg-0', city: 'Hanoi', num_days: 12, days: [{ day_index: 0, bad_weather: false, attractions: [], meals: {}, intracity_hops: [] }], unscheduled_attractions: [] }],
  risk_signals: { per_leg: [] },
};

// ─────────────────────────────────────────────────────────────────────────────
// Helpers
// ─────────────────────────────────────────────────────────────────────────────

async function mockCommon(page: Page): Promise<void> {
  await page.route('**/emergencies', (r) =>
    r.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ status: 'ok', countries: [] }) }));
  await page.route(/tile|openstreetmap|basemaps|demotiles/, (r) => r.abort());
}

async function preseedGuest(page: Page): Promise<void> {
  await page.addInitScript(() => {
    localStorage.setItem('tg_session', JSON.stringify({ user: null, guest: true, savedAt: Date.now() }));
  });
}

function preseedUser(page: Page, user: {
  user_id: string; display_name: string; persona: string;
  nationality: string; home_currency: string; wallet_balance_cents: number;
}): Promise<void> {
  return page.addInitScript((u) => {
    localStorage.setItem('tg_session', JSON.stringify({ user: u, guest: false, savedAt: Date.now() }));
  }, user);
}

/**
 * Mock /negotiate_text for a SINGLE user submission (2 calls: stream:true → {}; stream:false → plan).
 * Returns a getter for the body of each call so assertions can inspect what was sent.
 */
function mockNegotiate(page: Page, plan: unknown): {
  getStreamBody: () => Record<string, unknown> | null;
  getBlockingBody: () => Record<string, unknown> | null;
} {
  let streamBody: Record<string, unknown> | null = null;
  let blockingBody: Record<string, unknown> | null = null;
  let call = 0;

  page.route('**/negotiate_text', async (route) => {
    call++;
    const body = JSON.parse(route.request().postData() ?? '{}') as Record<string, unknown>;
    if (call % 2 === 1) {
      // Odd = stream:true attempt → degrade (no stream_id)
      streamBody = body;
      await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({}) });
    } else {
      // Even = blocking fallback → deliver plan
      blockingBody = body;
      await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(plan) });
    }
  });

  return {
    getStreamBody:   () => streamBody,
    getBlockingBody: () => blockingBody,
  };
}

/**
 * Mock /negotiate_text for TWO sequential user submissions.
 *   - Submission 1 (guide card click): stream:true → {}, stream:false → plan1
 *   - Submission 2 (follow-up text):   stream:true → {}, stream:false → plan2
 * Returns getters for each blocking body (the one with real content, not the stream degrade).
 */
function mockNegotiateTwo(page: Page, plan1: unknown, plan2: unknown): {
  getFirstBlockingBody:  () => Record<string, unknown> | null;
  getSecondBlockingBody: () => Record<string, unknown> | null;
} {
  let call = 0;
  let blockingCallCount = 0;
  const blockingBodies: Array<Record<string, unknown>> = [];

  page.route('**/negotiate_text', async (route) => {
    call++;
    const body = JSON.parse(route.request().postData() ?? '{}') as Record<string, unknown>;
    if (call % 2 === 1) {
      // stream attempt → degrade
      await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({}) });
    } else {
      blockingCallCount++;
      blockingBodies.push(body);
      const plan = blockingCallCount === 1 ? plan1 : plan2;
      await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(plan) });
    }
  });

  return {
    getFirstBlockingBody:  () => blockingBodies[0] ?? null,
    getSecondBlockingBody: () => blockingBodies[1] ?? null,
  };
}

// ─────────────────────────────────────────────────────────────────────────────
// A. Card Inventory — every region tab has the right count and correct prompts
// ─────────────────────────────────────────────────────────────────────────────

test.describe('A. Card inventory per region tab', () => {

  test('A-01: Asia tab has 6 cards with correct first and last prompts', async ({ page }) => {
    await preseedGuest(page);
    await mockCommon(page);
    await page.goto('/');
    await expect(page.getByTestId('guide-panel')).toBeVisible({ timeout: 5000 });

    const cards = page.locator('.guide-card');
    await expect(cards).toHaveCount(6);

    // First card = Japan
    await expect(cards.first()).toContainText('Japan');
    // Last card = South Korea
    await expect(cards.nth(5)).toContainText('South Korea');
  });

  test('A-02: Asia tab cards send the correct prompts (Vietnam full route)', async ({ page }) => {
    await preseedGuest(page);
    await mockCommon(page);
    const { getBlockingBody } = mockNegotiate(page, PLAN_READY_VIETNAM);

    await page.goto('/');
    await expect(page.getByTestId('guide-panel')).toBeVisible({ timeout: 5000 });

    // Vietnam is the 4th card (index 3)
    await page.locator('.guide-card').nth(3).click();

    await expect.poll(() => getBlockingBody()?.text, { timeout: 6000 })
      .toBe('12 days Vietnam north to south, $1500');
  });

  test('A-03: Asia tab Singapore card sends correct prompt', async ({ page }) => {
    await preseedGuest(page);
    await mockCommon(page);
    const { getBlockingBody } = mockNegotiate(page, PLAN_READY_JAPAN);

    await page.goto('/');
    await expect(page.getByTestId('guide-panel')).toBeVisible({ timeout: 5000 });

    // Singapore is the 5th card (index 4)
    await page.locator('.guide-card').nth(4).click();
    await expect.poll(() => getBlockingBody()?.text, { timeout: 6000 })
      .toBe('5 days Singapore, $2000');
  });

  test('A-04: Europe tab has 6 cards, France is first', async ({ page }) => {
    await preseedGuest(page);
    await mockCommon(page);
    await page.goto('/');
    await expect(page.getByTestId('guide-panel')).toBeVisible({ timeout: 5000 });

    await page.getByTestId('guide-panel').locator('.rtab', { hasText: 'Europe' }).click();

    const cards = page.locator('.guide-card');
    await expect(cards).toHaveCount(6);
    await expect(cards.first()).toContainText('France');
  });

  test('A-05: Europe tab first card sends correct France prompt', async ({ page }) => {
    await preseedGuest(page);
    await mockCommon(page);
    const { getBlockingBody } = mockNegotiate(page, PLAN_READY_FRANCE);

    await page.goto('/');
    await expect(page.getByTestId('guide-panel')).toBeVisible({ timeout: 5000 });

    await page.getByTestId('guide-panel').locator('.rtab', { hasText: 'Europe' }).click();
    await page.locator('.guide-card').first().click();

    await expect.poll(() => getBlockingBody()?.text, { timeout: 6000 })
      .toBe('10 days France, $2500');
  });

  test('A-06: Americas tab has 6 cards, USA is first', async ({ page }) => {
    await preseedGuest(page);
    await mockCommon(page);
    await page.goto('/');
    await expect(page.getByTestId('guide-panel')).toBeVisible({ timeout: 5000 });

    await page.getByTestId('guide-panel').locator('.rtab', { hasText: 'Americas' }).click();

    const cards = page.locator('.guide-card');
    await expect(cards).toHaveCount(6);
    await expect(cards.first()).toContainText('USA');
  });

  test('A-07: Americas USA card sends 14-day prompt', async ({ page }) => {
    await preseedGuest(page);
    await mockCommon(page);
    const { getBlockingBody } = mockNegotiate(page, PLAN_READY_FRANCE);

    await page.goto('/');
    await expect(page.getByTestId('guide-panel')).toBeVisible({ timeout: 5000 });

    await page.getByTestId('guide-panel').locator('.rtab', { hasText: 'Americas' }).click();
    await page.locator('.guide-card').first().click();

    await expect.poll(() => getBlockingBody()?.text, { timeout: 6000 })
      .toBe('14 days USA, $3500');
  });

  test('A-08: Africa & Middle East tab has 6 cards, Dubai is first', async ({ page }) => {
    await preseedGuest(page);
    await mockCommon(page);
    await page.goto('/');
    await expect(page.getByTestId('guide-panel')).toBeVisible({ timeout: 5000 });

    await page.getByTestId('guide-panel').locator('.rtab', { hasText: 'Africa' }).click();

    const cards = page.locator('.guide-card');
    await expect(cards).toHaveCount(6);
    await expect(cards.first()).toContainText('Dubai');
  });

  test('A-09: Africa-ME Kenya card sends safari prompt', async ({ page }) => {
    await preseedGuest(page);
    await mockCommon(page);
    const { getBlockingBody } = mockNegotiate(page, PLAN_READY_FRANCE);

    await page.goto('/');
    await expect(page.getByTestId('guide-panel')).toBeVisible({ timeout: 5000 });

    await page.getByTestId('guide-panel').locator('.rtab', { hasText: 'Africa' }).click();
    // Kenya is 4th card (index 3)
    await page.locator('.guide-card').nth(3).click();

    await expect.poll(() => getBlockingBody()?.text, { timeout: 6000 })
      .toBe('10 days Kenya safari, $4000');
  });

  test('A-10: all 4 region tab buttons are visible', async ({ page }) => {
    await preseedGuest(page);
    await mockCommon(page);
    await page.goto('/');
    await expect(page.getByTestId('guide-panel')).toBeVisible({ timeout: 5000 });

    const guidePanel = page.getByTestId('guide-panel');
    await expect(guidePanel.locator('.rtab', { hasText: 'Asia' })).toBeVisible();
    await expect(guidePanel.locator('.rtab', { hasText: 'Europe' })).toBeVisible();
    await expect(guidePanel.locator('.rtab', { hasText: 'Americas' })).toBeVisible();
    await expect(guidePanel.locator('.rtab', { hasText: 'Africa' })).toBeVisible();
  });

  test('A-11: every guide card request has plan:true (no accidental commit)', async ({ page }) => {
    await preseedGuest(page);
    await mockCommon(page);
    const { getBlockingBody } = mockNegotiate(page, PLAN_READY_JAPAN);

    await page.goto('/');
    await expect(page.getByTestId('guide-panel')).toBeVisible({ timeout: 5000 });
    await page.locator('.guide-card').first().click();

    await expect.poll(() => getBlockingBody(), { timeout: 6000 }).not.toBeNull();
    expect(getBlockingBody()?.plan).toBe(true);
  });
});

// ─────────────────────────────────────────────────────────────────────────────
// B. Plan success path — guide card → plan_ready → full UI renders
// ─────────────────────────────────────────────────────────────────────────────

test.describe('B. Plan success path', () => {

  test('B-01: Japan guide card → multi-city plan shows Tokyo, Kyoto, Osaka in itinerary', async ({ page }) => {
    await preseedGuest(page);
    await mockCommon(page);
    mockNegotiate(page, PLAN_READY_JAPAN);

    await page.goto('/');
    await expect(page.getByTestId('guide-panel')).toBeVisible({ timeout: 5000 });
    await page.locator('.guide-card').first().click();   // Japan

    await expect(page.getByTestId('status-banner')).toContainText('Plan ready', { timeout: 12000 });
    const itinerary = page.getByTestId('itinerary');
    await expect(itinerary).toContainText('Tokyo');
    await expect(itinerary).toContainText('Kyoto');
    await expect(itinerary).toContainText('Osaka');
  });

  test('B-02: Japan guide card plan shows status banner with held amount', async ({ page }) => {
    await preseedGuest(page);
    await mockCommon(page);
    mockNegotiate(page, PLAN_READY_JAPAN);

    await page.goto('/');
    await page.locator('.guide-card').first().click();

    const banner = page.getByTestId('status-banner');
    await expect(banner).toContainText('Plan ready', { timeout: 12000 });
    await expect(banner).toContainText('held');
    await expect(banner).toContainText('$2,200.00');
    await expect(page.getByTestId('confirm-book')).toBeVisible();
  });

  test('B-03: Vietnam guide card → 3-city plan shows Hanoi, Hoi An, Ho Chi Minh City', async ({ page }) => {
    await preseedGuest(page);
    await mockCommon(page);
    mockNegotiate(page, PLAN_READY_VIETNAM);

    await page.goto('/');
    await page.locator('.guide-card').nth(3).click();   // Vietnam

    await expect(page.getByTestId('itinerary')).toBeVisible({ timeout: 12000 });
    const itinerary = page.getByTestId('itinerary');
    await expect(itinerary).toContainText('Hanoi');
    await expect(itinerary).toContainText('Hoi An');
    await expect(itinerary).toContainText('Ho Chi Minh');
  });

  test('B-04: Europe France card → plan shows Paris in itinerary', async ({ page }) => {
    await preseedGuest(page);
    await mockCommon(page);
    mockNegotiate(page, PLAN_READY_FRANCE);

    await page.goto('/');
    await page.getByTestId('guide-panel').locator('.rtab', { hasText: 'Europe' }).click();
    await page.locator('.guide-card').first().click();

    await expect(page.getByTestId('itinerary')).toContainText('Paris', { timeout: 12000 });
  });

  test('B-05: guide card plan shows Budget tab with lodging row', async ({ page }) => {
    await preseedGuest(page);
    await mockCommon(page);
    mockNegotiate(page, PLAN_READY_JAPAN);

    await page.goto('/');
    await page.locator('.guide-card').first().click();

    await expect(page.getByTestId('status-banner')).toContainText('Plan ready', { timeout: 12000 });
    await page.getByTestId('right-rail').getByRole('button', { name: 'Budget' }).click();
    await expect(page.getByTestId('tab-budget')).toContainText('Lodging');
  });
});

// ─────────────────────────────────────────────────────────────────────────────
// C. Budget shortfall — cannot_satisfy with shortfall hint
// ─────────────────────────────────────────────────────────────────────────────

test.describe('C. Budget shortfall from guide card', () => {

  test('C-01: budget shortfall stateNote shows amount short and min feasible', async ({ page }) => {
    await preseedGuest(page);
    await mockCommon(page);
    mockNegotiate(page, BUDGET_SHORTFALL_VIETNAM);

    await page.goto('/');
    await page.locator('.guide-card').nth(3).click();   // Vietnam

    const stateCard = page.getByTestId('state-card');
    await expect(stateCard).toBeVisible({ timeout: 10000 });
    // stateNote: "About $120 short — raise the budget or shorten the trip (min feasible $1,620)."
    await expect(stateCard).toContainText('$120.00');
    await expect(stateCard).toContainText('$1,620.00');
    await expect(stateCard).toContainText('raise');
  });

  test('C-02: budget shortfall state-card has budget_shortfall CSS class (red colour)', async ({ page }) => {
    await preseedGuest(page);
    await mockCommon(page);
    mockNegotiate(page, BUDGET_SHORTFALL_VIETNAM);

    await page.goto('/');
    await page.locator('.guide-card').nth(3).click();

    await expect(page.locator('.placeholder.state-budget_shortfall')).toBeVisible({ timeout: 10000 });
  });

  test('C-03: Singapore guide card budget shortfall shows correct amounts', async ({ page }) => {
    await preseedGuest(page);
    await mockCommon(page);
    mockNegotiate(page, {
      outcome: 'cannot_satisfy',
      budget_shortfall_cents: 5000,         // $50 short
      min_feasible_total_cents: 205000,     // min $2,050
    });

    await page.goto('/');
    await page.locator('.guide-card').nth(4).click();   // Singapore

    const stateCard = page.getByTestId('state-card');
    await expect(stateCard).toBeVisible({ timeout: 10000 });
    await expect(stateCard).toContainText('$50.00');
    await expect(stateCard).toContainText('$2,050.00');
  });
});

// ─────────────────────────────────────────────────────────────────────────────
// D. Context-loss fix — follow-up after failure prepends original prompt
//
// Root cause: plan() sent only the new follow-up text, stripping destination.
// Fix (App.svelte): when result !== null, prepend priorUserMsg + " — " + text.
// ─────────────────────────────────────────────────────────────────────────────

test.describe('D. Context-loss fix: follow-up after failure', () => {

  test('D-01: follow-up "raise to $2000" after Vietnam budget-shortfall prepends original prompt', async ({ page }) => {
    await preseedGuest(page);
    await mockCommon(page);
    // Submission 1: guide card → budget shortfall
    // Submission 2: follow-up raise → success
    const { getFirstBlockingBody, getSecondBlockingBody } =
      mockNegotiateTwo(page, BUDGET_SHORTFALL_VIETNAM, PLAN_AFTER_RAISE);

    await page.goto('/');
    await page.locator('.guide-card').nth(3).click();   // "12 days Vietnam north to south, $1500"

    // Wait for the budget shortfall note to appear
    await expect(page.getByTestId('state-card')).toBeVisible({ timeout: 10000 });

    // User types follow-up without repeating the destination
    await page.getByTestId('chat-input').fill('raise to $2000');
    await page.getByRole('button', { name: 'Send' }).click();

    await expect.poll(() => getSecondBlockingBody(), { timeout: 8000 }).not.toBeNull();

    // THE CRITICAL ASSERTION: backend must receive the full context, not just "raise to $2000"
    expect(getSecondBlockingBody()?.text).toBe(
      '12 days Vietnam north to south, $1500 — raise to $2000',
    );
  });

  test('D-02: follow-up after budget shortfall yields successful plan (end-to-end recovery)', async ({ page }) => {
    await preseedGuest(page);
    await mockCommon(page);
    mockNegotiateTwo(page, BUDGET_SHORTFALL_VIETNAM, PLAN_AFTER_RAISE);

    await page.goto('/');
    await page.locator('.guide-card').nth(3).click();

    await expect(page.getByTestId('state-card')).toBeVisible({ timeout: 10000 });

    await page.getByTestId('chat-input').fill('raise to $2000');
    await page.getByRole('button', { name: 'Send' }).click();

    // Recovery: itinerary eventually appears
    await expect(page.getByTestId('status-banner')).toContainText('Plan ready', { timeout: 12000 });
  });

  test('D-03: follow-up "shorten to 8 days" after Japan budget-shortfall prepends Japan prompt', async ({ page }) => {
    await preseedGuest(page);
    await mockCommon(page);
    const japanShortfall = {
      outcome: 'cannot_satisfy',
      budget_shortfall_cents: 20000,
      min_feasible_total_cents: 270000,
    };
    const { getSecondBlockingBody } = mockNegotiateTwo(page, japanShortfall, PLAN_READY_JAPAN);

    await page.goto('/');
    await page.locator('.guide-card').first().click();  // "10 days Japan, $2500"

    await expect(page.getByTestId('state-card')).toBeVisible({ timeout: 10000 });

    await page.getByTestId('chat-input').fill('shorten to 8 days');
    await page.getByRole('button', { name: 'Send' }).click();

    await expect.poll(() => getSecondBlockingBody(), { timeout: 8000 }).not.toBeNull();
    expect(getSecondBlockingBody()?.text).toBe('10 days Japan, $2500 — shorten to 8 days');
  });

  test('D-04a: typed request after plan_ready goes to /refine with exact text (no prepend)', async ({ page }) => {
    // After plan_ready, the floating chat POSTs to /refine (not /negotiate_text).
    // The test must open the flyout first (visibility:hidden when closed) and assert
    // the /refine body.message field — no "Old trip — " prepend.
    await preseedGuest(page);
    await mockCommon(page);
    mockNegotiate(page, PLAN_READY_JAPAN);   // first trip: Japan

    let capturedMessage: string | null = null;
    await page.route('**/refine', async (route) => {
      const body = JSON.parse(route.request().postData() ?? '{}') as Record<string, unknown>;
      capturedMessage = (body.message as string) ?? null;
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          outcome: 'plan_ready',
          idempotency_key: 'trip-fr-1',
          plan: PLAN_READY_FRANCE,
          assistant_reply: 'Starting fresh with Paris.',
        }),
      });
    });

    await page.goto('/');
    await page.locator('.guide-card').first().click();   // "10 days Japan, $2500"
    await expect(page.getByTestId('status-banner')).toContainText('Plan ready', { timeout: 12000 });

    // Open the floating assistant flyout (closed by default), then type and send
    await page.getByTestId('assistant-trigger').click();
    await expect(page.getByTestId('assistant-flyout')).toBeVisible({ timeout: 3000 });
    await page.getByTestId('chat-input').fill('7 days Paris');
    await page.getByTestId('chat-input').press('Enter');

    // /refine must receive exactly "7 days Paris" — no "10 days Japan, $2500 — " prepend
    await expect.poll(() => capturedMessage, { timeout: 6000 }).toBe('7 days Paris');
  });

  test('D-04: identical follow-up text does NOT get doubled context (same text guard)', async ({ page }) => {
    // Edge case: if user types the exact original prompt again, it should NOT become
    // "12 days Vietnam north to south, $1500 — 12 days Vietnam north to south, $1500"
    await preseedGuest(page);
    await mockCommon(page);
    const { getSecondBlockingBody } = mockNegotiateTwo(
      page, BUDGET_SHORTFALL_VIETNAM, PLAN_AFTER_RAISE
    );

    await page.goto('/');
    await page.locator('.guide-card').nth(3).click();
    await expect(page.getByTestId('state-card')).toBeVisible({ timeout: 10000 });

    // Follow-up IS the same text as the original
    await page.getByTestId('chat-input').fill('12 days Vietnam north to south, $1500');
    await page.getByRole('button', { name: 'Send' }).click();

    await expect.poll(() => getSecondBlockingBody(), { timeout: 8000 }).not.toBeNull();
    // priorUserMsg.text === text → NO prepend (the guard `if priorUserMsg.text !== text`)
    expect(getSecondBlockingBody()?.text).toBe('12 days Vietnam north to south, $1500');
  });
});

// ─────────────────────────────────────────────────────────────────────────────
// E. Declined / advisory — cannot_satisfy without budget_shortfall
// ─────────────────────────────────────────────────────────────────────────────

test.describe('E. Declined / advisory response', () => {

  test('E-01: declined response shows "I won\'t book this one" message', async ({ page }) => {
    await preseedGuest(page);
    await mockCommon(page);
    mockNegotiate(page, DECLINED_NO_BOOK);

    await page.goto('/');
    await page.getByTestId('guide-panel').locator('.rtab', { hasText: 'Africa' }).click();
    await page.locator('.guide-card').nth(1).click();   // Morocco

    const stateCard = page.getByTestId('state-card');
    await expect(stateCard).toBeVisible({ timeout: 10000 });
    // stateNote for 'declined': r.reason or fallback message
    await expect(stateCard).toContainText(/do-not-recommend|advisory|won't book/i);
  });

  test('E-02: declined response shows the server reason if provided', async ({ page }) => {
    await preseedGuest(page);
    await mockCommon(page);
    mockNegotiate(page, DECLINED_NO_BOOK);

    await page.goto('/');
    await page.getByTestId('guide-panel').locator('.rtab', { hasText: 'Africa' }).click();
    await page.locator('.guide-card').nth(1).click();

    await expect(page.getByTestId('state-card')).toContainText('EXC-ADVISORY', { timeout: 10000 });
  });

  test('E-03: declined fallback message when reason is absent', async ({ page }) => {
    await preseedGuest(page);
    await mockCommon(page);
    mockNegotiate(page, { outcome: 'cannot_satisfy' });   // no reason field

    await page.goto('/');
    await page.locator('.guide-card').nth(1).click();   // Bali → declined (no budget shortfall)

    const stateCard = page.getByTestId('state-card');
    await expect(stateCard).toBeVisible({ timeout: 10000 });
    await expect(stateCard).toContainText(/won't book|do-not-recommend|advisory/i);
  });

  test('E-04: needs_clarification strips "cannot_satisfy:" prefix from reason', async ({ page }) => {
    // cannot_satisfy with reason starting with "cannot_satisfy:" prefix
    // → stateNote strips it before rendering
    await preseedGuest(page);
    await mockCommon(page);
    mockNegotiate(page, {
      outcome: 'cannot_satisfy',
      reason: 'cannot_satisfy: No lodging inventory in our catalog for this destination.',
    });

    await page.goto('/');
    await page.locator('.guide-card').first().click();

    const stateCard = page.getByTestId('state-card');
    await expect(stateCard).toBeVisible({ timeout: 10000 });
    // Prefix stripped — should show only the human-readable part
    await expect(stateCard).not.toContainText('cannot_satisfy:');
    await expect(stateCard).toContainText('No lodging inventory');
  });
});

// ─────────────────────────────────────────────────────────────────────────────
// F. Insufficient funds — wallet too low for the guide card trip
// ─────────────────────────────────────────────────────────────────────────────

test.describe('F. Insufficient funds from guide card', () => {

  test('F-01: insufficient_funds shows trip cost and wallet balance', async ({ page }) => {
    await preseedGuest(page);
    await mockCommon(page);
    mockNegotiate(page, INSUFFICIENT_FUNDS);

    await page.goto('/');
    await page.locator('.guide-card').first().click();   // Japan $2500

    const stateCard = page.getByTestId('state-card');
    await expect(stateCard).toBeVisible({ timeout: 10000 });
    // stateNote: "Trip exceeds your funded wallet ($2,500.00 vs $800.00). Top up or trim the trip."
    await expect(stateCard).toContainText('$2,500.00');
    await expect(stateCard).toContainText('$800.00');
    await expect(stateCard).toContainText(/Top up|trim/i);
  });

  test('F-02: insufficient_funds does NOT show budget-shortfall text (distinct error types)', async ({ page }) => {
    await preseedGuest(page);
    await mockCommon(page);
    mockNegotiate(page, INSUFFICIENT_FUNDS);

    await page.goto('/');
    await page.locator('.guide-card').first().click();

    const stateCard = page.getByTestId('state-card');
    await expect(stateCard).toBeVisible({ timeout: 10000 });
    await expect(stateCard).not.toContainText('raise the budget');
  });
});

// ─────────────────────────────────────────────────────────────────────────────
// G. Persona recommendations — all 6 types
// ─────────────────────────────────────────────────────────────────────────────

test.describe('G. Persona recommendations', () => {

  const PERSONAS = [
    { persona: 'hiker',         label: 'hiker',        card: 'Nepal',         prompt: '14 days Nepal trekking, $2000, adventure' },
    { persona: 'family',        label: 'family',       card: 'Japan',         prompt: '10 days Japan family, $4000, family' },
    { persona: 'luxury',        label: 'luxury',       card: 'Maldives',      prompt: '7 days Maldives luxury, $5000' },
    { persona: 'budget',        label: 'budget',       card: 'Vietnam',       prompt: '14 days Vietnam budget, $1200' },
    { persona: 'digital_nomad', label: 'digital nomad', card: 'Bali',        prompt: '30 days Bali digital nomad, $2500, remote work' },
  ];

  for (const { persona, label, card, prompt } of PERSONAS) {
    test(`G-${persona}: "${label}" persona shows section label and correct first card`, async ({ page }) => {
      await preseedUser(page, {
        user_id: `demo-${persona}`, display_name: 'Test', persona,
        nationality: 'US', home_currency: 'USD', wallet_balance_cents: 600000,
      });
      await mockCommon(page);
      await page.goto('/');

      const guidePanel = page.getByTestId('guide-panel');
      await expect(guidePanel).toBeVisible({ timeout: 5000 });

      // Section label must show the persona name (underscores → spaces)
      await expect(guidePanel.locator('.guide-section-label')).toContainText(label, { timeout: 3000 });
      // First card must be the correct destination
      await expect(guidePanel.locator('.guide-card').first()).toContainText(card);
    });

    test(`G-${persona}-prompt: "${label}" first card sends the correct prompt`, async ({ page }) => {
      await preseedUser(page, {
        user_id: `demo-${persona}`, display_name: 'Test', persona,
        nationality: 'SG', home_currency: 'SGD', wallet_balance_cents: 600000,
      });
      await mockCommon(page);
      const { getBlockingBody } = mockNegotiate(page, PLAN_READY_JAPAN);

      await page.goto('/');
      await expect(page.getByTestId('guide-panel')).toBeVisible({ timeout: 5000 });
      await page.locator('.guide-card').first().click();

      await expect.poll(() => getBlockingBody()?.text, { timeout: 6000 }).toBe(prompt);
    });
  }

  test('G-logout: persona section disappears after switching to guest', async ({ page }) => {
    // Start as a foodie user so persona recs are visible
    await preseedUser(page, {
      user_id: 'demo-foodie', display_name: 'Foodie', persona: 'foodie',
      nationality: 'SG', home_currency: 'SGD', wallet_balance_cents: 500000,
    });
    await mockCommon(page);
    await page.route('**/session', (r) =>
      r.fulfill({ status: 200, contentType: 'application/json',
        body: JSON.stringify({ demo_users: [] }) }));

    await page.goto('/');
    await expect(page.getByTestId('guide-panel').locator('.guide-section-label')).toBeVisible({ timeout: 5000 });

    // Log out → switch to guest
    await page.getByTestId('switch-user').click();

    // After logout, the session picker may appear; navigate past it
    const picker = page.getByTestId('continue-guest');
    if (await picker.isVisible({ timeout: 2000 }).catch(() => false)) {
      await picker.click();
    }

    // Persona section should be gone
    await expect(page.getByTestId('guide-panel').locator('.guide-section-label')).not.toBeVisible();
  });
});

// ─────────────────────────────────────────────────────────────────────────────
// H. Nationality-based region defaults
// ─────────────────────────────────────────────────────────────────────────────

test.describe('H. Nationality-based active region', () => {

  test('H-01: US user defaults to Americas tab', async ({ page }) => {
    await preseedUser(page, {
      user_id: 'demo-us', display_name: 'Alex', persona: 'culture',
      nationality: 'US', home_currency: 'USD', wallet_balance_cents: 500000,
    });
    await mockCommon(page);
    await page.goto('/');

    const guidePanel = page.getByTestId('guide-panel');
    await expect(guidePanel).toBeVisible({ timeout: 5000 });

    // Americas tab must be aria-selected
    await expect(guidePanel.locator('.rtab[aria-selected="true"]')).toContainText('Americas');
    // First card must be USA (Americas first entry)
    await expect(guidePanel.locator('.guide-card').first()).toContainText('USA');
  });

  test('H-02: CA (Canada) user defaults to Americas tab', async ({ page }) => {
    await preseedUser(page, {
      user_id: 'demo-ca', display_name: 'Cam', persona: 'hiker',
      nationality: 'CA', home_currency: 'CAD', wallet_balance_cents: 500000,
    });
    await mockCommon(page);
    await page.goto('/');
    await expect(page.getByTestId('guide-panel').locator('.rtab[aria-selected="true"]'))
      .toContainText('Americas', { timeout: 5000 });
  });

  test('H-03: EG (Egypt) user defaults to Africa & Middle East tab', async ({ page }) => {
    await preseedUser(page, {
      user_id: 'demo-eg', display_name: 'Ehab', persona: 'culture',
      nationality: 'EG', home_currency: 'EGP', wallet_balance_cents: 500000,
    });
    await mockCommon(page);
    await page.goto('/');

    const guidePanel = page.getByTestId('guide-panel');
    await expect(guidePanel).toBeVisible({ timeout: 5000 });

    await expect(guidePanel.locator('.rtab[aria-selected="true"]')).toContainText('Africa', { timeout: 3000 });
    await expect(guidePanel.locator('.guide-card').first()).toContainText('Dubai');
  });

  test('H-04: SG user defaults to Asia tab', async ({ page }) => {
    await preseedUser(page, {
      user_id: 'demo-sg', display_name: 'Mei', persona: 'foodie',
      nationality: 'SG', home_currency: 'SGD', wallet_balance_cents: 500000,
    });
    await mockCommon(page);
    await page.goto('/');
    await expect(page.getByTestId('guide-panel').locator('.rtab[aria-selected="true"]'))
      .toContainText('Asia', { timeout: 5000 });
  });

  test('H-05: guest (no nationality) defaults to Asia tab', async ({ page }) => {
    await preseedGuest(page);
    await mockCommon(page);
    await page.goto('/');
    await expect(page.getByTestId('guide-panel').locator('.rtab[aria-selected="true"]'))
      .toContainText('Asia', { timeout: 5000 });
  });
});

// ─────────────────────────────────────────────────────────────────────────────
// I. Wallet passthrough — wallet_balance_cents in every guide card request
// ─────────────────────────────────────────────────────────────────────────────

test.describe('I. Wallet balance passthrough', () => {

  test('I-01: guest guide card sends default wallet (500000¢ = $5000)', async ({ page }) => {
    await preseedGuest(page);
    await mockCommon(page);
    const { getBlockingBody } = mockNegotiate(page, PLAN_READY_JAPAN);

    await page.goto('/');
    await page.locator('.guide-card').first().click();

    await expect.poll(() => getBlockingBody(), { timeout: 6000 }).not.toBeNull();
    expect(getBlockingBody()?.wallet_balance_cents).toBe(500000);
  });

  test('I-02: logged-in user guide card sends their wallet balance', async ({ page }) => {
    await preseedUser(page, {
      user_id: 'demo-mei', display_name: 'Mei', persona: 'foodie',
      nationality: 'SG', home_currency: 'SGD', wallet_balance_cents: 350000,
    });
    await mockCommon(page);
    const { getBlockingBody } = mockNegotiate(page, PLAN_READY_JAPAN);

    await page.goto('/');
    await page.locator('.guide-card').first().click();

    await expect.poll(() => getBlockingBody(), { timeout: 6000 }).not.toBeNull();
    expect(getBlockingBody()?.wallet_balance_cents).toBe(350000);
  });

  test('I-03: wallet shown in UI matches what is sent in request', async ({ page }) => {
    await preseedUser(page, {
      user_id: 'demo-alex', display_name: 'Alex', persona: 'hiker',
      nationality: 'US', home_currency: 'USD', wallet_balance_cents: 420000,
    });
    await mockCommon(page);
    const { getBlockingBody } = mockNegotiate(page, PLAN_READY_JAPAN);

    await page.goto('/');

    // The wallet input field should show the user's balance ($4,200)
    const walletInput = page.locator('input[type="number"]');
    await expect(walletInput).toHaveValue('4200', { timeout: 3000 });

    await page.getByTestId('guide-panel').locator('.rtab', { hasText: 'Americas' }).click();
    await page.locator('.guide-card').first().click();

    await expect.poll(() => getBlockingBody(), { timeout: 6000 }).not.toBeNull();
    expect(getBlockingBody()?.wallet_balance_cents).toBe(420000);
  });
});

// ─────────────────────────────────────────────────────────────────────────────
// J. Sequence replan — second guide card starts fresh after existing plan
// ─────────────────────────────────────────────────────────────────────────────

test.describe('J. Sequence: second guide card after active plan', () => {

  test('J-01: guide card after successful plan shows loading state, then new plan', async ({ page }) => {
    await preseedGuest(page);
    await mockCommon(page);
    const { getFirstBlockingBody, getSecondBlockingBody } =
      mockNegotiateTwo(page, PLAN_READY_JAPAN, PLAN_READY_FRANCE);

    await page.goto('/');

    // First trip: Japan
    await page.locator('.guide-card').first().click();
    await expect(page.getByTestId('status-banner')).toContainText('Plan ready', { timeout: 12000 });
    await expect(page.getByTestId('itinerary')).toContainText('Tokyo');

    // Click "Start new trip" to go back to empty state
    await page.getByTestId('new-trip').click();
    await expect(page.getByTestId('guide-panel')).toBeVisible({ timeout: 5000 });

    // Second trip: France (switch to Europe tab)
    await page.getByTestId('guide-panel').locator('.rtab', { hasText: 'Europe' }).click();
    await page.locator('.guide-card').first().click();

    await expect(page.getByTestId('itinerary')).toContainText('Paris', { timeout: 12000 });
    expect(getSecondBlockingBody()?.text).toBe('10 days France, $2500');
  });
});

// ─────────────────────────────────────────────────────────────────────────────
// K. Advisory safety flags surfaced in plan from guide card
// ─────────────────────────────────────────────────────────────────────────────

test.describe('K. Safety advisory in plan from guide card', () => {

  test('K-01: plan with civil_unrest advisory shows flag in Safety tab', async ({ page }) => {
    await preseedGuest(page);
    await mockCommon(page);
    mockNegotiate(page, PLAN_READY_WITH_ADVISORY);

    await page.goto('/');
    // Use Africa-ME tab → Jordan card
    await page.getByTestId('guide-panel').locator('.rtab', { hasText: 'Africa' }).click();
    await page.locator('.guide-card').nth(4).click();   // Jordan

    await expect(page.getByTestId('status-banner')).toContainText('Plan ready', { timeout: 12000 });

    // Safety tab must show advisory details
    await page.getByTestId('right-rail').getByRole('button', { name: 'Safety' }).click();
    await expect(page.getByTestId('tab-safety')).toContainText(/civil_unrest|Monitor.*advisories/i);
  });

  test('K-02: clean plan from guide card does not show safety advisories', async ({ page }) => {
    await preseedGuest(page);
    await mockCommon(page);
    mockNegotiate(page, PLAN_READY_JAPAN);

    await page.goto('/');
    await page.locator('.guide-card').first().click();

    await expect(page.getByTestId('status-banner')).toContainText('Plan ready', { timeout: 12000 });

    await page.getByTestId('right-rail').getByRole('button', { name: 'Safety' }).click();
    // No advisories in PLAN_READY_JAPAN → no advisory block rendered
    await expect(page.getByTestId('tab-safety')).not.toContainText('civil_unrest');
  });
});

// ─────────────────────────────────────────────────────────────────────────────
// J2. Context-leak guard — guide card click after successful plan
//
// Bug risk: result !== null triggers effectiveText prepend even after SUCCESS.
// Clicking a new guide card directly after "new trip" reset must clear the
// prior result so no prepend occurs.
// ─────────────────────────────────────────────────────────────────────────────

test.describe('J2. Context-leak guard: new guide card after prior success', () => {

  test('J2-01: second guide card (different tab) after plan_ready sends ONLY new prompt', async ({ page }) => {
    await preseedGuest(page);
    await mockCommon(page);
    const { getFirstBlockingBody, getSecondBlockingBody } =
      mockNegotiateTwo(page, PLAN_READY_JAPAN, PLAN_READY_FRANCE);

    await page.goto('/');

    // First trip: Japan
    await page.locator('.guide-card').first().click();
    await expect(page.getByTestId('status-banner')).toContainText('Plan ready', { timeout: 12000 });
    await expect(getFirstBlockingBody()?.text).toBe('10 days Japan, $2500');

    // Reset via "New trip"
    await page.getByTestId('new-trip').click();
    await expect(page.getByTestId('guide-panel')).toBeVisible({ timeout: 5000 });

    // Second trip: France
    await page.getByTestId('guide-panel').locator('.rtab', { hasText: 'Europe' }).click();
    await page.locator('.guide-card').first().click();

    await expect.poll(() => getSecondBlockingBody(), { timeout: 8000 }).not.toBeNull();

    // MUST be only the France prompt — NOT "10 days Japan, $2500 — 10 days France, $2500"
    expect(getSecondBlockingBody()?.text).toBe('10 days France, $2500');
    expect((getSecondBlockingBody()?.text as string ?? '').includes(' — ')).toBe(false);
  });
});

// ─────────────────────────────────────────────────────────────────────────────
// L. Request body completeness — fields the backend needs for correct routing
// ─────────────────────────────────────────────────────────────────────────────

test.describe('L. Request body completeness', () => {

  test('L-01: logged-in guide card sends user_id and nationality', async ({ page }) => {
    await preseedUser(page, {
      user_id: 'demo-mei', display_name: 'Mei', persona: 'foodie',
      nationality: 'SG', home_currency: 'SGD', wallet_balance_cents: 500000,
    });
    await mockCommon(page);
    const { getBlockingBody } = mockNegotiate(page, PLAN_READY_JAPAN);

    await page.goto('/');
    await page.locator('.guide-card').first().click();

    await expect.poll(() => getBlockingBody(), { timeout: 6000 }).not.toBeNull();
    expect(getBlockingBody()?.user_id).toBe('demo-mei');
    expect(getBlockingBody()?.nationality).toBe('SG');
  });

  test('L-02: guest guide card sends no user_id (null or absent)', async ({ page }) => {
    await preseedGuest(page);
    await mockCommon(page);
    const { getBlockingBody } = mockNegotiate(page, PLAN_READY_JAPAN);

    await page.goto('/');
    await page.locator('.guide-card').first().click();

    await expect.poll(() => getBlockingBody(), { timeout: 6000 }).not.toBeNull();
    const uid = getBlockingBody()?.user_id;
    expect(uid === null || uid === undefined || uid === '').toBe(true);
  });

  test('L-03: all 6 Asia guide card prompts are distinct and non-empty', async ({ page }) => {
    await preseedGuest(page);
    await mockCommon(page);

    const capturedTexts: string[] = [];

    await page.route('**/negotiate_text', async (route) => {
      const body = JSON.parse(route.request().postData() ?? '{}') as Record<string, unknown>;
      if (typeof body.text === 'string' && body.stream !== true) {
        capturedTexts.push(body.text as string);
      }
      await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({}) });
    });

    // Click each card on a fresh page load so result=null each time
    for (let i = 0; i < 6; i++) {
      await page.goto('/');
      await expect(page.getByTestId('guide-panel')).toBeVisible({ timeout: 5000 });
      await page.locator('.guide-card').nth(i).click();
      await page.waitForTimeout(1500);
    }

    await expect.poll(() => capturedTexts.length, { timeout: 5000 }).toBeGreaterThanOrEqual(6);

    // All 6 prompts must be non-empty and unique
    const unique = new Set(capturedTexts.slice(0, 6));
    expect(unique.size).toBe(6);
    capturedTexts.slice(0, 6).forEach((t) => expect(t.length).toBeGreaterThan(0));
  });

  test('L-04: plan:true sent for Europe, Americas, Africa-ME first cards', async ({ page }) => {
    await preseedGuest(page);
    await mockCommon(page);
    const capturedPlanFlags: boolean[] = [];

    await page.route('**/negotiate_text', async (route) => {
      const body = JSON.parse(route.request().postData() ?? '{}') as Record<string, unknown>;
      if (body.stream !== true) {
        capturedPlanFlags.push(body.plan as boolean);
      }
      await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({}) });
    });

    const TABS = ['Europe', 'Americas', 'Africa'];
    for (const tab of TABS) {
      await page.goto('/');
      await expect(page.getByTestId('guide-panel')).toBeVisible({ timeout: 5000 });
      await page.getByTestId('guide-panel').locator('.rtab', { hasText: tab }).click();
      await page.locator('.guide-card').first().click();
      await page.waitForTimeout(1500);
    }

    await expect.poll(() => capturedPlanFlags.length, { timeout: 5000 }).toBeGreaterThanOrEqual(3);
    capturedPlanFlags.slice(0, 3).forEach((flag) => expect(flag).toBe(true));
  });
});

// ─────────────────────────────────────────────────────────────────────────────
// Mobile viewport — guide card interaction on small screens
// ─────────────────────────────────────────────────────────────────────────────

test.describe('Mobile viewport (390×844 — iPhone 14)', () => {
  test.use({ viewport: { width: 390, height: 844 } });

  test('M-01: guide panel and chat bubble both visible on mobile', async ({ page }) => {
    await preseedGuest(page);
    await mockCommon(page);
    await page.goto('/');

    await expect(page.getByTestId('guide-panel')).toBeVisible({ timeout: 5000 });
    // On mobile the full-width chat textarea is hidden; the floating bubble is the entry point.
    await expect(page.getByRole('button', { name: 'Open assistant' })).toBeVisible({ timeout: 5000 });
  });

  test('M-02: guide card click triggers plan on mobile viewport', async ({ page }) => {
    await preseedGuest(page);
    await mockCommon(page);
    const { getBlockingBody } = mockNegotiate(page, PLAN_READY_JAPAN);

    await page.goto('/');
    await expect(page.getByTestId('guide-panel')).toBeVisible({ timeout: 5000 });
    await page.locator('.guide-card').first().click();

    await expect.poll(() => getBlockingBody(), { timeout: 8000 }).not.toBeNull();
    expect(getBlockingBody()?.text).toBe('10 days Japan, $2500');
    expect(getBlockingBody()?.plan).toBe(true);
  });

  test('M-03: guide panel does not overflow viewport width on mobile', async ({ page }) => {
    await preseedGuest(page);
    await mockCommon(page);
    await page.goto('/');

    const guidePanel = page.getByTestId('guide-panel');
    await expect(guidePanel).toBeVisible({ timeout: 5000 });

    const box = await guidePanel.boundingBox();
    expect(box).not.toBeNull();
    // Panel right edge must not exceed viewport width (390px)
    expect((box!.x + box!.width)).toBeLessThanOrEqual(390 + 1); // 1px tolerance
  });
});

// ─────────────────────────────────────────────────────────────────────────────
// N. New itinerary UI — hotel name, meal edit button, on:suggest flow
// ─────────────────────────────────────────────────────────────────────────────

// Fixture: one leg with hotel_title + a day with a Lunch meal.
// meals keys must be lowercase to match mealAnchoredTimeline's meals.lunch lookup.
const PLAN_WITH_HOTEL_AND_MEALS = {
  outcome: 'plan_ready',
  payment_status: 'held',
  booking_ref: null,
  idempotency_key: 'trip-hotel-1',
  package_total_with_fees_cents: 220000,
  total_budget_cents: 250000,
  wallet: { balance_cents: 500000, held_cents: 220000, debited: false },
  fee_line_items: [{ description: 'Lodging', usd_cents: 180000 }],
  legs: [
    {
      leg_id: 'leg-0', city: 'Tokyo',
      checkin: '2026-09-01', checkout: '2026-09-05',
      lat: 35.68, lng: 139.69,
      hotel_title: 'Park Hyatt Tokyo',
    },
  ],
  day_plans: [
    {
      leg_id: 'leg-0', city: 'Tokyo', num_days: 1,
      days: [{
        day_index: 0, bad_weather: false,
        attractions: [],
        meals: { lunch: { name: 'Ramen at Ichiran', lat: 35.71, lon: 139.78 } },
        intracity_hops: [],
      }],
      unscheduled_attractions: [],
    },
  ],
  risk_signals: { per_leg: [] },
};

test.describe('N. Itinerary UI — hotel name & meal edit', () => {

  test('N-01: hotel_title rendered as hotel-name row in leg header', async ({ page }) => {
    await preseedGuest(page);
    await mockCommon(page);
    mockNegotiate(page, PLAN_WITH_HOTEL_AND_MEALS);

    await page.goto('/');
    await page.locator('.guide-card').first().click();

    await expect(page.getByTestId('status-banner')).toContainText('Plan ready', { timeout: 12000 });
    const hotelName = page.getByTestId('hotel-name-0');
    await expect(hotelName).toBeVisible();
    await expect(hotelName).toContainText('Park Hyatt Tokyo');
  });

  test('N-02: meal edit ✎ button visible for meal items in editable (plan_ready) state', async ({ page }) => {
    await preseedGuest(page);
    await mockCommon(page);
    mockNegotiate(page, PLAN_WITH_HOTEL_AND_MEALS);

    await page.goto('/');
    await page.locator('.guide-card').first().click();

    await expect(page.getByTestId('status-banner')).toContainText('Plan ready', { timeout: 12000 });
    const mealEditBtn = page.locator('[data-testid^="meal-edit-"]');
    await expect(mealEditBtn.first()).toBeVisible();
  });

  test('N-03: meal edit ✎ click opens the inline swap panel; picking an alternative sends /replan swap_meal', async ({ page }) => {
    await preseedGuest(page);
    await mockCommon(page);

    // #121 meal-swap redesign: ✎ no longer sends a chat refine message — it opens an
    // inline panel listing day.meal_pool[slot] as msp-chip buttons; clicking one calls
    // /replan with a structured swap_meal op (see Itinerary.svelte openMealPanel/runOps).
    // NOTE: the timeline item's slot is the display-cased label ('Lunch', per itinerary.ts
    // MEAL_SLOTS), NOT the lowercase Meals key ('lunch') — meal_pool must be keyed the same way.
    const PLAN_WITH_MEAL_POOL = {
      ...PLAN_WITH_HOTEL_AND_MEALS,
      day_plans: [{
        ...PLAN_WITH_HOTEL_AND_MEALS.day_plans[0],
        days: [{
          ...PLAN_WITH_HOTEL_AND_MEALS.day_plans[0].days[0],
          meal_pool: { Lunch: [{ name: 'Sushi Dai', cuisine: 'sushi' }] },
        }],
      }],
    };
    mockNegotiate(page, PLAN_WITH_MEAL_POOL);

    let replanBody: Record<string, unknown> | null = null;
    await page.route('**/replan', async (route) => {
      replanBody = JSON.parse(route.request().postData() ?? '{}') as Record<string, unknown>;
      await route.fulfill({
        status: 200, contentType: 'application/json',
        body: JSON.stringify({ outcome: 'plan_ready', idempotency_key: 'trip-hotel-1',
          applied: ['swap_meal:leg0.day0.Lunch'], plan: PLAN_WITH_MEAL_POOL }),
      });
    });

    await page.goto('/');
    await page.locator('.guide-card').first().click();
    await expect(page.getByTestId('status-banner')).toContainText('Plan ready', { timeout: 12000 });

    const mealEditBtn = page.locator('[data-testid^="meal-edit-"]').first();
    await expect(mealEditBtn).toBeVisible();
    await mealEditBtn.click();

    // Inline swap panel opens with the pool's alternative(s) as chips.
    const panel = page.getByTestId('meal-swap-panel-0-0-Lunch');
    await expect(panel).toBeVisible({ timeout: 5000 });
    const chip = page.getByTestId('msp-chip-Sushi Dai');
    await expect(chip).toBeVisible();

    await chip.click();

    await expect.poll(() => replanBody, { timeout: 5000 }).not.toBeNull();
    expect(replanBody!.idempotency_key).toBe('trip-hotel-1');
    expect(replanBody!.ops).toEqual([
      { op: 'swap_meal', leg_index: 0, day_index: 0, slot: 'Lunch', restaurant_name: 'Sushi Dai' },
    ]);

    // Panel closes after a successful swap.
    await expect(panel).toHaveCount(0);
  });
});

// ─────────────────────────────────────────────────────────────────────────────
// H addendum — Gulf nationality codes → Africa & Middle East default region tab
// ─────────────────────────────────────────────────────────────────────────────

test.describe('H (addendum). Gulf nationality → Africa & Middle East tab', () => {

  test('H-06: AE (UAE) user defaults to Africa & Middle East tab', async ({ page }) => {
    await preseedUser(page, {
      user_id: 'demo-ae', display_name: 'Aisha', persona: 'luxury',
      nationality: 'AE', home_currency: 'AED', wallet_balance_cents: 1000000,
    });
    await mockCommon(page);
    await page.goto('/');

    const guidePanel = page.getByTestId('guide-panel');
    await expect(guidePanel).toBeVisible({ timeout: 5000 });
    await expect(guidePanel.locator('.rtab[aria-selected="true"]'))
      .toContainText('Africa', { timeout: 3000 });
  });

  test('H-07: SA (Saudi Arabia) user defaults to Africa & Middle East tab', async ({ page }) => {
    await preseedUser(page, {
      user_id: 'demo-sa', display_name: 'Faris', persona: 'culture',
      nationality: 'SA', home_currency: 'SAR', wallet_balance_cents: 1000000,
    });
    await mockCommon(page);
    await page.goto('/');

    await expect(page.getByTestId('guide-panel').locator('.rtab[aria-selected="true"]'))
      .toContainText('Africa', { timeout: 5000 });
  });
});

// ─────────────────────────────────────────────────────────────────────────────
// O. Suggestion chip click-to-add (itinerary editable rail)
// ─────────────────────────────────────────────────────────────────────────────

const PLAN_WITH_UNSCHEDULED = {
  outcome: 'plan_ready',
  payment_status: 'held',
  booking_ref: null,
  idempotency_key: 'trip-sug-1',
  package_total_with_fees_cents: 150000,
  total_budget_cents: 300000,
  wallet: { balance_cents: 500000, held_cents: 150000, debited: false },
  fee_line_items: [{ description: 'Lodging', usd_cents: 150000 }],
  legs: [{ leg_id: 'leg-0', city: 'Tokyo', checkin: '2026-10-01', checkout: '2026-10-04', lat: 35.68, lng: 139.69 }],
  day_plans: [{
    leg_id: 'leg-0', city: 'Tokyo', num_days: 3,
    days: [{
      day_index: 0, bad_weather: false,
      attractions: [{ name: 'Senso-ji', category: 'tourism=temple', lat: 35.71, lon: 139.79, fee: null }],
      meals: {},
      intracity_hops: [],
    }],
    unscheduled_attractions: [
      { name: 'TeamLab', category: 'tourism=museum', lat: 35.65, lon: 139.79, fee: 3200, ua_ref: 'teamlab-0' },
      { name: 'Ueno Park', category: 'leisure=park', lat: 35.71, lon: 139.77, fee: null, ua_ref: 'ueno-1' },
    ],
  }],
  risk_signals: { per_leg: [] },
};

const REPLAN_NOOP = { outcome: 'noop', applied: [], rejected: [], notes: [], plan: PLAN_WITH_UNSCHEDULED };

test.describe('O. Suggestion chip click-to-add', () => {

  test('O-01: suggestion chip has pointer cursor affordance and click-to-add tooltip', async ({ page }) => {
    await preseedGuest(page);
    await mockCommon(page);
    mockNegotiate(page, PLAN_WITH_UNSCHEDULED);
    await page.route('**/replan', (route) =>
      route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(REPLAN_NOOP) }));

    await page.goto('/');
    await page.locator('.guide-card').first().click();
    await expect(page.getByTestId('status-banner')).toContainText('Plan ready', { timeout: 12000 });

    // Chip exists in day suggestions rail
    const chip = page.locator('[data-testid^="sug-chip-"]').first();
    await expect(chip).toBeVisible({ timeout: 5000 });

    // Title reflects click affordance
    const title = await chip.getAttribute('title');
    expect(title).toContain('Click to add');
  });

  test('O-02: clicking suggestion chip calls /replan with add_place op', async ({ page }) => {
    await preseedGuest(page);
    await mockCommon(page);
    mockNegotiate(page, PLAN_WITH_UNSCHEDULED);

    let capturedOps: unknown[] | null = null;
    await page.route('**/replan', async (route) => {
      const body = JSON.parse(route.request().postData() ?? '{}') as Record<string, unknown>;
      capturedOps = body.ops as unknown[];
      await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(REPLAN_NOOP) });
    });

    await page.goto('/');
    await page.locator('.guide-card').first().click();
    await expect(page.getByTestId('status-banner')).toContainText('Plan ready', { timeout: 12000 });

    const chip = page.locator('[data-testid^="sug-chip-"]').first();
    await chip.click();

    // /replan must be called with an add_place op from the unscheduled pool
    await expect.poll(() => capturedOps, { timeout: 5000 }).not.toBeNull();
    expect(Array.isArray(capturedOps)).toBe(true);
    const addOp = (capturedOps as Record<string, unknown>[]).find((op) => op.op === 'add_place');
    expect(addOp).toBeTruthy();
    expect(addOp?.from).toBe('unscheduled');
  });

});

// ─────────────────────────────────────────────────────────────────────────────
// P. Destination photos per region tab
//
// Per the sourced-photo implementation (App.svelte REGION_TABS), all 24
// destinations across the 4 region tabs (Asia, Europe, Americas, Africa &
// Middle East) shipped with a real thumb under src/lib/assets/destinations/
// thumbs/ — so every card renders `<img class="guide-photo">` and none fall
// back to the `<span class="guide-icon">` emoji. This is NOT true of every
// GuideDest in the app: PERSONA_RECS mixes region-tab destinations that do
// have a photo with several (Nepal, New Zealand, Iceland, Australia,
// Maldives, Seychelles, French Riviera, Chiang Mai, Medellín) that don't —
// those stay on the emoji fallback and are out of scope for the region-tab
// count asserted here.
// ─────────────────────────────────────────────────────────────────────────────

test.describe('P. Destination photos per region tab', () => {

  test('P-01: Asia tab (default) — 6 cards, all 6 show real photos, 0 emoji fallback', async ({ page }) => {
    await preseedGuest(page);
    await mockCommon(page);
    await page.goto('/');
    await expect(page.getByTestId('guide-panel')).toBeVisible({ timeout: 5000 });

    const guidePanel = page.getByTestId('guide-panel');
    await expect(guidePanel.locator('.guide-card')).toHaveCount(6);
    await expect(guidePanel.locator('.guide-card .guide-photo')).toHaveCount(6);
    await expect(guidePanel.locator('.guide-card .guide-icon')).toHaveCount(0);
  });

  test('P-02: Europe tab — 6 cards, all 6 show real photos, 0 emoji fallback', async ({ page }) => {
    await preseedGuest(page);
    await mockCommon(page);
    await page.goto('/');
    await expect(page.getByTestId('guide-panel')).toBeVisible({ timeout: 5000 });

    const guidePanel = page.getByTestId('guide-panel');
    await guidePanel.locator('.rtab', { hasText: 'Europe' }).click();

    await expect(guidePanel.locator('.guide-card')).toHaveCount(6);
    await expect(guidePanel.locator('.guide-card .guide-photo')).toHaveCount(6);
    await expect(guidePanel.locator('.guide-card .guide-icon')).toHaveCount(0);
  });

  test('P-03: Americas tab — 6 cards, all 6 show real photos, 0 emoji fallback', async ({ page }) => {
    await preseedGuest(page);
    await mockCommon(page);
    await page.goto('/');
    await expect(page.getByTestId('guide-panel')).toBeVisible({ timeout: 5000 });

    const guidePanel = page.getByTestId('guide-panel');
    await guidePanel.locator('.rtab', { hasText: 'Americas' }).click();

    await expect(guidePanel.locator('.guide-card')).toHaveCount(6);
    await expect(guidePanel.locator('.guide-card .guide-photo')).toHaveCount(6);
    await expect(guidePanel.locator('.guide-card .guide-icon')).toHaveCount(0);
  });

  test('P-04: Africa & Middle East tab — 6 cards, all 6 show real photos, 0 emoji fallback', async ({ page }) => {
    await preseedGuest(page);
    await mockCommon(page);
    await page.goto('/');
    await expect(page.getByTestId('guide-panel')).toBeVisible({ timeout: 5000 });

    const guidePanel = page.getByTestId('guide-panel');
    await guidePanel.locator('.rtab', { hasText: 'Africa' }).click();

    await expect(guidePanel.locator('.guide-card')).toHaveCount(6);
    await expect(guidePanel.locator('.guide-card .guide-photo')).toHaveCount(6);
    await expect(guidePanel.locator('.guide-card .guide-icon')).toHaveCount(0);
  });

  test('P-05: guide-photo <img> elements carry a resolved (non-empty) src', async ({ page }) => {
    await preseedGuest(page);
    await mockCommon(page);
    await page.goto('/');
    await expect(page.getByTestId('guide-panel')).toBeVisible({ timeout: 5000 });

    const photos = page.getByTestId('guide-panel').locator('.guide-card .guide-photo');
    const count = await photos.count();
    for (let i = 0; i < count; i++) {
      const src = await photos.nth(i).getAttribute('src');
      expect(src).toBeTruthy();
    }
  });

});
