import { test, expect, type Page } from '@playwright/test';

// name-tier-chip-overflow.spec.ts — #181 (G1/G2) geometry regression coverage.
//
// vitest/jsdom (Itinerary.name-tier-gaps.test.ts) already locks the CONTENT of the
// day-sug-chip / msp-chip name-tier fix (unreadable indicator, name_en primary, local
// companion). What jsdom CANNOT verify is layout geometry, so this spec is the one
// place asserting the CSS side of the fix: a long, unbroken served name (no whitespace
// to wrap on — the exact shape a CJK/Thai/etc. name with no name_en can take) must not
// push its chip past its container, and must not force the page to scroll horizontally.
//
// All routes mocked — no live backend.

const LONG_NAME = 'Donaudampfschifffahrtsgesellschaftskapitaenskajuetenbesichtigungsverzeichnis';

const PLAN_WITH_LONG_SUGGESTION_AND_MEAL_POOL = {
  outcome: 'plan_ready',
  payment_status: 'held',
  booking_ref: null,
  idempotency_key: 'trip-overflow-1',
  package_total_with_fees_cents: 100000,
  total_budget_cents: 150000,
  wallet: { balance_cents: 500000, held_cents: 100000, debited: false },
  fee_line_items: [{ description: 'Lodging', usd_cents: 100000 }],
  legs: [{ leg_id: 'leg-0', city: 'Vienna', checkin: '2026-09-01', checkout: '2026-09-03', lat: 48.21, lng: 16.37 }],
  day_plans: [{
    leg_id: 'leg-0', city: 'Vienna', num_days: 2,
    days: [{
      day_index: 0, bad_weather: false,
      attractions: [],
      meals: { lunch: { name: 'Ramen Counter', category: 'amenity=restaurant', cuisine: 'ramen', lat: 48.21, lon: 16.37 } },
      meal_pool: { Lunch: [{ name: LONG_NAME, cuisine: 'ramen' }] }, // #181 G2: keyed by display-cased slot
      intracity_hops: [],
    }],
    unscheduled_attractions: [{ name: LONG_NAME, category: 'tourism=attraction', lat: 48.22, lon: 16.38 }], // #181 G1
  }],
  risk_signals: { per_leg: [] },
};

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

/** Mirrors guide-cards.spec.ts's mockNegotiate: stream:true attempt degrades, blocking fallback delivers the plan. */
function mockNegotiate(page: Page, plan: unknown): void {
  let call = 0;
  page.route('**/negotiate_text', async (route) => {
    call++;
    if (call % 2 === 1) {
      await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({}) });
    } else {
      await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(plan) });
    }
  });
}

test.describe('#181 G1/G2 — chip overflow guard', () => {
  test('per-day suggestion chip (.day-sug-chip) never overflows its day card, and meal-swap chip (.msp-chip) never overflows its panel', async ({ page }) => {
    await preseedGuest(page);
    await mockCommon(page);
    mockNegotiate(page, PLAN_WITH_LONG_SUGGESTION_AND_MEAL_POOL);

    await page.goto('/');
    await page.locator('.guide-card').first().click();
    await expect(page.getByTestId('status-banner')).toContainText('Plan ready', { timeout: 12000 });

    // G1: per-day suggestion chip vs its containing .day-sugs row.
    const sugChip = page.locator('.day-sug-chip').first();
    await expect(sugChip).toBeVisible();
    const sugRow = page.locator('.day-sugs').first();
    const [chipBox, rowBox] = [await sugChip.boundingBox(), await sugRow.boundingBox()];
    expect(chipBox).not.toBeNull();
    expect(rowBox).not.toBeNull();
    expect(chipBox!.x + chipBox!.width).toBeLessThanOrEqual(rowBox!.x + rowBox!.width + 1);

    // G2: meal-swap panel alternative chip vs its containing .meal-swap-panel.
    await page.locator('[data-testid^="meal-edit-"]').first().click();
    const mspChip = page.locator('.msp-chip').first();
    await expect(mspChip).toBeVisible();
    const mspPanel = page.locator('.meal-swap-panel').first();
    const [mspBox, panelBox] = [await mspChip.boundingBox(), await mspPanel.boundingBox()];
    expect(mspBox).not.toBeNull();
    expect(panelBox).not.toBeNull();
    expect(mspBox!.x + mspBox!.width).toBeLessThanOrEqual(panelBox!.x + panelBox!.width + 1);

    // Neither chip forces the page itself to scroll horizontally.
    const noHorizontalScroll = await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth + 1);
    expect(noHorizontalScroll).toBe(true);
  });
});
