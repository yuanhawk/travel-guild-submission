import { test, expect, type Page } from '@playwright/test';

// mobile-fab-overlap.spec.ts — Finding #8 (map/mobile UX sweep) regression.
//
// ChatPane's floating assistant trigger (.bot-trigger, position:fixed, bottom-left,
// ~68px footprint) sat on top of whatever RightRail's own pane-scroll happened to
// bring to the bottom — the reported cases were the Safety tab's insurance reference
// link and the Map tab's OSM attribution corner. B5 (see place-card.spec.ts) already
// reserves this same space for SHEETS via hideMobileBubble, but ordinary
// scrolled-under pane content had no reservation at all. Fixed via a mobile-only
// `.pane { padding-bottom: 84px }` (RightRail.svelte) — the same
// padding-bottom-reservation pattern Preview.svelte already uses for its own
// persistent bottom bar.

const PLAN_WITH_INSURANCE_LINK = {
  outcome: 'plan_ready',
  payment_status: 'held',
  booking_ref: null,
  idempotency_key: 'trip-fab-overlap-1',
  package_total_with_fees_cents: 100000,
  total_budget_cents: 200000,
  wallet: { balance_cents: 500000, held_cents: 100000, debited: false },
  legs: [{ leg_id: 'leg-0', city: 'Lisbon', checkin: '2026-09-01', checkout: '2026-09-03' }],
  day_plans: [{
    leg_id: 'leg-0', city: 'Lisbon', num_days: 2,
    days: [{ day_index: 0, bad_weather: false, attractions: [], meals: {}, intracity_hops: [] }],
  }],
  risk_signals: { per_leg: [] },
  booking_links: {
    insurance: {
      booking_url: null,
      kind: 'compare_note',
      label: 'Compare coverage independently (no vendor plan offered)',
      providers: null,
    },
  },
};

async function preseedGuest(page: Page): Promise<void> {
  await page.addInitScript(() => {
    localStorage.setItem('tg_session', JSON.stringify({ user: null, guest: true, savedAt: Date.now() }));
  });
}

async function mockCommon(page: Page): Promise<void> {
  await page.route('**/emergencies', (r) =>
    r.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ status: 'ok', countries: [] }) }));
  await page.route(/tile|openstreetmap|basemaps|demotiles/, (r) => r.abort());
}

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

test.describe('Finding #8 — mobile Safety-tab content no longer sits under the assistant FAB (390×844)', () => {
  test.use({ viewport: { width: 390, height: 844 } });

  test('the pane reserves bottom space for the trigger, and the insurance link never overlaps it', async ({ page }) => {
    await preseedGuest(page);
    await mockCommon(page);
    mockNegotiate(page, PLAN_WITH_INSURANCE_LINK);

    await page.goto('/');
    await page.locator('.guide-card').first().click();
    await expect(page.getByTestId('status-banner')).toContainText('Plan ready', { timeout: 12000 });

    await page.getByTestId('right-rail').getByRole('button', { name: 'Safety' }).click();
    const pane = page.getByTestId('tab-safety');
    await expect(pane).toBeVisible();

    // The reservation itself: mobile-only padding-bottom big enough to clear the
    // trigger's ~68px fixed footprint.
    const paddingBottom = await pane.evaluate((el) => parseFloat(getComputedStyle(el).paddingBottom));
    expect(paddingBottom).toBeGreaterThanOrEqual(68);

    // The real-world proof: scroll the pane's own content to its bottom, then check
    // the insurance link's bounding box doesn't intersect the trigger's.
    const insuranceLink = page.getByTestId('booking-link-group-insurance');
    await expect(insuranceLink).toBeVisible();
    await insuranceLink.scrollIntoViewIfNeeded();

    const trigger = page.getByTestId('assistant-trigger');
    await expect(trigger).toBeVisible();

    const [linkBox, triggerBox] = [await insuranceLink.boundingBox(), await trigger.boundingBox()];
    expect(linkBox).not.toBeNull();
    expect(triggerBox).not.toBeNull();
    // No vertical overlap: the link's bottom edge must sit above the trigger's top edge.
    expect(linkBox!.y + linkBox!.height).toBeLessThanOrEqual(triggerBox!.y + 1);
  });
});
