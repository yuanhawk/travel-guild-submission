import { test, expect } from '@playwright/test';

// itinerary-scale.spec.ts — itinerary-scale redesign (mockups/itinerary-scale-draft1..5.html)
// + Item 3 always-visible thumbnails (mockups/image-placement-draft2.html), both in
// Itinerary.svelte. Extends e2e/edit-lane.spec.ts (which already covers the PRE-EXISTING
// activity-row drag-drop + per-day suggestion-chip flows) rather than duplicating it — this
// file is scoped to the NEW mechanics: leg accordion, per-leg day-tabs, day-tab drag-reorder
// (desktop DnD + mobile long-press), tap/swipe navigation, and the lazy per-item thumbnail
// fetch that's keyed to the active day-tab.
//
// Highest-risk mechanics covered (see task):
//   (a) day-tabs-vs-flat-list threshold, AT the per-leg boundary (6 days exactly vs 5)
//   (b) within-leg day-tab drag-reorder succeeds and updates day CONTENT (not day_index) correctly
//   (c) day-tab drag ACROSS a leg boundary is rejected — no network call, itinerary unchanged
//   (d) boundary-day lock (first/last day of a leg is never draggable) — REVERT-AND-CONFIRMED
//   (e) tap (released before the 500ms long-press threshold) navigates, not drags;
//       swipe nav moves one adjacent day; a direct tab click jumps across many at once
//   (f) thumbnails lazy-fetch ONLY for the active day-tab; revisiting a day is a cache hit
//   (g) THE COUPLING CASE — after a day-tab reorder, each day shows ITS OWN (not stale)
//       photo — REVERT-AND-CONFIRMED
//
// Playwright has no built-in helper for either native HTML5 DnD with a custom JSON payload
// or synthetic touch gestures, so both are dispatched via page.evaluate() with real
// DataTransfer / Touch / TouchEvent objects — see dragDayTab() / dispatchTouchOnSelector()
// below. This mirrors how the component itself reads dragKind/dayDragSource module state
// rather than dataTransfer.getData() for day-tab drags, so a bare DataTransfer stand-in
// (no real OS-level drag) exercises the exact same code path a real drag would.

function attraction(name: string) {
  return { name, category: 'tourism=attraction', lat: 35.0, lon: 135.0, fee: null, weather_exposure: 'outdoor' };
}

function dayWith(day_index: number, name: string) {
  return { day_index, bad_weather: false, attractions: [attraction(name)], meals: {} };
}

// 6 days = exactly the DAY_TABS_THRESHOLD (draft3 Decision C). Boundary days are index 0
// (arrival) and index 5 (checkout) — indices 1-4 are the only legally-swappable days.
const KYOTO_NAMES = [
  'Nishiki Market', 'Fushimi Inari Shrine', 'Arashiyama Bamboo Grove',
  'Kinkaku-ji Temple', 'Gion District', 'Nijo Castle',
];
const KYOTO_DAYS_6 = KYOTO_NAMES.map((n, i) => dayWith(i, n));

const OSAKA_NAMES_6 = [
  'Osaka Castle', 'Dotonbori', 'Universal Studios Japan',
  'Umeda Sky Building', 'Shitennoji Temple', 'Kaiyukan Aquarium',
];
const OSAKA_DAYS_6 = OSAKA_NAMES_6.map((n, i) => dayWith(i, n));
const OSAKA_DAYS_5 = OSAKA_NAMES_6.slice(0, 5).map((n, i) => dayWith(i, n));

/** Swap two days' CONTENT (attractions/meals/bad_weather) but keep day_index — matches
 *  the swap_days contract in api.ts: "swaps two days' FULL content ... never changes day
 *  count/reindexes." Day N's tab always still says "Day N"; only what's inside changes. */
function swapDayContent(days: ReturnType<typeof dayWith>[], a: number, b: number) {
  const out = days.map((d) => ({ ...d }));
  const contentA = { attractions: out[a].attractions, meals: out[a].meals, bad_weather: out[a].bad_weather };
  const contentB = { attractions: out[b].attractions, meals: out[b].meals, bad_weather: out[b].bad_weather };
  out[a] = { ...out[a], ...contentB };
  out[b] = { ...out[b], ...contentA };
  return out;
}

function plan(idKey: string, dayPlans: unknown[], legs: unknown[]) {
  return {
    outcome: 'plan_ready', payment_status: 'held', booking_ref: null, idempotency_key: idKey,
    package_total_with_fees_cents: 180000, total_budget_cents: 900000,
    wallet: { balance_cents: 1200000, held_cents: 180000, debited: false },
    legs, day_plans: dayPlans,
    risk_signals: { per_leg: [] },
  };
}

function kyotoLeg(idKey = 'trip-scale-1') {
  return plan(
    idKey,
    [{ leg_id: 'leg-0', city: 'Kyoto', country: 'Japan', num_days: 6, days: KYOTO_DAYS_6 }],
    [{ leg_id: 'leg-0', city: 'Kyoto', country: 'Japan', checkin: '2026-11-01', checkout: '2026-11-07' }],
  );
}

async function setupPlan(page, planEnvelope: unknown, prompt: string) {
  await page.addInitScript(() => {
    localStorage.setItem('tg_session', JSON.stringify({ user: null, guest: true, savedAt: Date.now() }));
  });
  await page.route('**/negotiate_text', (route) =>
    route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(planEnvelope) }));
  await page.route('**/tile.openstreetmap.org/**', (route) => route.abort());
  await page.goto('/');
  await page.getByTestId('chat-input').fill(prompt);
  await page.getByRole('button', { name: 'Send' }).click();
  await expect(page.getByTestId('itinerary')).toBeVisible({ timeout: 8000 });
}

/** Dispatch a real dragstart -> dragover -> drop -> dragend sequence between two
 *  data-testid'd elements, sharing one DataTransfer across all four events (same as a
 *  real drag). Itinerary.svelte's day-tab drag reads dragKind/dayDragSource module state,
 *  not dataTransfer.getData(), so this exercises the exact same branches a real OS-level
 *  drag would. */
async function dragDayTab(page, srcTestId: string, dstTestId: string) {
  await page.evaluate(({ srcTestId, dstTestId }) => {
    const src = document.querySelector(`[data-testid="${srcTestId}"]`) as HTMLElement | null;
    const dst = document.querySelector(`[data-testid="${dstTestId}"]`) as HTMLElement | null;
    if (!src || !dst) throw new Error(`dragDayTab: missing element ${srcTestId} or ${dstTestId}`);
    const dt = new DataTransfer();
    src.dispatchEvent(new DragEvent('dragstart', { bubbles: true, cancelable: true, dataTransfer: dt }));
    dst.dispatchEvent(new DragEvent('dragover', { bubbles: true, cancelable: true, dataTransfer: dt }));
    dst.dispatchEvent(new DragEvent('drop', { bubbles: true, cancelable: true, dataTransfer: dt }));
    src.dispatchEvent(new DragEvent('dragend', { bubbles: true, cancelable: true, dataTransfer: dt }));
  }, { srcTestId, dstTestId });
}

/** Dispatch a single-finger TouchEvent of `type` at (clientX, clientY) on the first element
 *  matching `selector`. touchend carries an EMPTY `touches` list (finger lifted) and the
 *  touch in `changedTouches`, matching the real TouchEvent contract the component reads
 *  (onDayContentTouchEnd uses e.changedTouches[0]). */
async function dispatchTouchOnSelector(page, selector: string, type: string, clientX: number, clientY: number) {
  await page.evaluate(({ selector, type, clientX, clientY }) => {
    const el = document.querySelector(selector) as HTMLElement | null;
    if (!el) throw new Error(`dispatchTouchOnSelector: missing element ${selector}`);
    const touch = new Touch({ identifier: 1, target: el, clientX, clientY });
    const ev = new TouchEvent(type, {
      bubbles: true, cancelable: true,
      touches: type === 'touchend' ? [] : [touch],
      changedTouches: [touch],
    });
    el.dispatchEvent(ev);
  }, { selector, type, clientX, clientY });
}

// ═══════════════════════════════════════════════════════════════════════════
// (a) day-tabs vs flat-list threshold — AT the per-leg boundary (draft3 Decision C:
//     "≥6 days in that leg" — so 6 must show tabs and 5 must stay flat, per-leg).
// ═══════════════════════════════════════════════════════════════════════════

test('day-tabs vs flat-list: exactly 6 days shows tabs, 5 days (same trip) stays flat', async ({ page }) => {
  const PLAN_THRESHOLD = plan(
    'trip-scale-thresh',
    [
      { leg_id: 'leg-0', city: 'Kyoto', country: 'Japan', num_days: 6, days: KYOTO_DAYS_6 },
      { leg_id: 'leg-1', city: 'Osaka', country: 'Japan', num_days: 5, days: OSAKA_DAYS_5 },
    ],
    [
      { leg_id: 'leg-0', city: 'Kyoto', country: 'Japan', checkin: '2026-11-01', checkout: '2026-11-07' },
      { leg_id: 'leg-1', city: 'Osaka', country: 'Japan', checkin: '2026-11-07', checkout: '2026-11-12' },
    ],
  );
  await setupPlan(page, PLAN_THRESHOLD, '6 days kyoto then 5 days osaka');

  // 11 total trip days >= 8 -> legCollapseDefault is true, so only leg 0 (li===0) starts
  // open. Expand leg 1 explicitly before asserting its (flat) day layout.
  await page.getByTestId('leg-toggle-1').click();
  await expect(page.getByTestId('day-panel-1-0')).toBeVisible();

  // Leg 0 (6 days, AT the threshold): day-tabs render; only the active (first) day panel
  // is visible, the rest are mounted-but-hidden (draft2 tech-note: never unmounted).
  await expect(page.getByTestId('day-tabs-0')).toBeVisible();
  await expect(page.getByTestId('day-panel-0-0')).toBeVisible();
  for (let di = 1; di < 6; di++) {
    await expect(page.getByTestId(`day-panel-0-${di}`)).toBeHidden();
  }

  // Leg 1 (5 days, ONE below the threshold): no day-tabs at all, every day panel visible
  // simultaneously — identical to the pre-redesign flat markup.
  await expect(page.getByTestId('day-tabs-1')).toHaveCount(0);
  for (let di = 0; di < 5; di++) {
    await expect(page.getByTestId(`day-panel-1-${di}`)).toBeVisible();
  }
});

// ═══════════════════════════════════════════════════════════════════════════
// (b) within-leg day-tab drag-reorder succeeds and updates day CONTENT correctly
// ═══════════════════════════════════════════════════════════════════════════

test('day-tab drag-reorder within a leg: swaps day content (not day_index) and re-renders correctly', async ({ page }) => {
  let replanBody: { ops: unknown[] } | null = null;
  await page.route('**/replan', (route) => {
    replanBody = JSON.parse(route.request().postData() || '{}');
    const swappedPlan = kyotoLeg('trip-scale-1');
    (swappedPlan.day_plans[0] as { days: unknown }).days = swapDayContent(KYOTO_DAYS_6, 1, 3);
    route.fulfill({
      status: 200, contentType: 'application/json',
      body: JSON.stringify({
        outcome: 'plan_ready', idempotency_key: 'trip-scale-1',
        applied: ['swap_days:leg0.day1.day3'], rejected: [], notes: [],
        plan: swappedPlan,
      }),
    });
  });

  await setupPlan(page, kyotoLeg(), '6 days in kyoto');
  await expect(page.getByTestId('day-tab-0-1')).toBeVisible();

  await dragDayTab(page, 'day-tab-0-1', 'day-tab-0-3');

  await expect(page.getByTestId('replan-flash')).toContainText('Updated', { timeout: 5000 });
  expect(replanBody).not.toBeNull();
  expect((replanBody!.ops[0] as Record<string, unknown>)).toEqual({
    op: 'swap_days', leg_index: 0, day_a: 1, day_b: 3,
  });

  // Day 1's TAB position now shows what was originally day 3's content, and vice versa —
  // "Day 2"/"Day 4" labels are unchanged (day_index didn't move), only what's inside did.
  await page.getByTestId('day-tab-0-1').click();
  await expect(page.getByTestId('day-panel-0-1')).toContainText('Kinkaku-ji Temple');
  await expect(page.getByTestId('day-panel-0-1')).not.toContainText('Fushimi Inari Shrine');

  await page.getByTestId('day-tab-0-3').click();
  await expect(page.getByTestId('day-panel-0-3')).toContainText('Fushimi Inari Shrine');
  await expect(page.getByTestId('day-panel-0-3')).not.toContainText('Kinkaku-ji Temple');
});

// ═══════════════════════════════════════════════════════════════════════════
// (c) day-tab drag ACROSS a leg boundary is rejected — no network call, state unchanged.
//     Day-tabs strips are per-leg DOM regions; onDayTabDrop's own `src.li !== li` guard is
//     the code path under test here (independent of/in addition to any DOM separation).
// ═══════════════════════════════════════════════════════════════════════════

test('day-tab drag across a leg boundary is rejected: no /replan call, itinerary unchanged', async ({ page }) => {
  const PLAN_TWO_TABBED_LEGS = plan(
    'trip-scale-crossleg',
    [
      { leg_id: 'leg-0', city: 'Kyoto', country: 'Japan', num_days: 6, days: KYOTO_DAYS_6 },
      { leg_id: 'leg-1', city: 'Osaka', country: 'Japan', num_days: 6, days: OSAKA_DAYS_6 },
    ],
    [
      { leg_id: 'leg-0', city: 'Kyoto', country: 'Japan', checkin: '2026-11-01', checkout: '2026-11-07' },
      { leg_id: 'leg-1', city: 'Osaka', country: 'Japan', checkin: '2026-11-07', checkout: '2026-11-13' },
    ],
  );
  let replanCalled = false;
  await page.route('**/replan', (route) => {
    replanCalled = true;
    route.fulfill({
      status: 200, contentType: 'application/json',
      body: JSON.stringify({ outcome: 'plan_ready', idempotency_key: 'trip-scale-crossleg', applied: [], rejected: [], notes: [], plan: PLAN_TWO_TABBED_LEGS }),
    });
  });

  await setupPlan(page, PLAN_TWO_TABBED_LEGS, '6 days kyoto then 6 days osaka');
  await page.getByTestId('leg-toggle-1').click();
  await expect(page.getByTestId('day-tab-1-2')).toBeVisible();

  // Leg 0 day 1 (Fushimi Inari) -> leg 1 day 2 (Universal Studios Japan): different legs.
  await dragDayTab(page, 'day-tab-0-1', 'day-tab-1-2');

  // Give any (incorrect) async op a moment to fire before asserting its absence.
  await page.waitForTimeout(300);
  expect(replanCalled).toBe(false);
  await expect(page.getByTestId('replan-flash')).toHaveCount(0);

  // Content on both sides is exactly as it was — nothing silently mutated client-side.
  await page.getByTestId('day-tab-0-1').click();
  await expect(page.getByTestId('day-panel-0-1')).toContainText('Fushimi Inari Shrine');
  await page.getByTestId('day-tab-1-2').click();
  await expect(page.getByTestId('day-panel-1-2')).toContainText('Universal Studios Japan');
});

// ═══════════════════════════════════════════════════════════════════════════
// (d) boundary-day lock — first/last day of a leg is never draggable (arrival/checkout).
//     SAFETY-CRITICAL: REVERT-AND-CONFIRMED below.
// ═══════════════════════════════════════════════════════════════════════════

test('boundary day (first/last of a leg) cannot be drag-reordered, either as source or target', async ({ page }) => {
  let replanCalled = false;
  await page.route('**/replan', (route) => {
    replanCalled = true;
    route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ outcome: 'plan_ready', idempotency_key: 'trip-scale-1', applied: [], rejected: [], notes: [], plan: kyotoLeg() }) });
  });

  await setupPlan(page, kyotoLeg(), '6 days in kyoto');

  const first = page.getByTestId('day-tab-0-0');
  const last = page.getByTestId('day-tab-0-5');
  await expect(first).toHaveAttribute('draggable', 'false');
  await expect(last).toHaveAttribute('draggable', 'false');

  // Boundary day as the DRAG SOURCE (onDayTabDragStart's own isBoundaryDay guard).
  await dragDayTab(page, 'day-tab-0-0', 'day-tab-0-2');
  await page.waitForTimeout(300);
  expect(replanCalled).toBe(false);

  // Boundary day as the DROP TARGET (onDayTabDrop's own isBoundaryDay guard — defense in
  // depth, independent of the source-side check above).
  await dragDayTab(page, 'day-tab-0-1', 'day-tab-0-5');
  await page.waitForTimeout(300);
  expect(replanCalled).toBe(false);

  await expect(page.getByTestId('day-panel-0-0')).toContainText('Nishiki Market');
});

// ═══════════════════════════════════════════════════════════════════════════
// (e) tap-vs-long-press disambiguation + swipe navigation (mobile/tablet touch surface)
// ═══════════════════════════════════════════════════════════════════════════

test.describe('touch interactions', () => {
  test.use({ hasTouch: true });

  test('a quick tap on a day-tab (released well before the 500ms long-press) navigates, does not drag', async ({ page }) => {
    let replanCalled = false;
    await page.route('**/replan', (route) => {
      replanCalled = true;
      route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ outcome: 'plan_ready', idempotency_key: 'trip-scale-1', applied: [], rejected: [], notes: [], plan: kyotoLeg() }) });
    });

    await setupPlan(page, kyotoLeg(), '6 days in kyoto');
    await expect(page.getByTestId('day-panel-0-0')).toBeVisible();

    // Playwright's .tap() dispatches a real touchstart+touchend pair (well under 500ms)
    // followed by the browser's own synthetic click — the same sequence a real phone
    // produces for a tap, exercising onDayTabTouchStart/End's early-release branch.
    await page.getByTestId('day-tab-0-2').tap();

    await expect(page.getByTestId('day-panel-0-2')).toBeVisible();
    await expect(page.getByTestId('day-panel-0-0')).toBeHidden();
    expect(replanCalled).toBe(false); // pure client-side navigation, no /replan side-effect
  });

  test('swipe navigation: adjacent swipe moves one day; tab-click jumps directly across many; boundary swipe is a no-op', async ({ page }) => {
    await setupPlan(page, kyotoLeg(), '6 days in kyoto');
    const panelGroup = '.day-panel-group'; // single leg in this fixture — unambiguous
    await expect(page.getByTestId('day-panel-0-0')).toBeVisible();

    // Swipe LEFT (finger moves right->left, dx=-100px, past SWIPE_MIN_PX=50) -> next day.
    await dispatchTouchOnSelector(page, panelGroup, 'touchstart', 300, 200);
    await dispatchTouchOnSelector(page, panelGroup, 'touchend', 200, 200);
    await expect(page.getByTestId('day-panel-0-1')).toBeVisible();
    await expect(page.getByTestId('day-panel-0-0')).toBeHidden();

    // Direct tab click JUMPS across many days in one action (not a chain of adjacent moves).
    await page.getByTestId('day-tab-0-4').click();
    await expect(page.getByTestId('day-panel-0-4')).toBeVisible();

    // Swipe RIGHT (finger moves left->right, dx=+100px) -> previous day (4 -> 3).
    await dispatchTouchOnSelector(page, panelGroup, 'touchstart', 200, 200);
    await dispatchTouchOnSelector(page, panelGroup, 'touchend', 300, 200);
    await expect(page.getByTestId('day-panel-0-3')).toBeVisible();

    // Boundary: at day 0, a further "previous" swipe must be a no-op (no negative index /
    // no crash) — jump back to day 0 first, then swipe right again.
    await page.getByTestId('day-tab-0-0').click();
    await expect(page.getByTestId('day-panel-0-0')).toBeVisible();
    await dispatchTouchOnSelector(page, panelGroup, 'touchstart', 200, 200);
    await dispatchTouchOnSelector(page, panelGroup, 'touchend', 300, 200);
    await expect(page.getByTestId('day-panel-0-0')).toBeVisible(); // stayed put
  });

  // ═══════════════════════════════════════════════════════════════════════════
  // COVERAGE-GAP FIX (test-audit finding): the only mobile drag coverage above is
  // tap-navigate (e) and swipe-nav (e) — neither one ever actually holds past
  // LONG_PRESS_MS=500 and drags, so onDayTabTouchStart's setTimeout ->
  // touchDragLive=true branch and onDayTabTouchMove's live-drag branch were BOTH
  // dead code as far as e2e was concerned. Now that backend swap_days exists,
  // this drives the real gesture end-to-end: touchstart, a REAL >=500ms hold
  // (not a fake/advanced clock), touchmove onto a different day-tab, touchend.
  //
  // This test ALSO carries the coupling assertion ((a) in the task) — but unlike
  // section (g)'s desktop-drag COUPLING test (which deliberately PRE-fetches
  // both days' photos before swapping, so it only proves thumbCache's NAME-keyed
  // identity is correct, never proves a fetch actually re-fires), this one visits
  // ONLY the drag SOURCE day before swapping. The drag TARGET's content
  // (Kinkaku-ji) is never fetched at its original position (day 3) at all, so the
  // only way it can appear correctly at day-tab position 1 afterward is if
  // dayBatchStartedForPlans's reset genuinely re-armed the position-keyed fetch
  // guard for the new `plans` identity and a fresh fetch actually fired — i.e.
  // this is the one test that would go red if that reset were removed/broken.
  // (Confirmed by a manual revert-and-restore of that reset during authoring.)
  // ═══════════════════════════════════════════════════════════════════════════

  test('mobile long-press (real >=500ms hold) then drag-release on another day-tab completes a day swap: correct /replan swap_days fires, day content visibly swaps, and the newly-swapped-in day fetches ITS OWN photo (not stale)', async ({ page }) => {
    const photoFor: Record<string, string> = {
      'Fushimi Inari Shrine': 'https://img.example/fushimi.jpg',
      'Kinkaku-ji Temple': 'https://img.example/kinkakuji.jpg',
    };
    await page.route('**/place_card', (route) => {
      const body = JSON.parse(route.request().postData() || '{}');
      const url = photoFor[body.name];
      route.fulfill({
        status: 200, contentType: 'application/json',
        body: JSON.stringify({ status: 'ok', place: { photos: url ? [url] : [] } }),
      });
    });
    let replanBody: { ops: unknown[] } | null = null;
    await page.route('**/replan', (route) => {
      replanBody = JSON.parse(route.request().postData() || '{}');
      const swappedPlan = kyotoLeg('trip-scale-1');
      (swappedPlan.day_plans[0] as { days: unknown }).days = swapDayContent(KYOTO_DAYS_6, 1, 3);
      route.fulfill({
        status: 200, contentType: 'application/json',
        body: JSON.stringify({
          outcome: 'plan_ready', idempotency_key: 'trip-scale-1',
          applied: ['swap_days:leg0.day1.day3'], rejected: [], notes: [],
          plan: swappedPlan,
        }),
      });
    });

    await setupPlan(page, kyotoLeg(), '6 days in kyoto');

    // Navigate to day 1 (the drag SOURCE) BEFORE swapping — this both makes it the
    // active tab (so it's still active, and re-checked, after the swap lands) and
    // deliberately "poisons" dayBatchStarted['0:1'] the way the pre-fix code would
    // have left it forever poisoned. Day 3 (the drag TARGET / swap-in content) is
    // never visited at its original position — its photo has NEVER been fetched
    // by any path when the swap happens.
    await page.getByTestId('day-tab-0-1').click();
    await expect(
      page.getByTestId('day-panel-0-1').getByTestId('item-activity-0').locator('.item-thumb img'),
    ).toHaveAttribute('src', photoFor['Fushimi Inari Shrine'], { timeout: 5000 });

    const srcTab = page.getByTestId('day-tab-0-1');
    const dstTab = page.getByTestId('day-tab-0-3');
    const srcBox = await srcTab.boundingBox();
    const dstBox = await dstTab.boundingBox();
    if (!srcBox || !dstBox) throw new Error('missing day-tab bounding box');
    const srcX = srcBox.x + srcBox.width / 2, srcY = srcBox.y + srcBox.height / 2;
    const dstX = dstBox.x + dstBox.width / 2, dstY = dstBox.y + dstBox.height / 2;

    // touch capture semantics: touchmove/touchend keep targeting the element the
    // touchstart fired on (matching this file's swipe test above), even though the
    // finger's clientX/clientY have moved elsewhere — onDayTabTouchMove reads
    // document.elementFromPoint(clientX, clientY) itself to resolve the live target.
    await dispatchTouchOnSelector(page, '[data-testid="day-tab-0-1"]', 'touchstart', srcX, srcY);
    await page.waitForTimeout(600); // REAL wait, past LONG_PRESS_MS=500 -> the hold fires, drag goes live
    await dispatchTouchOnSelector(page, '[data-testid="day-tab-0-1"]', 'touchmove', dstX, dstY);
    await dispatchTouchOnSelector(page, '[data-testid="day-tab-0-1"]', 'touchend', dstX, dstY);

    // The correct /replan swap_days call fired from the gesture (not a tap-nav, not
    // a swipe — the actual long-press-then-drag code path).
    await expect(page.getByTestId('replan-flash')).toContainText('Updated', { timeout: 5000 });
    expect(replanBody).not.toBeNull();
    expect((replanBody!.ops[0] as Record<string, unknown>)).toEqual({
      op: 'swap_days', leg_index: 0, day_a: 1, day_b: 3,
    });

    // Day content visibly swaps: tab position 1 (still active) now shows day 3's
    // original content (Kinkaku-ji), not day 1's original content (Fushimi).
    await expect(page.getByTestId('day-panel-0-1')).toContainText('Kinkaku-ji Temple');
    await expect(page.getByTestId('day-panel-0-1')).not.toContainText('Fushimi Inari Shrine');

    // THE COUPLING CASE, via the real gesture: position 1's photo is Kinkaku-ji's
    // OWN photo — freshly fetched post-swap — never a leftover Fushimi photo and
    // never a stale/blank icon-tile from a fetch that never re-fired.
    await expect(
      page.getByTestId('day-panel-0-1').getByTestId('item-activity-0').locator('.item-thumb img'),
    ).toHaveAttribute('src', photoFor['Kinkaku-ji Temple'], { timeout: 5000 });
  });
});

// ═══════════════════════════════════════════════════════════════════════════
// (f) thumbnails lazy-fetch ONLY for the active day-tab; revisiting a day is a cache hit
// ═══════════════════════════════════════════════════════════════════════════

test('thumbnails lazy-fetch scoped to the active day-tab only; revisiting a day does not re-fetch', async ({ page }) => {
  const calls: string[] = [];
  await page.route('**/place_card', (route) => {
    const body = JSON.parse(route.request().postData() || '{}');
    calls.push(body.name);
    route.fulfill({
      status: 200, contentType: 'application/json',
      body: JSON.stringify({ status: 'ok', place: { photos: [`https://img.example/${encodeURIComponent(body.name)}.jpg`] } }),
    });
  });

  await setupPlan(page, kyotoLeg(), '6 days in kyoto');

  // Day 0 (active by default) fetches ITS item only; nothing else has been touched yet.
  await expect.poll(() => calls.filter((n) => n === 'Nishiki Market').length).toBeGreaterThan(0);
  expect(calls).not.toContain('Fushimi Inari Shrine');
  expect(calls).not.toContain('Kinkaku-ji Temple');
  expect(calls).not.toContain('Nijo Castle');

  // Switch to day 2 -> fetches day 2's item; day 0/1/3/4/5 items remain untouched.
  await page.getByTestId('day-tab-0-2').click();
  await expect.poll(() => calls.filter((n) => n === 'Arashiyama Bamboo Grove').length).toBeGreaterThan(0);
  expect(calls).not.toContain('Fushimi Inari Shrine');
  expect(calls).not.toContain('Kinkaku-ji Temple');

  const day0CallsBeforeRevisit = calls.filter((n) => n === 'Nishiki Market').length;

  // Switch BACK to day 0 (a previously-viewed day) -> cache hit, no additional network call.
  await page.getByTestId('day-tab-0-0').click();
  await page.waitForTimeout(400); // give an accidental re-fetch a chance to fire
  expect(calls.filter((n) => n === 'Nishiki Market').length).toBe(day0CallsBeforeRevisit);
});

// ═══════════════════════════════════════════════════════════════════════════
// (g) THE COUPLING CASE — after a day-tab reorder, each day shows its OWN photo, never a
//     stale one left over from whatever used to occupy that tab position.
//     SAFETY-CRITICAL: REVERT-AND-CONFIRMED below.
// ═══════════════════════════════════════════════════════════════════════════

test('COUPLING: after a day-tab reorder, the swapped-in day shows ITS OWN photo, not a stale one', async ({ page }) => {
  const photoFor: Record<string, string> = {
    'Fushimi Inari Shrine': 'https://img.example/fushimi.jpg',
    'Kinkaku-ji Temple': 'https://img.example/kinkakuji.jpg',
  };
  await page.route('**/place_card', (route) => {
    const body = JSON.parse(route.request().postData() || '{}');
    const url = photoFor[body.name];
    route.fulfill({
      status: 200, contentType: 'application/json',
      body: JSON.stringify({ status: 'ok', place: { photos: url ? [url] : [] } }),
    });
  });
  await page.route('**/replan', (route) => {
    const swappedPlan = kyotoLeg('trip-scale-1');
    (swappedPlan.day_plans[0] as { days: unknown }).days = swapDayContent(KYOTO_DAYS_6, 1, 3);
    route.fulfill({
      status: 200, contentType: 'application/json',
      body: JSON.stringify({
        outcome: 'plan_ready', idempotency_key: 'trip-scale-1',
        applied: ['swap_days:leg0.day1.day3'], rejected: [], notes: [],
        plan: swappedPlan,
      }),
    });
  });

  await setupPlan(page, kyotoLeg(), '6 days in kyoto');

  // Pre-fetch BOTH day 1's and day 3's own photos by visiting each once before reordering,
  // so the post-swap assertion below is testing cache-identity correctness, not merely
  // "did a fetch happen" (that's test (f)'s job).
  await page.getByTestId('day-tab-0-1').click();
  await expect(
    page.getByTestId('day-panel-0-1').getByTestId('item-activity-0').locator('.item-thumb img'),
  ).toHaveAttribute('src', photoFor['Fushimi Inari Shrine'], { timeout: 5000 });

  await page.getByTestId('day-tab-0-3').click();
  await expect(
    page.getByTestId('day-panel-0-3').getByTestId('item-activity-0').locator('.item-thumb img'),
  ).toHaveAttribute('src', photoFor['Kinkaku-ji Temple'], { timeout: 5000 });

  // Reorder: day 1 <-> day 3.
  await dragDayTab(page, 'day-tab-0-1', 'day-tab-0-3');
  await expect(page.getByTestId('replan-flash')).toContainText('Updated', { timeout: 5000 });

  // Day-tab position 1 now holds Kinkaku-ji's content — it must show KINKAKU-JI'S photo,
  // never Fushimi's (the photo that used to live at this tab position).
  await page.getByTestId('day-tab-0-1').click();
  await expect(page.getByTestId('day-panel-0-1')).toContainText('Kinkaku-ji Temple');
  await expect(
    page.getByTestId('day-panel-0-1').getByTestId('item-activity-0').locator('.item-thumb img'),
  ).toHaveAttribute('src', photoFor['Kinkaku-ji Temple']);

  // ...and position 3 now holds Fushimi's content — its own photo, not Kinkaku-ji's.
  await page.getByTestId('day-tab-0-3').click();
  await expect(page.getByTestId('day-panel-0-3')).toContainText('Fushimi Inari Shrine');
  await expect(
    page.getByTestId('day-panel-0-3').getByTestId('item-activity-0').locator('.item-thumb img'),
  ).toHaveAttribute('src', photoFor['Fushimi Inari Shrine']);
});

// ═══════════════════════════════════════════════════════════════════════════
// (b) duplicate photo affordance removal — the pre-fix markup rendered an
//     embedded <PlacePhotos> component ALONGSIDE the new always-visible Part B
//     thumbnail button (leg-thumb / item-thumb), i.e. two separate "see a photo"
//     controls for the same hotel/item. The fix deletes the <PlacePhotos>
//     instances from Itinerary.svelte entirely (Preview.svelte's own leg-hero
//     banner is untouched — a different component, out of scope here). Assert
//     by DOM element COUNT, not just "a photo control exists", so a regression
//     that re-introduces a second affordance is actually caught.
// ═══════════════════════════════════════════════════════════════════════════

test('exactly one photo affordance renders per leg-header and per item row (no duplicate PlacePhotos + thumb)', async ({ page }) => {
  await page.route('**/place_card', (route) =>
    route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ status: 'ok', place: { photos: [] } }) }));

  const withHotel = kyotoLeg();
  (withHotel.legs[0] as Record<string, unknown>).hotel_title = 'Kyoto Grand Hotel';
  await setupPlan(page, withHotel, '6 days in kyoto');

  // Leg header: exactly one photo affordance (the leg-hero thumbnail button).
  await expect(page.locator('[data-testid="leg-h-0"] .leg-thumb')).toHaveCount(1);
  // No leftover embedded <PlacePhotos> toggle (its distinguishing "📷 Photo" label)
  // anywhere in the leg header.
  await expect(page.locator('[data-testid="leg-h-0"]').getByText('📷 Photo')).toHaveCount(0);

  // Item row (day 0's sole item, active by default) — scoped to day 0's OWN panel:
  // day-tabs mount every day's panel simultaneously (just hidden — see draft2's
  // "never unmounted" note), so ALL 6 days each have their own item-activity-0;
  // an unscoped locator would match 6 elements, one per day, not "duplicates on
  // one row". Exactly one photo affordance within THIS row — not two.
  const day0Item = page.getByTestId('day-panel-0-0').getByTestId('item-activity-0');
  await expect(day0Item.locator('.item-thumb')).toHaveCount(1);
  await expect(day0Item.getByText('📷 Photo')).toHaveCount(0);
});

// ═══════════════════════════════════════════════════════════════════════════
// (c) collapsed-leg honesty fix — the leg-hero thumbnail fetches EAGERLY for
//     every leg regardless of open/collapsed state (see Itinerary.svelte's
//     `$: { plans.forEach(...) }` block comment: gating this fetch on `isOpen`
//     would leave a collapsed leg's thumbnail PERMANENTLY unfetched, so its
//     icon-tile fallback would render "No photo available" — a false
//     "confirmed absent" claim when the truth is just "not yet checked"). The
//     leg HEADER (with its thumbnail button) always renders even while the
//     leg's day-by-day BODY is collapsed, so this is fully observable without
//     ever expanding the leg.
// ═══════════════════════════════════════════════════════════════════════════

test("a collapsed leg's thumbnail is fetched eagerly and shows the real photo, never a false 'no photo' state", async ({ page }) => {
  const PLAN_TWO_LEGS = plan(
    'trip-scale-collapsed',
    [
      { leg_id: 'leg-0', city: 'Kyoto', country: 'Japan', num_days: 6, days: KYOTO_DAYS_6 },
      { leg_id: 'leg-1', city: 'Osaka', country: 'Japan', num_days: 6, days: OSAKA_DAYS_6 },
    ],
    [
      { leg_id: 'leg-0', city: 'Kyoto', country: 'Japan', checkin: '2026-11-01', checkout: '2026-11-07', hotel_title: 'Kyoto Grand Hotel' },
      { leg_id: 'leg-1', city: 'Osaka', country: 'Japan', checkin: '2026-11-07', checkout: '2026-11-13', hotel_title: 'Osaka Bay Hotel' },
    ],
  );
  await page.route('**/place_card', (route) => {
    const body = JSON.parse(route.request().postData() || '{}');
    const url = body.name === 'Osaka Bay Hotel' ? 'https://img.example/osaka-bay.jpg' : null;
    route.fulfill({
      status: 200, contentType: 'application/json',
      body: JSON.stringify({ status: 'ok', place: { photos: url ? [url] : [] } }),
    });
  });

  await setupPlan(page, PLAN_TWO_LEGS, '6 days kyoto then 6 days osaka');

  // 12 total trip days >= 8 -> legCollapseDefault true; only leg 0 opens by default,
  // so leg 1 (Osaka) starts COLLAPSED — its day-tabs body never mounts...
  await expect(page.getByTestId('day-tabs-1')).toHaveCount(0);
  await expect(page.getByTestId('leg-collapsed-1')).toBeVisible();
  // ...but the leg HEADER (and its hero thumbnail button) is always present.
  const collapsedThumb = page.locator('[data-testid="leg-h-1"] .leg-thumb');
  await expect(collapsedThumb).toBeVisible();

  // The eager fetch resolves a REAL photo for the still-collapsed leg — the button
  // must end up in its "has a photo" state (real <img>, no .icon-tile class, an
  // honest "View photos of…" label), never stuck falsely claiming "No photo
  // available" for a photo that was simply never checked because the leg happened
  // to be collapsed.
  await expect(collapsedThumb.locator('img')).toHaveAttribute('src', 'https://img.example/osaka-bay.jpg', { timeout: 5000 });
  await expect(collapsedThumb).not.toHaveClass(/icon-tile/);
  await expect(collapsedThumb).toHaveAttribute('aria-label', 'View photos of Osaka Bay Hotel');
  await expect(collapsedThumb).toHaveAttribute('title', 'Tap for photos');

  // Leg 1's body genuinely never mounted at any point in this test (this wasn't a
  // "fetched only after expanding" trick) — day-tabs-1 still doesn't exist.
  await expect(page.getByTestId('day-tabs-1')).toHaveCount(0);
});

// ═══════════════════════════════════════════════════════════════════════════
// (h) RightRail-sourced drag over a day-tab must still dwell-switch (F1 spillover)
// ═══════════════════════════════════════════════════════════════════════════
//
// Bug: onDayTabDragOver's dwell-switch (hover a day-tab for 600ms mid-drag ->
// the visible day switches so its off-screen drop zones become reachable) was
// gated on the module-local `dragKind` flag alone ('activity' | 'suggestion').
// dragKind is set ONLY by this component's OWN onDragStart/onDragStartSuggestion
// — a drag SOURCED from the separate RightRail.svelte component (its
// onSugDragStart) never touches Itinerary's dragKind, so it stayed `null` for
// the whole drag. Net effect: on any tabbed leg (>=6 days), a RightRail
// suggestion card could only ever be dropped into the CURRENTLY-ACTIVE day —
// every other day's panel stayed `tab-hidden` (display:none, never unmounted)
// and unreachable, because the tab hover never triggered the switch.
//
// Fix mirrors the identical gate already fixed in onDragOverItem: also
// accept `e.dataTransfer?.effectAllowed === 'copy'`, which BOTH
// suggestion sources set at dragstart (RightRail's onSugDragStart included),
// regardless of which component started the drag.
//
// Per edit-lane.spec.ts's own documented finding (see its "Real pointer-driven
// drag via Playwright's dragTo()" comments), a manually-dispatched DragEvent
// sequence via page.evaluate() does NOT reliably transition dataTransfer's
// read/write mode for a subsequent getData() call — it silently no-ops for the
// final onDrop() in this app. So this test drives a genuine pointer-based
// browser drag (mouse down -> move over the tab -> hold there -> move over the
// target day's drop-end zone -> mouse up) via page.mouse, the same primitive
// Playwright's own dragTo() is built on, instead of the synthetic-DragEvent
// pattern dragDayTab() uses elsewhere in this file (safe there only because
// day-tab-to-day-tab drops read module state, never dataTransfer.getData()).
//
// One more wrinkle specific to a PAUSE mid-drag (which dragTo() itself never
// does — it goes straight from source to target): Chromium's CDP drag
// interception only emits a fresh 'dragover' DOM event in direct response to
// an explicit mouse.move() call. A stationary pointer during a plain
// page.waitForTimeout() produces none at all (unlike a genuine OS-level drag,
// which keeps re-firing dragover on its own timer) — so hoverAndDwell() below
// nudges the pointer by 1px repeatedly while waiting, just to keep generating
// real 'dragover' dispatches at the target until the component's own 600ms
// dwell setTimeout has had a genuine chance to elapse.
async function hoverAndDwell(page, cx: number, cy: number, totalMs: number, stepMs = 90) {
  for (let waited = 0; waited < totalMs; waited += stepMs) {
    await page.mouse.move(cx + (waited % (stepMs * 2) === 0 ? 1 : -1), cy);
    await page.waitForTimeout(stepMs);
  }
}

test('RightRail suggestion drag over a day-tab dwell-switches to that day and drops into it', async ({ page }) => {
  const env = kyotoLeg('trip-dwell-1');
  (env.day_plans[0] as Record<string, unknown>).unscheduled_attractions = [
    { name: 'Ueno Park', category: 'tourism=attraction' },
  ];

  let capturedBody: string | null = null;
  await page.route('**/replan', (route) => {
    capturedBody = route.request().postData();
    route.fulfill({
      status: 200, contentType: 'application/json',
      body: JSON.stringify({
        outcome: 'plan_ready', idempotency_key: 'trip-dwell-1',
        applied: ['add:leg0.day2:Ueno Park'], rejected: [], notes: [], plan: env,
      }),
    });
  });
  await setupPlan(page, env, '6 days in kyoto');

  await expect(page.getByTestId('day-tab-0-2')).toBeVisible();
  await expect(page.getByTestId('day-panel-0-2')).toBeHidden();
  const sugCard = page.getByTestId('sug-item-Ueno Park');
  await expect(sugCard).toBeVisible();

  const srcBox = (await sugCard.boundingBox())!;
  const tabBox = (await page.getByTestId('day-tab-0-2').boundingBox())!;
  const tabCx = tabBox.x + tabBox.width / 2;
  const tabCy = tabBox.y + tabBox.height / 2;

  await page.mouse.move(srcBox.x + srcBox.width / 2, srcBox.y + srcBox.height / 2);
  await page.mouse.down();
  await page.mouse.move(tabCx, tabCy, { steps: 20 });
  await hoverAndDwell(page, tabCx, tabCy, 750); // dwell threshold is 600ms

  // Core regression lock: the dwell fired -> day 2 became the active tab/panel
  // WHILE STILL DRAGGING (before any drop) — this is the exact behavior the bug
  // prevented for a RightRail-sourced drag.
  await expect(page.getByTestId('day-tab-0-2')).toHaveClass(/active/);
  await expect(page.getByTestId('day-panel-0-2')).toBeVisible();

  const dropBox = (await page.getByTestId('drop-end-0-2').boundingBox())!;
  const dropCx = dropBox.x + dropBox.width / 2;
  const dropCy = dropBox.y + dropBox.height / 2;
  await page.mouse.move(dropCx, dropCy, { steps: 20 });
  await hoverAndDwell(page, dropCx, dropCy, 240, 60); // settle so the drop resolves over the zone
  await page.mouse.up();

  // And the drop landed in DAY 2 (now reachable), not the day that was active
  // at dragstart.
  expect(capturedBody).not.toBeNull();
  const body = JSON.parse(capturedBody!);
  expect(body.ops[0].op).toBe('add_place');
  expect(body.ops[0].day_index).toBe(2);
  expect(body.ops[0].attraction_ref).toBe('Ueno Park');
});
