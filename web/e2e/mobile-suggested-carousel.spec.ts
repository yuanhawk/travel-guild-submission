import { test, expect, type Page } from '@playwright/test';

// mobile-suggested-carousel.spec.ts — Finding #5 (map/mobile UX sweep) regression.
//
// RightRail.svelte's ≤768px `.sug-list`/`.sug`/`.sug-ctrl` media query used to be
// declared BEFORE the equal-specificity base rules for the same selectors, so the
// cascade's source-order tiebreak let the later, unconditional base rules win even
// on mobile: the horizontal carousel never activated (.sug-list stayed
// flex-direction:column) and long, unbroken suggestion names — which had no width
// constraint on `.sug-b` to ellipsis against — rendered past their 240px card and
// off the viewport edge. jsdom/vitest can't compute real CSS-cascade/layout, so
// this is the one place asserting it live.

const LONG_NAME = 'MAAT - Museum of Art, Architecture and Technology of Lisbon Waterfront';

const PLAN_WITH_LONG_SUGGESTION = {
  outcome: 'plan_ready',
  payment_status: 'held',
  booking_ref: null,
  idempotency_key: 'trip-mobile-sug-1',
  package_total_with_fees_cents: 100000,
  total_budget_cents: 200000,
  wallet: { balance_cents: 500000, held_cents: 100000, debited: false },
  fee_line_items: [{ description: 'Lodging', usd_cents: 100000 }],
  legs: [{ leg_id: 'leg-0', city: 'Lisbon', checkin: '2026-09-01', checkout: '2026-09-05', lat: 38.72, lng: -9.14 }],
  day_plans: [{
    leg_id: 'leg-0', city: 'Lisbon', num_days: 4,
    days: [{ day_index: 0, bad_weather: false, attractions: [], meals: {}, intracity_hops: [] }],
    unscheduled_attractions: [
      { name: LONG_NAME, category: 'tourism=museum', lat: 38.695, lon: -9.194 },
      { name: 'Belem Tower', category: 'tourism=monument', lat: 38.691, lon: -9.216 },
    ],
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

/** Mirrors name-tier-chip-overflow.spec.ts's mockNegotiate: stream:true attempt
 *  degrades, blocking fallback delivers the plan. */
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

test.describe('Finding #5 — mobile Suggested-tab carousel (390×844)', () => {
  test.use({ viewport: { width: 390, height: 844 } });

  test('the horizontal carousel activates, and a long name never overflows its card or the viewport', async ({ page }) => {
    await preseedGuest(page);
    await mockCommon(page);
    mockNegotiate(page, PLAN_WITH_LONG_SUGGESTION);

    await page.goto('/');
    await page.locator('.guide-card').first().click();
    await expect(page.getByTestId('status-banner')).toContainText('Plan ready', { timeout: 12000 });

    await page.getByRole('button', { name: 'Suggested' }).click();
    const sugList = page.getByTestId('sug-list');
    await expect(sugList).toBeVisible();

    // Carousel activated: a horizontal, scrollable flex row (NOT the vertical list).
    const flexDirection = await sugList.evaluate((el) => getComputedStyle(el).flexDirection);
    expect(flexDirection).toBe('row');
    const overflowX = await sugList.evaluate((el) => getComputedStyle(el).overflowX);
    expect(overflowX).toBe('auto');

    // The long-name card's name element must stay within its own card...
    const longCard = page.getByTestId(/^sug-item-/).first();
    const nameEl = longCard.locator('.nm').first();
    const [nameBox, cardBox] = [await nameEl.boundingBox(), await longCard.boundingBox()];
    expect(nameBox).not.toBeNull();
    expect(cardBox).not.toBeNull();
    expect(nameBox!.x).toBeGreaterThanOrEqual(cardBox!.x - 1);
    expect(nameBox!.x + nameBox!.width).toBeLessThanOrEqual(cardBox!.x + cardBox!.width + 1);

    // ...and never bleed off the left edge of the viewport (the reported symptom).
    expect(nameBox!.x).toBeGreaterThanOrEqual(0);

    // No page-level horizontal scroll/overflow introduced by the carousel.
    const hasHorizontalOverflow = await page.evaluate(
      () => document.documentElement.scrollWidth > document.documentElement.clientWidth + 1,
    );
    expect(hasHorizontalOverflow).toBe(false);
  });
});
