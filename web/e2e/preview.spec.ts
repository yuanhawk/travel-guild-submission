import { test, expect } from '@playwright/test';

// Preview / save-to-phone E2E (hermetic): plan → Confirm & Book → booked → open the
// trip summary → the LLM narrative renders → the .ics download triggers. Mocked routes.
//
// Covers the trip-summary redesign (Draft 2, hero band + top-aligned accent budget
// card on desktop; Draft 3, collapsed budget bar + slide-up breakdown sheet on
// mobile — see Preview.svelte's tech-notes) and the accompanying ChatPane change:
// while Preview is open, ChatPane's floating mobile bubble (.bot-trigger / .flyout,
// hideMobileBubble prop) must be hidden on mobile widths so it doesn't collide with
// Preview's fixed bottom budget bar.
//
// Also covers two later bugfixes to the same component:
//  (a) multi-country route attribution — the hero route must never append a single
//      leg's country to a route that spans 2+ distinct countries (would fabricate
//      e.g. "Tokyo → Seoul, Japan").
//  (b) Draft 3's mobile budget bar starts COLLAPSED (not always-expanded like the
//      Draft 2 mobile treatment it replaced) and toggles a slide-up sheet.

const PLAN_READY = {
  outcome: 'plan_ready', payment_status: 'held', booking_ref: null, idempotency_key: 'trip-pv-1',
  package_total_with_fees_cents: 184000, total_budget_cents: 300000,
  wallet: { balance_cents: 500000, held_cents: 184000, debited: false },
  legs: [{ leg_id: 'leg-0', city: 'Tokyo', country: 'Japan', checkin: '2026-10-01', checkout: '2026-10-04' }],
  day_plans: [{
    leg_id: 'leg-0', city: 'Tokyo', country: 'Japan', checkin: '2026-10-01', checkout: '2026-10-04', num_days: 3,
    days: [{ day_index: 0, meals: { breakfast: { name: 'Kissaten' } },
      attractions: [{ name: 'Senso-ji', category: 'tourism=attraction' }] }],
  }],
};
const BOOKED = {
  ...PLAN_READY, outcome: 'success', payment_status: 'charged', booking_ref: 'BK-PV-9',
  wallet: { balance_cents: 316000, debited: true, debit_cents: 184000 },
  // itinerary_narrative is an object with .overview (NOT a flat string) — see ItineraryNarrative type.
  // assistantSummary() reads .overview; a flat string would produce overview=undefined → no narrative block.
  itinerary_narrative: { overview: 'A gentle first day in Tokyo.\n\nThen onward to the mountains.' },
};
// A booked envelope whose narrative was flagged stale by the backend after a
// structural /replan edit (server.py:2282-2288) — overview text is unchanged but
// stale/stale_reason are now set. The narrative panel must show an honest "may
// be outdated" caveat alongside the (still-rendered) overview text.
const BOOKED_STALE_NARRATIVE = {
  ...BOOKED,
  booking_ref: 'BK-PV-STALE',
  idempotency_key: 'trip-pv-stale',
  itinerary_narrative: {
    overview: 'A gentle first day in Tokyo.\n\nThen onward to the mountains.',
    stale: true,
    stale_reason: "This AI-written summary was generated before your latest edit and may still describe stops that were since removed, added, or reordered.",
  },
};
// Same but stale explicitly false — must render identically to the normal BOOKED
// case, i.e. no caveat (regression guard for the "false, not just absent" case).
const BOOKED_NOT_STALE_NARRATIVE = {
  ...BOOKED,
  booking_ref: 'BK-PV-NOTSTALE',
  idempotency_key: 'trip-pv-notstale',
  itinerary_narrative: { ...BOOKED.itinerary_narrative, stale: false },
};
// Same but with NO narrative — the LLM-off / no-fabrication path.
// itinerary_narrative is omitted entirely (not just an empty string) to match the
// real server contract for an LLM-off run.
const BOOKED_NO_NARRATIVE = (() => {
  const { itinerary_narrative, ...rest } = BOOKED as typeof BOOKED & { itinerary_narrative?: unknown };
  return { ...rest, booking_ref: 'BK-PV-10', idempotency_key: 'trip-pv-2' };
})();
// Multi-country route (2 legs, 2 distinct countries) — the specific fixture that
// regressed before the routeCountry fix: routeCountry used to fall back to (or
// otherwise leak) a single leg's country even though the trip spans Japan AND
// South Korea, fabricating an attribution like "Tokyo → Seoul, Japan".
const BOOKED_MULTI_COUNTRY = {
  ...BOOKED,
  booking_ref: 'BK-PV-11',
  idempotency_key: 'trip-pv-3',
  legs: [
    { leg_id: 'leg-0', city: 'Tokyo', country: 'Japan', checkin: '2026-10-01', checkout: '2026-10-04' },
    { leg_id: 'leg-1', city: 'Seoul', country: 'South Korea', checkin: '2026-10-04', checkout: '2026-10-07' },
  ],
  day_plans: [
    {
      leg_id: 'leg-0', city: 'Tokyo', country: 'Japan', checkin: '2026-10-01', checkout: '2026-10-04', num_days: 3,
      days: [{ day_index: 0, meals: { breakfast: { name: 'Kissaten' } },
        attractions: [{ name: 'Senso-ji', category: 'tourism=attraction' }] }],
    },
    {
      leg_id: 'leg-1', city: 'Seoul', country: 'South Korea', checkin: '2026-10-04', checkout: '2026-10-07', num_days: 3,
      days: [{ day_index: 0, meals: { breakfast: { name: 'Gukbap house' } },
        attractions: [{ name: 'Gyeongbokgung', category: 'tourism=attraction' }] }],
    },
  ],
};
// Mixed country PRESENCE (not a genuine 2-country trip): leg 0 has a real, known
// `country`; leg 1 omits `country` entirely (matches the real DayPlan/Leg contract,
// where `country` is optional and simply absent rather than an empty string — see
// DayPlan/Leg in api.ts). Distinct from BOOKED_MULTI_COUNTRY (2 known, DIFFERENT
// countries): this is 1 known + 1 unknown. Without the `plans.every((p) => p.country)`
// guard, routeCountry's Set would have size 1 (only the known country is truthy and
// added) and the old code would misattribute the whole route to that single leg's
// country even though the second leg's real country is simply not known.
const BOOKED_MIXED_COUNTRY_PRESENCE = {
  ...BOOKED,
  booking_ref: 'BK-PV-12',
  idempotency_key: 'trip-pv-4',
  legs: [
    { leg_id: 'leg-0', city: 'Tokyo', country: 'Japan', checkin: '2026-10-01', checkout: '2026-10-04' },
    { leg_id: 'leg-1', city: 'Osaka', checkin: '2026-10-04', checkout: '2026-10-07' },
  ],
  day_plans: [
    {
      leg_id: 'leg-0', city: 'Tokyo', country: 'Japan', checkin: '2026-10-01', checkout: '2026-10-04', num_days: 3,
      days: [{ day_index: 0, meals: { breakfast: { name: 'Kissaten' } },
        attractions: [{ name: 'Senso-ji', category: 'tourism=attraction' }] }],
    },
    {
      // No `country` field at all — an unknown-country leg, not merely an
      // empty-string one, matching how the real server contract represents it.
      leg_id: 'leg-1', city: 'Osaka', checkin: '2026-10-04', checkout: '2026-10-07', num_days: 3,
      days: [{ day_index: 0, meals: { breakfast: { name: 'Takoyaki stand' } },
        attractions: [{ name: 'Osaka Castle', category: 'tourism=attraction' }] }],
    },
  ],
};

// ── Leg-hero photo band (Preview's PlacePhotos variant="banner") ────────────
// Reuses BOOKED's existing marquee activity (Senso-ji — the leg's first
// activity-kind item in mealAnchoredTimeline order) for the basic "photo
// available" / "photo unavailable" tests below; only the /place_card mock
// differs between them, no new booking envelope needed for those two.

// A single leg with MULTIPLE activities spread across 2 days — the fixture for
// "one banner per leg, not one per activity": a template bug that mounted
// <PlacePhotos variant="banner"> once per activity (instead of once per leg,
// keyed off legMarqueeName()'s single first-match) would show up as >1 banner.
const BOOKED_MULTI_ACTIVITY_LEG = {
  ...BOOKED,
  booking_ref: 'BK-PV-15',
  idempotency_key: 'trip-pv-6',
  day_plans: [{
    leg_id: 'leg-0', city: 'Tokyo', country: 'Japan', checkin: '2026-10-01', checkout: '2026-10-04', num_days: 3,
    days: [
      { day_index: 0, meals: { breakfast: { name: 'Kissaten' } },
        attractions: [
          { name: 'Senso-ji', category: 'tourism=attraction' },
          { name: 'Tokyo Tower', category: 'tourism=attraction' },
        ] },
      { day_index: 1, meals: { lunch: { name: 'Ramen Counter' } },
        attractions: [{ name: 'Shibuya Crossing', category: 'tourism=attraction' }] },
    ],
  }],
};

// Two legs, each with its own marquee activity — the per-leg cardinality check:
// exactly one banner PER LEG (2 total for this fixture), not a single shared
// banner and not more than one per leg either.
const BOOKED_TWO_LEGS_WITH_ACTIVITIES = {
  ...BOOKED,
  booking_ref: 'BK-PV-16',
  idempotency_key: 'trip-pv-7',
  legs: [
    { leg_id: 'leg-0', city: 'Tokyo', country: 'Japan', checkin: '2026-10-01', checkout: '2026-10-04' },
    { leg_id: 'leg-1', city: 'Kyoto', country: 'Japan', checkin: '2026-10-04', checkout: '2026-10-07' },
  ],
  day_plans: [
    {
      leg_id: 'leg-0', city: 'Tokyo', country: 'Japan', checkin: '2026-10-01', checkout: '2026-10-04', num_days: 3,
      days: [{ day_index: 0, meals: { breakfast: { name: 'Kissaten' } },
        attractions: [{ name: 'Senso-ji', category: 'tourism=attraction' }] }],
    },
    {
      leg_id: 'leg-1', city: 'Kyoto', country: 'Japan', checkin: '2026-10-04', checkout: '2026-10-07', num_days: 3,
      days: [{ day_index: 0, meals: { breakfast: { name: 'Kissaten Kyoto' } },
        attractions: [{ name: 'Fushimi Inari', category: 'tourism=attraction' }] }],
    },
  ],
};

// Meal-only day — no activity-kind item anywhere in the leg, so legMarqueeName()
// returns null and NO <PlacePhotos variant="banner"> is mounted at all. Distinct
// from "mounted but the fetch came back empty/unavailable" (covered separately
// below) — this is the "nothing to show a photo of in the first place" path.
const BOOKED_NO_ACTIVITIES = {
  ...BOOKED,
  booking_ref: 'BK-PV-17',
  idempotency_key: 'trip-pv-8',
  day_plans: [{
    leg_id: 'leg-0', city: 'Tokyo', country: 'Japan', checkin: '2026-10-01', checkout: '2026-10-04', num_days: 3,
    days: [{ day_index: 0, meals: { breakfast: { name: 'Kissaten' } } }],
  }],
};

const PLACE_CARD_PHOTO_OK = {
  status: 'ok',
  place: {
    display_name: 'Senso-ji',
    photos: ['/place_photo?ref=hero-abc123'],
    rating: 4.6,
    user_rating_count: 12000,
    open_now: true,
  },
};
const PLACE_CARD_NO_PHOTOS = { status: 'ok', place: { photos: [], rating: 4.6, user_rating_count: 12000 } };
const PLACE_CARD_UNAVAILABLE = { status: 'unavailable' };

// On mobile widths the embedded desktop chat-input is display:none (replaced by
// the floating 🤖 bubble + slide-up sheet — see mobile-chat.spec.ts); planning
// must go through that sheet instead of getByTestId('chat-input').
async function mobilePlan(page: import('@playwright/test').Page, text: string): Promise<void> {
  await page.locator('.mob-bubble').click();
  const textarea = page.locator('.mob-sheet-box textarea');
  await textarea.fill(text);
  await page.locator('.mob-sheet-box .send').click();
}

async function seedGuestSession(page: import('@playwright/test').Page): Promise<void> {
  // Pre-seed a guest session so the SessionPicker is bypassed (slice 4 picker only
  // appears on a fresh page with no stored choice). Storage key = 'tg_session'.
  await page.addInitScript(() => {
    localStorage.setItem('tg_session', JSON.stringify({ user: null, guest: true, savedAt: Date.now() }));
  });
}

async function bookAndOpenSummary(
  page: import('@playwright/test').Page,
  bookedEnvelope: unknown,
  opts: { mobile?: boolean; placeCardRoute?: (route: import('@playwright/test').Route) => void } = {},
): Promise<void> {
  await seedGuestSession(page);
  await page.route('**/negotiate_text', (route) =>
    route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(PLAN_READY) }));
  await page.route('**/confirm', (route) =>
    route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(bookedEnvelope) }));
  await page.route('**/tile.openstreetmap.org/**', (route) => route.abort());
  // Leg-hero band (PlacePhotos variant="banner") fetches POST /place_card eagerly on
  // mount — undefined here (the default) means no route is registered, so the fetch
  // hits the network and fails/rejects hermetically, matching every pre-existing test
  // in this file that never mocked it (banner silently renders nothing; see the
  // dedicated 'leg-hero band' describe block below for tests that DO mock it).
  if (opts.placeCardRoute) {
    await page.route('**/place_card', opts.placeCardRoute);
  }

  await page.goto('/');
  if (opts.mobile) {
    await mobilePlan(page, '5 days in tokyo, culture, $3000');
  } else {
    await page.getByTestId('chat-input').fill('5 days in tokyo, culture, $3000');
    await page.getByRole('button', { name: 'Send' }).click();
  }
  await page.getByTestId('confirm-book').click();
  await expect(page.getByTestId('status-banner')).toContainText('Booked');

  await page.getByTestId('view-summary').click();
  await expect(page.getByTestId('preview')).toBeVisible();
}

test('booked → trip summary renders the LLM narrative → .ics downloads', async ({ page }) => {
  await bookAndOpenSummary(page, BOOKED);

  const preview = page.getByTestId('preview');
  await expect(preview).toContainText('BK-PV-9');
  await expect(page.getByTestId('narrative')).toContainText('gentle first day in Tokyo');   // real LLM text
  await expect(preview).toContainText('Day 1');
  await expect(preview).toContainText('Senso-ji');

  // save-to-phone: clicking the hero icon-button triggers an .ics download
  const [download] = await Promise.all([
    page.waitForEvent('download'),
    page.getByTestId('save-ics').click(),
  ]);
  expect(download.suggestedFilename()).toBe('travel-guild-BK-PV-9.ics');
});

test('no narrative (LLM-off path): narrative block is absent, nothing fabricated', async ({ page }) => {
  await bookAndOpenSummary(page, BOOKED_NO_NARRATIVE);

  const preview = page.getByTestId('preview');
  await expect(preview).toContainText('BK-PV-10');
  // The itinerary itself still renders fine without a narrative.
  await expect(preview).toContainText('Day 1');
  await expect(preview).toContainText('Senso-ji');
  // No narrative block, and no "✨ Your assistant's summary" label anywhere —
  // assistantSummary() must return null (not an empty-string placeholder) and the
  // component must not render a stand-in summary.
  await expect(page.getByTestId('narrative')).toHaveCount(0);
  await expect(preview.locator('.nlabel')).toHaveCount(0);
});

// ── Stale-narrative honesty caveat (itinerary_narrative.stale / .stale_reason,
// set by the backend after a structural /replan edit — server.py:2282-2288) ──
test('stale narrative: shows the honest stale caveat alongside the (still-rendered) overview', async ({ page }) => {
  await bookAndOpenSummary(page, BOOKED_STALE_NARRATIVE);

  await expect(page.getByTestId('narrative')).toContainText('gentle first day in Tokyo');   // overview still shown
  const caveat = page.getByTestId('narrative-stale');
  await expect(caveat).toBeVisible();
  await expect(caveat).toContainText('generated before your latest edit');
  await expect(caveat).toContainText('removed, added, or reordered');
});

test('stale: false narrative: renders the overview with NO caveat (no regression)', async ({ page }) => {
  await bookAndOpenSummary(page, BOOKED_NOT_STALE_NARRATIVE);

  await expect(page.getByTestId('narrative')).toContainText('gentle first day in Tokyo');
  await expect(page.getByTestId('narrative-stale')).toHaveCount(0);
});

test('no narrative + no stale flag: no caveat and no narrative block (nothing fabricated)', async ({ page }) => {
  await bookAndOpenSummary(page, BOOKED_NO_NARRATIVE);

  await expect(page.getByTestId('narrative')).toHaveCount(0);
  await expect(page.getByTestId('narrative-stale')).toHaveCount(0);
});

test('close (back) button returns from the trip summary to the itinerary view', async ({ page }) => {
  await bookAndOpenSummary(page, BOOKED);

  await page.getByTestId('preview-back').click();
  await expect(page.getByTestId('preview')).toHaveCount(0);
  await expect(page.getByTestId('itinerary')).toBeVisible();
});

// ── Hero route country attribution ──────────────────────────────────────────
test('single-country trip: hero route is suffixed with that country', async ({ page }) => {
  await bookAndOpenSummary(page, BOOKED);

  const route = page.locator('.hero-route');
  await expect(route).toContainText('Tokyo');
  await expect(route).toContainText('Japan');
});

test('multi-country trip: hero route omits any country suffix (no single-leg fabrication)', async ({ page }) => {
  await bookAndOpenSummary(page, BOOKED_MULTI_COUNTRY);

  const route = page.locator('.hero-route');
  // Both leg cities are present (the route itself is still shown)...
  await expect(route).toContainText('Tokyo');
  await expect(route).toContainText('Seoul');
  // ...but no country is appended anywhere in the hero route — specifically,
  // neither leg's country may be tacked on (that would fabricate an
  // attribution like "Tokyo → Seoul, Japan" or "..., South Korea").
  const text = (await route.textContent()) ?? '';
  expect(text).not.toContain('Japan');
  expect(text).not.toContain('South Korea');
});

test('mixed country presence (one leg known, one leg unknown): hero route omits any country suffix', async ({ page }) => {
  await bookAndOpenSummary(page, BOOKED_MIXED_COUNTRY_PRESENCE);

  const route = page.locator('.hero-route');
  // Both leg cities are still shown...
  await expect(route).toContainText('Tokyo');
  await expect(route).toContainText('Osaka');
  // ...but the single known country ("Japan") must NOT be tacked on just because
  // it's the only one present — the second leg's country is unknown, not "also
  // Japan", so no country may be shown at all (the plans.every((p) => p.country)
  // guard in routeCountry).
  const text = (await route.textContent()) ?? '';
  expect(text).not.toContain('Japan');
});

// ── (a) Desktop: budget card renders as a genuinely distinct boxed element ──────
test.describe('desktop viewport (1280×800)', () => {
  test.use({ viewport: { width: 1280, height: 800 } });

  test('budget card is a distinct accent-bordered box in the two-column layout', async ({ page }) => {
    await bookAndOpenSummary(page, BOOKED);

    const preview = page.getByTestId('preview');
    // Two-column grid (main itinerary + aside budget card) — NOT the old
    // single-column stacked layout. .layout.single only applies when there are
    // no budget rows; this fixture has both fee rows so it must be two-column.
    const layout = preview.locator('.layout');
    await expect(layout).toBeVisible();
    await expect(layout).not.toHaveClass(/single/);
    const cols = await layout.evaluate((el) => getComputedStyle(el).gridTemplateColumns);
    expect(cols.trim().split(/\s+/).length).toBe(2);   // "1fr" + "300px", i.e. genuinely two columns

    // The budget card itself: a visually distinct box (background/border/accent
    // left-border), not just a plain in-flow <article> the way the old design was.
    const card = preview.locator('.budget-card');
    await expect(card).toBeVisible();
    await expect(card).toContainText('Budget');
    await expect(card).toContainText('Bookable package');
    await expect(card).toContainText('Your budget');
    const style = await card.evaluate((el) => {
      const cs = getComputedStyle(el);
      return { borderLeftWidth: cs.borderLeftWidth, borderRadius: cs.borderRadius, background: cs.backgroundColor };
    });
    expect(parseFloat(style.borderLeftWidth)).toBeGreaterThanOrEqual(4);   // accent left-border (not a bare table row)
    expect(parseFloat(style.borderRadius)).toBeGreaterThan(0);            // rounded card, not flush text

    // The card carries its own "Save to calendar" affordance and it works too.
    const [download] = await Promise.all([
      page.waitForEvent('download'),
      page.getByTestId('save-ics-card').click(),
    ]);
    expect(download.suggestedFilename()).toBe('travel-guild-BK-PV-9.ics');
  });

  // Desktop has no persistent budget bar, so the hero icon-button is the sole
  // Save affordance here — it must stay visible (only hidden on mobile, where
  // .bb-save in the budget-bar takes over instead; see the mobile counterpart
  // below for the regression this guards).
  test('hero save icon-button is visible (desktop has no persistent budget bar)', async ({ page }) => {
    await bookAndOpenSummary(page, BOOKED);
    await expect(page.getByTestId('save-ics')).toBeVisible();
  });
});

// ── (b) Mobile: ChatPane's floating bubble must NOT be visible while Preview is
// open — this is the specific regression this track exists to prevent: Preview's
// sticky bottom bar and ChatPane's floating trigger both want the mobile bottom
// edge, and they must never both be shown at once. ──────────────────────────────
test.describe('mobile viewport (390×844 — iPhone 14)', () => {
  test.use({ viewport: { width: 390, height: 844 } });

  test('assistant bubble is visible with a plan but hidden once the trip summary opens', async ({ page }) => {
    await seedGuestSession(page);
    await page.route('**/negotiate_text', (route) =>
      route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(PLAN_READY) }));
    await page.route('**/confirm', (route) =>
      route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(BOOKED) }));
    await page.route('**/tile.openstreetmap.org/**', (route) => route.abort());

    await page.goto('/');
    await mobilePlan(page, '5 days in tokyo, culture, $3000');
    await page.getByTestId('confirm-book').click();
    await expect(page.getByTestId('status-banner')).toContainText('Booked');

    // Baseline (regression guard): with a plan/booking active and Preview NOT
    // open, the floating assistant bubble IS visible on mobile.
    const bubble = page.getByTestId('assistant-trigger');
    await expect(bubble).toBeVisible();

    // Open the trip summary — the bubble (and its flyout) must disappear.
    await page.getByTestId('view-summary').click();
    await expect(page.getByTestId('preview')).toBeVisible();

    await expect(bubble).toBeHidden();
    await expect(page.getByTestId('assistant-flyout')).toBeHidden();

    // Preview's own fixed-bottom budget bar (Draft 3: .budget-bar) is what
    // legitimately owns the mobile bottom edge instead — confirms this isn't
    // just "nothing renders".
    await expect(page.locator('.budget-bar')).toBeVisible();
    // Money-itemization fix: the mobile bar labels this "Charged" (not "Total") whenever
    // a real wallet.debit_cents is present — BOOKED sets one, so this is a real charge.
    await expect(page.locator('.budget-bar')).toContainText('Charged');

    // Closing the trip summary restores the bubble (hideMobileBubble is reactive,
    // not a one-way latch).
    await page.getByTestId('preview-back').click();
    await expect(page.getByTestId('preview')).toHaveCount(0);
    await expect(bubble).toBeVisible();
  });

  test('save-to-phone still works from the mobile budget bar', async ({ page }) => {
    await bookAndOpenSummary(page, BOOKED, { mobile: true });

    const [download] = await Promise.all([
      page.waitForEvent('download'),
      page.getByTestId('save-ics-sticky').click(),
    ]);
    expect(download.suggestedFilename()).toBe('travel-guild-BK-PV-9.ics');
  });

  // ── Draft 3: collapsed budget bar + slide-up breakdown sheet ────────────────
  // The sheet is toggled via a CSS transform (translateY), not display:none, so
  // Playwright's toBeVisible()/toBeHidden() can't tell open from closed (an
  // off-screen-but-`display:flex` element still reports visible=true). Assert
  // the real on-screen position via boundingBox() instead — that's what an
  // actual user would see.
  // The sheet is `position: fixed; bottom: 58px` (docked directly above the bar)
  // with `translateY(100%)` for the collapsed state. Because the transform
  // shifts by the sheet's OWN height, its collapsed top edge lands exactly at
  // the bar's top edge (not further down / fully past the viewport bottom) —
  // so "on screen" must be judged relative to the bar's top, not the raw
  // viewport height: only when the sheet's top is ABOVE the bar's top does any
  // of it actually peek out from behind the opaque bar for the user to see.
  async function sheetOnScreen(page: import('@playwright/test').Page): Promise<boolean> {
    const sheetBox = await page.locator('.budget-sheet').boundingBox();
    const barBox = await page.locator('.budget-bar').boundingBox();
    return !!sheetBox && !!barBox && sheetBox.y < barBox.y - 1;   // -1px tolerance for rounding
  }

  test('budget bar starts collapsed: total is visible, full breakdown is not on-screen', async ({ page }) => {
    await bookAndOpenSummary(page, BOOKED, { mobile: true });

    const bar = page.locator('.budget-bar');
    await expect(bar).toBeVisible();
    await expect(bar).toContainText('Charged'); // BOOKED carries wallet.debit_cents — a real charge

    const toggle = page.locator('.bb-toggle');
    await expect(toggle).toHaveAttribute('aria-expanded', 'false');

    // The sheet exists (fixed in the DOM, per the mob-sheet pattern) but is
    // parked off-screen below the viewport — not something the user can see
    // or scroll to without tapping the bar first.
    await expect.poll(() => sheetOnScreen(page)).toBe(false);
    await expect(page.locator('.budget-sheet')).toHaveAttribute('aria-hidden', 'true');
  });

  test('tapping the bar reveals the full breakdown sheet', async ({ page }) => {
    await bookAndOpenSummary(page, BOOKED, { mobile: true });

    await page.locator('.bb-toggle').click();

    await expect(page.locator('.bb-toggle')).toHaveAttribute('aria-expanded', 'true');
    await expect(page.locator('.budget-sheet')).toHaveAttribute('aria-hidden', 'false');
    await expect.poll(() => sheetOnScreen(page)).toBe(true);

    // The full line-item breakdown is genuinely there now, not just a wrapper.
    const sheet = page.locator('.budget-sheet');
    await expect(sheet).toContainText('Budget breakdown');
    await expect(sheet).toContainText('Bookable package');
    await expect(sheet).toContainText('Your budget');
  });

  test('tapping the bar again collapses the sheet back', async ({ page }) => {
    await bookAndOpenSummary(page, BOOKED, { mobile: true });

    const toggle = page.locator('.bb-toggle');
    await toggle.click();
    await expect.poll(() => sheetOnScreen(page)).toBe(true);

    await toggle.click();

    await expect(toggle).toHaveAttribute('aria-expanded', 'false');
    await expect(page.locator('.budget-sheet')).toHaveAttribute('aria-hidden', 'true');
    await expect.poll(() => sheetOnScreen(page)).toBe(false);
  });

  test("the sheet's own close affordance collapses it back", async ({ page }) => {
    await bookAndOpenSummary(page, BOOKED, { mobile: true });

    await page.locator('.bb-toggle').click();
    await expect.poll(() => sheetOnScreen(page)).toBe(true);

    await page.getByRole('button', { name: 'Close' }).click();

    await expect(page.locator('.bb-toggle')).toHaveAttribute('aria-expanded', 'false');
    await expect.poll(() => sheetOnScreen(page)).toBe(false);
  });

  test('Save works from the collapsed bar and continues to work once expanded', async ({ page }) => {
    await bookAndOpenSummary(page, BOOKED, { mobile: true });

    // Collapsed state (the default).
    await expect(page.locator('.bb-toggle')).toHaveAttribute('aria-expanded', 'false');
    const [download1] = await Promise.all([
      page.waitForEvent('download'),
      page.getByTestId('save-ics-sticky').click(),
    ]);
    expect(download1.suggestedFilename()).toBe('travel-guild-BK-PV-9.ics');

    // Expanded state — Save lives only on the bar (not duplicated into the
    // sheet), and must keep working once the sheet is open.
    await page.locator('.bb-toggle').click();
    await expect(page.locator('.bb-toggle')).toHaveAttribute('aria-expanded', 'true');
    const [download2] = await Promise.all([
      page.waitForEvent('download'),
      page.getByTestId('save-ics-sticky').click(),
    ]);
    expect(download2.suggestedFilename()).toBe('travel-guild-BK-PV-9.ics');
  });

  // Regression lock for the duplicate-Save-icon bugfix: on mobile the hero's
  // own save icon-button (.icon-btn.save, data-testid=save-ics) must be hidden —
  // Save lives only in the persistent budget-bar's .bb-save (save-ics-sticky)
  // there, so the two affordances are never shown stacked on top of each other.
  test('hero save icon-button is hidden on mobile; only the sticky bar Save button shows', async ({ page }) => {
    await bookAndOpenSummary(page, BOOKED, { mobile: true });

    await expect(page.getByTestId('save-ics')).toBeHidden();
    await expect(page.getByTestId('save-ics-sticky')).toBeVisible();
  });
});

// ── Leg-hero photo band (image-placement #1: PlacePhotos variant="banner") ──
// Preview.svelte mounts exactly one <PlacePhotos variant="banner"> per leg,
// between the leg's <h2> and its first .day, keyed off legMarqueeName() (the
// leg's first activity-kind timeline item, never a meal). Banner mode fetches
// eagerly on mount and fails COMPLETELY silent — no placeholder, no broken-image
// icon, no "no photos" text — since an always-on banner showing an empty state
// would read as a visible defect rather than an honest absence.
test.describe('leg-hero photo band', () => {
  test('renders when a photo is available (mocked /place_card)', async ({ page }) => {
    await page.route('**/place_photo**', (route) =>
      route.fulfill({ status: 200, contentType: 'image/jpeg', body: Buffer.alloc(100) }));

    await bookAndOpenSummary(page, BOOKED, {
      placeCardRoute: (route) =>
        route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(PLACE_CARD_PHOTO_OK) }),
    });

    const preview = page.getByTestId('preview');
    const banner = preview.getByTestId('leg-hero-banner');
    await expect(banner).toBeVisible({ timeout: 5000 });
    // Real image, not a placeholder/broken-image glyph: alt = the marquee activity
    // name, src resolves through the server-proxied /place_photo path.
    await expect(banner).toHaveAttribute('alt', 'Senso-ji');
    const src = await banner.getAttribute('src');
    expect(src).toContain('/place_photo');

    // Placed between the leg heading and the first day, itinerary column only —
    // not inside the budget card.
    await expect(preview.locator('.leg').first().locator('h2 + img[data-testid="leg-hero-banner"]')).toHaveCount(1);
  });

  test('absent (genuinely no element, not a broken-image icon) when /place_card is unavailable', async ({ page }) => {
    await bookAndOpenSummary(page, BOOKED, {
      placeCardRoute: (route) =>
        route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(PLACE_CARD_UNAVAILABLE) }),
    });

    const preview = page.getByTestId('preview');
    // Zero elements in the DOM — not a hidden/broken <img>, not a "no photo"
    // placeholder message. The itinerary content around it still renders fine.
    await expect(preview.getByTestId('leg-hero-banner')).toHaveCount(0);
    await expect(preview.locator('.leg img')).toHaveCount(0);
    await expect(preview).toContainText('Senso-ji');   // the timeline item itself is unaffected
  });

  test('absent when /place_card returns ok with an empty photos array', async ({ page }) => {
    await bookAndOpenSummary(page, BOOKED, {
      placeCardRoute: (route) =>
        route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(PLACE_CARD_NO_PHOTOS) }),
    });

    const preview = page.getByTestId('preview');
    await expect(preview.getByTestId('leg-hero-banner')).toHaveCount(0);
    await expect(preview.locator('.leg img')).toHaveCount(0);
  });

  test('absent when the leg has no activity-kind item at all (meal-only day)', async ({ page }) => {
    // NOTE: this test does NOT count /place_card calls the way the earlier
    // tests in this describe block do — Itinerary.svelte's separate, pre-
    // existing "Part B Item 3" per-item thumbnail feature also eagerly calls
    // /place_card (for the pre-booking itinerary view's own visible items,
    // e.g. PLAN_READY's Senso-ji attraction), which would pollute a raw call
    // count and isn't what this test is about. The DOM assertion below (no
    // banner element for THIS leg, once booked) is the real signal.
    await bookAndOpenSummary(page, BOOKED_NO_ACTIVITIES, {
      placeCardRoute: (route) =>
        route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(PLACE_CARD_PHOTO_OK) }),
    });

    const preview = page.getByTestId('preview');
    // legMarqueeName() returned null, so <PlacePhotos variant="banner"> was
    // never mounted for this leg at all — genuinely no element in the DOM.
    await expect(preview.getByTestId('leg-hero-banner')).toHaveCount(0);
    await expect(preview.locator('.leg img')).toHaveCount(0);
  });

  test('exactly one banner per leg, even with multiple activities across multiple days', async ({ page }) => {
    await page.route('**/place_photo**', (route) =>
      route.fulfill({ status: 200, contentType: 'image/jpeg', body: Buffer.alloc(100) }));

    await bookAndOpenSummary(page, BOOKED_MULTI_ACTIVITY_LEG, {
      placeCardRoute: (route) =>
        route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(PLACE_CARD_PHOTO_OK) }),
    });

    const preview = page.getByTestId('preview');
    // 1 leg, 3 activities (Senso-ji, Tokyo Tower, Shibuya Crossing) across 2 days
    // → still exactly 1 banner, not 3.
    await expect(preview.locator('.leg')).toHaveCount(1);
    await expect(preview.getByTestId('leg-hero-banner')).toHaveCount(1);
  });

  test('exactly one banner per leg across a multi-leg trip (2 legs → 2 banners)', async ({ page }) => {
    await page.route('**/place_photo**', (route) =>
      route.fulfill({ status: 200, contentType: 'image/jpeg', body: Buffer.alloc(100) }));

    await bookAndOpenSummary(page, BOOKED_TWO_LEGS_WITH_ACTIVITIES, {
      placeCardRoute: (route) =>
        route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(PLACE_CARD_PHOTO_OK) }),
    });

    const preview = page.getByTestId('preview');
    await expect(preview.locator('.leg')).toHaveCount(2);
    await expect(preview.getByTestId('leg-hero-banner')).toHaveCount(2);
    // Each leg article owns exactly one banner (not e.g. 2 on the first leg, 0 on
    // the second).
    const legs = preview.locator('.leg');
    for (let i = 0; i < 2; i++) {
      await expect(legs.nth(i).getByTestId('leg-hero-banner')).toHaveCount(1);
    }
  });
});

// ── Chat-bubble stale caveat (App.svelte plan()): if the very first plan_ready/
// success response already carries a stale-flagged narrative, the chat feed must
// show the server's stale_reason as an honest 'note' bubble right after the
// narrative bubble — reusing the same visual pattern as stateNote()/kept_previous
// (data-testid="chat-msgs" .b.note), never inventing its own wording. ────────────
test.describe('chat-bubble stale caveat', () => {
  test('narrative bubble followed by a note bubble with the honest stale_reason', async ({ page }) => {
    await seedGuestSession(page);
    await page.route('**/negotiate_text', (route) =>
      route.fulfill({
        status: 200, contentType: 'application/json',
        body: JSON.stringify({
          ...PLAN_READY,
          itinerary_narrative: {
            overview: 'A gentle first day in Tokyo.',
            stale: true,
            stale_reason: "This AI-written summary was generated before your latest edit and may still describe stops that were since removed, added, or reordered.",
          },
        }),
      }));
    await page.route('**/tile.openstreetmap.org/**', (route) => route.abort());

    await page.goto('/');
    await page.getByTestId('chat-input').fill('5 days in tokyo, culture, $3000');
    await page.getByRole('button', { name: 'Send' }).click();
    await expect(page.getByTestId('status-banner')).toContainText('Plan ready');

    const msgs = page.getByTestId('chat-msgs');
    await expect(msgs).toContainText('A gentle first day in Tokyo.');
    const note = msgs.locator('.b.note').last();
    await expect(note).toContainText('generated before your latest edit');
  });

  test('narrative bubble with NO stale flag: no extra note bubble (no regression)', async ({ page }) => {
    await seedGuestSession(page);
    await page.route('**/negotiate_text', (route) =>
      route.fulfill({
        status: 200, contentType: 'application/json',
        body: JSON.stringify({
          ...PLAN_READY,
          itinerary_narrative: { overview: 'A gentle first day in Tokyo.' },
        }),
      }));
    await page.route('**/tile.openstreetmap.org/**', (route) => route.abort());

    await page.goto('/');
    await page.getByTestId('chat-input').fill('5 days in tokyo, culture, $3000');
    await page.getByRole('button', { name: 'Send' }).click();
    await expect(page.getByTestId('status-banner')).toContainText('Plan ready');

    const msgs = page.getByTestId('chat-msgs');
    await expect(msgs).toContainText('A gentle first day in Tokyo.');
    await expect(msgs.locator('.b.note')).toHaveCount(0);
  });
});

// ── (d) narrate:true routing — the LLM-generated itinerary_narrative (this file's
// "✨ assistant's summary" / trip-summary narrative block, see the very first tests
// above) is only worth the ~14-30s it costs on the INITIAL plan() call, where the
// narrative is actually surfaced. A refineCurrentPlan follow-up ("make it cheaper",
// etc.) deliberately must NOT request it — see App.svelte's plan()/refineCurrentPlan
// and api.ts's NegotiateBody.narrate / RefineBody (RefineBody has no narrate field
// at all — it is structurally impossible for a refine call to send one). ────────────
test.describe('narrate:true request-body routing (initial plan only, never on refine)', () => {
  test("the initial plan() call's /negotiate_text request body includes narrate:true", async ({ page }) => {
    await seedGuestSession(page);
    let capturedBody: Record<string, unknown> | null = null;
    await page.route('**/negotiate_text', (route) => {
      capturedBody = JSON.parse(route.request().postData() || '{}');
      route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(PLAN_READY) });
    });
    await page.route('**/tile.openstreetmap.org/**', (route) => route.abort());

    await page.goto('/');
    await page.getByTestId('chat-input').fill('5 days in tokyo, culture, $3000');
    await page.getByRole('button', { name: 'Send' }).click();
    await expect(page.getByTestId('status-banner')).toContainText('Plan ready');

    expect(capturedBody).not.toBeNull();
    expect((capturedBody as Record<string, unknown>).narrate).toBe(true);
  });

  test('a refineCurrentPlan follow-up (after plan_ready) does NOT include narrate in its /refine request body', async ({ page }) => {
    await seedGuestSession(page);
    await page.route('**/negotiate_text', (route) =>
      route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(PLAN_READY) }));
    await page.route('**/tile.openstreetmap.org/**', (route) => route.abort());

    let refineBody: Record<string, unknown> | null = null;
    await page.route('**/refine', (route) => {
      refineBody = JSON.parse(route.request().postData() || '{}');
      route.fulfill({
        status: 200, contentType: 'application/json',
        body: JSON.stringify({
          outcome: 'plan_ready', idempotency_key: 'trip-pv-refine',
          plan: PLAN_READY, assistant_reply: 'Updated your plan.',
        }),
      });
    });

    await page.goto('/');
    await page.getByTestId('chat-input').fill('5 days in tokyo, culture, $3000');
    await page.getByRole('button', { name: 'Send' }).click();
    await expect(page.getByTestId('status-banner')).toContainText('Plan ready');

    // hasPlan is now true — ChatPane switched to floating mode; open the flyout
    // before typing the follow-up (same house pattern as guide-cards.spec.ts's D-04a).
    await page.getByTestId('assistant-trigger').click();
    await expect(page.getByTestId('assistant-flyout')).toBeVisible({ timeout: 3000 });
    await page.getByTestId('chat-input').fill('make it cheaper');
    await page.getByTestId('chat-input').press('Enter');

    await expect.poll(() => refineBody, { timeout: 6000 }).not.toBeNull();
    expect('narrate' in (refineBody as Record<string, unknown>)).toBe(false);
    expect((refineBody as Record<string, unknown>).narrate).toBeUndefined();
  });
});
