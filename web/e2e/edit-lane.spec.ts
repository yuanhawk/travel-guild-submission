import { test, expect } from '@playwright/test';

// edit-lane.spec.ts — Edit lane: variant toggle / remove place → POST /replan → itinerary updates.
//
// The Itinerary component is editable when state === 'plan_ready' OR 'success'.
// All structural changes funnel through runOps() → replanTrip() → POST /replan.
// On success (outcome:'plan_ready', applied.length > 0), dispatch('replanned') fires →
// App.svelte swaps result → Svelte re-renders the itinerary from the server envelope.
// On noop (every op rejected), an honest flash shows and plan is unchanged.
//
// Tests:
//   1. Variant toggle (bad_weather day) → /replan switch_variant → "Updated ✓" flash
//   2. Remove place → /replan remove_place → attraction gone from itinerary
//   3. Noop → honest "Couldn't apply" flash, plan unchanged

const DAY_0 = {
  day_index: 0,
  bad_weather: true,
  // fair_weather_attractions must be an array (even empty) for hasVariant to be true
  fair_weather_attractions: [{ name: 'Fushimi Inari Shrine', category: 'tourism=attraction', lat: 34.967, lon: 135.772 }],
  attractions: [{
    name: 'Nishiki Market', category: 'tourism=attraction',
    lat: 35.005, lon: 135.765, fee: null, weather_exposure: 'indoor',
  }],
  meals: { breakfast: { name: 'Ippodo Tea', category: 'amenity=cafe', cuisine: 'tea' } },
};

const PLAN_READY = {
  outcome: 'plan_ready',
  payment_status: 'held',
  booking_ref: null,
  idempotency_key: 'trip-el-1',
  package_total_with_fees_cents: 180000,
  total_budget_cents: 400000,
  wallet: { balance_cents: 500000, held_cents: 180000, debited: false },
  legs: [{ leg_id: 'leg-0', city: 'Kyoto', checkin: '2026-11-01', checkout: '2026-11-04' }],
  day_plans: [{
    leg_id: 'leg-0', city: 'Kyoto', num_days: 3,
    days: [DAY_0],
  }],
  risk_signals: { per_leg: [] },
};

// Updated plan returned by /replan after the attraction is removed
const PLAN_AFTER_REMOVE = {
  ...PLAN_READY,
  day_plans: [{
    ...PLAN_READY.day_plans[0],
    days: [{ ...DAY_0, attractions: [] }],  // attraction removed
  }],
};

// Updated plan returned by /replan after variant switch
const PLAN_AFTER_VARIANT = {
  ...PLAN_READY,
  day_plans: [{
    ...PLAN_READY.day_plans[0],
    days: [{
      ...DAY_0,
      // After switch to fair: _variant_active = 'fair', bad_weather flag still true
      _variant_active: 'fair',
      attractions: DAY_0.fair_weather_attractions,
    }],
  }],
};

async function setupPlan(page) {
  await page.addInitScript(() => {
    localStorage.setItem('tg_session', JSON.stringify({ user: null, guest: true, savedAt: Date.now() }));
  });
  await page.route('**/negotiate_text', (route) =>
    route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(PLAN_READY) }));
  await page.route('**/tile.openstreetmap.org/**', (route) => route.abort());
  await page.goto('/');
  await page.getByTestId('chat-input').fill('3 days in kyoto, markets, temples');
  await page.getByRole('button', { name: 'Send' }).click();
  // Wait for editable itinerary to render
  await expect(page.getByTestId('itinerary')).toBeVisible({ timeout: 8000 });
  // bad_weather day shows wet-weather indicator
  await expect(page.getByTestId('itinerary')).toContainText('wet-weather risk');
}

test('variant toggle → /replan switch_variant → Updated ✓ flash', async ({ page }) => {
  await page.route('**/replan', (route) =>
    route.fulfill({
      status: 200, contentType: 'application/json',
      body: JSON.stringify({
        outcome: 'plan_ready',
        idempotency_key: 'trip-el-1',
        applied: ['switch_variant:leg0.day0→fair'],
        rejected: [],
        notes: [],
        plan: PLAN_AFTER_VARIANT,
      }),
    }));

  await setupPlan(page);

  // The variant toggle button shows because bad_weather=true AND fair_weather_attractions is an array
  const variantBtn = page.getByTestId('variant-0-0');
  await expect(variantBtn).toBeVisible();
  await expect(variantBtn).toContainText('fair-weather'); // shows "Show fair-weather plan ☀" initially

  await variantBtn.click();

  // "Updated ✓" flash confirms the server applied the change
  const flash = page.getByTestId('replan-flash');
  await expect(flash).toBeVisible({ timeout: 5000 });
  await expect(flash).toContainText('Updated');

  // Itinerary re-renders from the server envelope (Fushimi Inari instead of Nishiki Market)
  await expect(page.getByTestId('itinerary')).toContainText('Fushimi Inari', { timeout: 5000 });
});

test('remove place → /replan remove_place → attraction gone from itinerary', async ({ page }) => {
  await page.route('**/replan', (route) =>
    route.fulfill({
      status: 200, contentType: 'application/json',
      body: JSON.stringify({
        outcome: 'plan_ready',
        idempotency_key: 'trip-el-1',
        applied: ['remove:leg0.day0.pos0:Nishiki Market'],
        rejected: [],
        notes: [],
        plan: PLAN_AFTER_REMOVE,
      }),
    }));

  await setupPlan(page);

  // Attraction is visible before remove
  await expect(page.getByTestId('itinerary')).toContainText('Nishiki Market');

  // Remove button for leg=0, day=0, rawIndex=0 (the one and only attraction)
  const removeBtn = page.getByTestId('remove-0-0-0');
  await expect(removeBtn).toBeVisible();
  await removeBtn.click();

  // Flash confirms the server applied the change
  await expect(page.getByTestId('replan-flash')).toContainText('Updated', { timeout: 5000 });

  // Attraction removed from the itinerary (server envelope is truth)
  await expect(page.getByTestId('itinerary')).not.toContainText('Nishiki Market', { timeout: 5000 });
});

test('noop from /replan → honest flash, plan unchanged', async ({ page }) => {
  await page.route('**/replan', (route) =>
    route.fulfill({
      status: 200, contentType: 'application/json',
      body: JSON.stringify({
        outcome: 'noop',
        idempotency_key: 'trip-el-1',
        applied: [],
        rejected: [{ op: 'remove_place', reason: 'attraction not found at position' }],
        notes: [],
        plan: PLAN_READY,   // unchanged plan echoed back
      }),
    }));

  await setupPlan(page);

  // Trigger any op — noop path fires for stale-board rejections
  await page.getByTestId('remove-0-0-0').click();

  // Honest "Couldn't apply" flash (not a silent failure)
  const flash = page.getByTestId('replan-flash');
  await expect(flash).toBeVisible({ timeout: 5000 });
  await expect(flash).toContainText("Couldn't apply");

  // Plan is UNCHANGED — attraction still shown (server returned the same plan)
  await expect(page.getByTestId('itinerary')).toContainText('Nishiki Market');
});

// ── drag-into-day / suggestion pool tests (#121) ──────────────────────────────
//
// attractionRef({name:'Ueno Park'}) → 'Ueno Park'  (no wikidata/name_en present)
// So all testids use the name string, not the ua_ref field.
// The add-btn click fires: runOps([{op:'add_place', leg_index, day_index, position, attraction_ref, from:'unscheduled'}])
// The sug-chip click (added in this PR) does the same for the current day.

const PLAN_WITH_SUGGESTIONS = {
  outcome: 'plan_ready',
  payment_status: 'held',
  booking_ref: null,
  idempotency_key: 'trip-drag-1',
  package_total_with_fees_cents: 100000,
  total_budget_cents: 300000,
  wallet: { balance_cents: 500000, held_cents: 100000, debited: false },
  legs: [{ leg_id: 'leg-0', city: 'Tokyo', checkin: '2026-10-01', checkout: '2026-10-05' }],
  day_plans: [{
    leg_id: 'leg-0', city: 'Tokyo', num_days: 4,
    days: [{
      day_index: 0, bad_weather: false,
      attractions: [{ name: 'Senso-ji Temple', category: 'tourism=attraction',
                      weather_exposure: 'outdoor', lat: 35.71, lon: 139.79, fee: null }],
      meals: { lunch: { name: 'Test Ramen', category: 'amenity=restaurant', cuisine: 'japanese' } },
      intracity_hops: [],
    }],
    unscheduled_attractions: [{ name: 'Ueno Park', ua_ref: 'ua-ueno-park', category: 'tourism=attraction' }],
  }],
  risk_signals: { per_leg: [] },
};

// attractionRef uses name (no wikidata/name_en), so pool testid key is the plain name
const UENO_REF = 'Ueno Park';

// Response after add_place; Ueno Park is now in day attractions, pool is empty
const REPLAN_AFTER_ADD = {
  ...PLAN_WITH_SUGGESTIONS,
  day_plans: [{
    ...PLAN_WITH_SUGGESTIONS.day_plans[0],
    days: [{
      ...PLAN_WITH_SUGGESTIONS.day_plans[0].days[0],
      attractions: [
        { name: 'Senso-ji Temple', category: 'tourism=attraction',
          weather_exposure: 'outdoor', lat: 35.71, lon: 139.79, fee: null },
        { name: 'Ueno Park', category: 'tourism=attraction',
          weather_exposure: 'outdoor', lat: 35.71, lon: 139.72, fee: null },
      ],
    }],
    unscheduled_attractions: [],
  }],
};

async function setupPlanWithSuggestions(page) {
  await page.addInitScript(() => {
    localStorage.setItem('tg_session', JSON.stringify({ user: null, guest: true, savedAt: Date.now() }));
  });
  await page.route('**/negotiate_text', (route) =>
    route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(PLAN_WITH_SUGGESTIONS) }));
  await page.route('**/tile.openstreetmap.org/**', (route) => route.abort());
  await page.goto('/');
  await page.getByTestId('chat-input').fill('4 days in tokyo');
  await page.getByRole('button', { name: 'Send' }).click();
  // Wait for the scheduled attraction to confirm itinerary is rendered
  await expect(page.getByTestId('itinerary')).toContainText('Senso-ji Temple', { timeout: 8000 });
}

// The per-leg "unscheduled pool" (unscheduled-*/pool-item-*/add-0-* testids) was removed —
// it fully duplicated the per-day chips (sug-chip-*, tested below) and RightRail's Suggested
// tab (sug-item-*/sug-add-*, tested here). These two tests replace its former coverage.

test('RightRail Suggested tab renders the unscheduled candidate', async ({ page }) => {
  await setupPlanWithSuggestions(page);

  // "Suggested" is RightRail's default-open tab — no extra click needed.
  await expect(page.getByTestId('tab-suggested')).toBeVisible();
  await expect(page.getByTestId(`sug-item-${UENO_REF}`)).toBeVisible();
  await expect(page.getByTestId(`sug-add-${UENO_REF}`)).toBeVisible();

  // Card is a genuine drag source (not just decoration) — see the drag test below for
  // the full drop path.
  await expect(page.getByTestId(`sug-item-${UENO_REF}`)).toHaveAttribute('draggable', 'true');

  // The "Click + Add, or drag a card straight into a day." instructional hint was
  // removed as unnecessary UI clutter — must not reappear in the Suggested pane.
  await expect(page.getByTestId('tab-suggested')).not.toContainText('Click + Add');
  await expect(page.getByTestId('tab-suggested')).not.toContainText('drag a card straight into a day');
});

test('click + Add in RightRail Suggested tab → /replan add_place → suggestion moves to itinerary', async ({ page }) => {
  let capturedBody: string | null = null;
  await page.route('**/replan', (route) => {
    capturedBody = route.request().postData();
    route.fulfill({
      status: 200, contentType: 'application/json',
      body: JSON.stringify({
        outcome: 'plan_ready',
        idempotency_key: 'trip-drag-1',
        applied: [`add:leg0.day0:${UENO_REF}`],
        rejected: [],
        notes: [],
        plan: REPLAN_AFTER_ADD,
      }),
    });
  });

  await setupPlanWithSuggestions(page);

  await page.getByTestId(`sug-add-${UENO_REF}`).click();

  // RightRail's own add-flash confirms the server applied the change (distinct testid
  // from Itinerary's replan-flash — this add path is driven entirely from the rail).
  await expect(page.getByTestId('sug-flash')).toContainText('Added', { timeout: 5000 });

  expect(capturedBody).not.toBeNull();
  const body = JSON.parse(capturedBody!);
  expect(body.ops[0].op).toBe('add_place');
  expect(body.ops[0].attraction_ref).toBe(UENO_REF);
  expect(body.ops[0].from).toBe('unscheduled');

  // Ueno Park now appears in the itinerary (server envelope is truth)
  await expect(page.getByTestId('itinerary')).toContainText('Ueno Park', { timeout: 5000 });

  // Candidate is gone from the Suggested tab — unscheduled_attractions is empty in the new plan
  await expect(page.getByTestId(`sug-item-${UENO_REF}`)).not.toBeVisible({ timeout: 5000 });
});

test('drag a RightRail Suggested card into a day → /replan add_place with the correct payload', async ({ page }) => {
  let capturedBody: string | null = null;
  await page.route('**/replan', (route) => {
    capturedBody = route.request().postData();
    route.fulfill({
      status: 200, contentType: 'application/json',
      body: JSON.stringify({
        outcome: 'plan_ready',
        idempotency_key: 'trip-drag-1',
        applied: [`add:leg0.day0:${UENO_REF}`],
        rejected: [],
        notes: [],
        plan: REPLAN_AFTER_ADD,
      }),
    });
  });

  await setupPlanWithSuggestions(page);

  // Real pointer-driven drag via Playwright's dragTo() -- NOT a manually-dispatched
  // DragEvent sequence. The HTML5 spec gives dataTransfer a "mode" (read/write during
  // dragstart, protected during dragover, read-only during drop); Itinerary's onDrop
  // calls dataTransfer.getData() and only a genuine browser-recognized drag reliably
  // transitions that mode. A synthetic dispatchEvent() sequence does not (confirmed:
  // it silently no-ops here, matching why the existing dragDayTab() helper elsewhere
  // in this suite deliberately avoids getData() and reads component state instead).
  // dragTo() exercises the real cross-component path this feature actually depends on.
  await page.getByTestId(`sug-item-${UENO_REF}`).dragTo(page.getByTestId('drop-end-0-0'));

  expect(capturedBody).not.toBeNull();
  const body = JSON.parse(capturedBody!);
  expect(body.ops[0].op).toBe('add_place');
  expect(body.ops[0].attraction_ref).toBe(UENO_REF);
  expect(body.ops[0].from).toBe('unscheduled');
  await expect(page.getByTestId('itinerary')).toContainText('Ueno Park', { timeout: 5000 });
});

// ── mid-list drop-zone position test (drag-order bug fix regression) ─────────
//
// Bug: dropping a RightRail Suggested card at a SPECIFIC mid-list position
// always landed at the bottom of the day instead. Root cause (confirmed):
// Itinerary.svelte's per-item "drop BEFORE this item" hit target was only 8px
// tall against much taller (52-56px thumbnail) item rows, AND `.item` itself
// was a dead (non-accepting) drop target for a RightRail-originated drag
// (onDragOverItem gated on a dragKind flag RightRail never sets) — so a miss
// on the thin zone had nowhere valid to land except the big `drop-end` zone at
// the bottom. Fixed by growing the drop-zone hit box (8px -> 24px, matching
// drop-end) and having onDragOverItem also recognize effectAllowed==='copy'
// (set by both suggestion drag sources) so `.item` becomes a swap-accepting
// (not dead) hover target regardless of drag origin.
//
// Day 0 has TWO scheduled attractions (Senso-ji, Tokyo Tower); dropping Ueno
// Park on the drop-zone BEFORE Tokyo Tower (attrIndex=1) must send position=1
// (mid-list), not position=2 (end-of-day).

const PLAN_TWO_ATTRACTIONS = {
  outcome: 'plan_ready',
  payment_status: 'held',
  booking_ref: null,
  idempotency_key: 'trip-drag-mid-1',
  package_total_with_fees_cents: 100000,
  total_budget_cents: 300000,
  wallet: { balance_cents: 500000, held_cents: 100000, debited: false },
  legs: [{ leg_id: 'leg-0', city: 'Tokyo', checkin: '2026-10-01', checkout: '2026-10-05' }],
  day_plans: [{
    leg_id: 'leg-0', city: 'Tokyo', num_days: 4,
    days: [{
      day_index: 0, bad_weather: false,
      attractions: [
        { name: 'Senso-ji Temple', category: 'tourism=attraction',
          weather_exposure: 'outdoor', lat: 35.71, lon: 139.79, fee: null },
        { name: 'Tokyo Tower', category: 'tourism=attraction',
          weather_exposure: 'outdoor', lat: 35.65, lon: 139.74, fee: null },
      ],
      meals: {},
      intracity_hops: [],
      // Pre-add gap: Senso-ji -> Tokyo Tower only (1 pair, 2 items).
      item_transport_gaps: [{ mode: 'taxi', minutes: 30, estimate: true }],
    }],
    unscheduled_attractions: [{ name: 'Ueno Park', ua_ref: 'ua-ueno-park', category: 'tourism=attraction' }],
  }],
  risk_signals: { per_leg: [] },
};

// Response after add_place at the MID position: [Senso-ji, Ueno Park, Tokyo Tower].
// Task 3 (real-time recompute): the server recomputes item_transport_gaps for the
// NEW 3-item order (2 pairs) — the frontend must render THESE values, not the
// stale 1-entry pre-add array above.
const REPLAN_AFTER_MID_ADD = {
  ...PLAN_TWO_ATTRACTIONS,
  day_plans: [{
    ...PLAN_TWO_ATTRACTIONS.day_plans[0],
    days: [{
      ...PLAN_TWO_ATTRACTIONS.day_plans[0].days[0],
      attractions: [
        { name: 'Senso-ji Temple', category: 'tourism=attraction',
          weather_exposure: 'outdoor', lat: 35.71, lon: 139.79, fee: null },
        { name: 'Ueno Park', category: 'tourism=attraction',
          weather_exposure: 'outdoor', lat: 35.71, lon: 139.72, fee: null },
        { name: 'Tokyo Tower', category: 'tourism=attraction',
          weather_exposure: 'outdoor', lat: 35.65, lon: 139.74, fee: null },
      ],
      item_transport_gaps: [
        { mode: 'walk', minutes: 10, estimate: true },   // Senso-ji -> Ueno Park
        { mode: 'metro', minutes: 20, estimate: true },  // Ueno Park -> Tokyo Tower
      ],
    }],
    unscheduled_attractions: [],
  }],
};

// Response after a SWAP (drop directly onto the Tokyo Tower item row): Tokyo Tower
// is replaced in place by Ueno Park — [Senso-ji, Ueno Park], still 2 items.
const REPLAN_AFTER_SWAP = {
  ...PLAN_TWO_ATTRACTIONS,
  day_plans: [{
    ...PLAN_TWO_ATTRACTIONS.day_plans[0],
    days: [{
      ...PLAN_TWO_ATTRACTIONS.day_plans[0].days[0],
      attractions: [
        { name: 'Senso-ji Temple', category: 'tourism=attraction',
          weather_exposure: 'outdoor', lat: 35.71, lon: 139.79, fee: null },
        { name: 'Ueno Park', category: 'tourism=attraction',
          weather_exposure: 'outdoor', lat: 35.71, lon: 139.72, fee: null },
      ],
      item_transport_gaps: [{ mode: 'walk', minutes: 12, estimate: true }],
    }],
    unscheduled_attractions: [],
  }],
};

test('drag a RightRail Suggested card onto a MID-list drop-zone → lands at that position, not the bottom', async ({ page }) => {
  let capturedBody: string | null = null;
  await page.route('**/negotiate_text', (route) =>
    route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(PLAN_TWO_ATTRACTIONS) }));
  await page.route('**/tile.openstreetmap.org/**', (route) => route.abort());
  await page.route('**/replan', (route) => {
    capturedBody = route.request().postData();
    route.fulfill({
      status: 200, contentType: 'application/json',
      body: JSON.stringify({
        outcome: 'plan_ready',
        idempotency_key: 'trip-drag-mid-1',
        applied: [`add:leg0.day0.pos1:${UENO_REF}`],
        rejected: [],
        notes: [],
        plan: REPLAN_AFTER_MID_ADD,
      }),
    });
  });

  await page.addInitScript(() => {
    localStorage.setItem('tg_session', JSON.stringify({ user: null, guest: true, savedAt: Date.now() }));
  });
  await page.goto('/');
  await page.getByTestId('chat-input').fill('4 days in tokyo, two spots one day');
  await page.getByRole('button', { name: 'Send' }).click();
  await expect(page.getByTestId('itinerary')).toContainText('Senso-ji Temple', { timeout: 8000 });
  await expect(page.getByTestId('itinerary')).toContainText('Tokyo Tower');

  // Real pointer-driven drag (Playwright's dragTo(), not a synthetic DragEvent
  // sequence — see the drag-end test above for why that landmine matters here)
  // onto the drop-zone BEFORE Tokyo Tower (attrIndex=1) — the mid-list target,
  // not drop-end-0-0.
  const midZone = page.getByTestId('drop-0-0-1');
  await expect(midZone).toBeAttached();
  await page.getByTestId(`sug-item-${UENO_REF}`).dragTo(midZone);

  expect(capturedBody).not.toBeNull();
  const body = JSON.parse(capturedBody!);
  expect(body.ops[0].op).toBe('add_place');
  expect(body.ops[0].attraction_ref).toBe(UENO_REF);
  // The core regression lock: position must be the MID-list index (1), never
  // the end-of-day index (2, i.e. day.attractions.length at drop time).
  expect(body.ops[0].position).toBe(1);
  expect(body.ops[0].position).not.toBe(2);

  // Server envelope is truth: Ueno Park now renders BETWEEN the two originals.
  await expect(page.getByTestId('itinerary')).toContainText('Ueno Park', { timeout: 5000 });
});

// ── drop-zone REST-STATE height lock (audit follow-up) ────────────────────────
//
// The MID-list test above uses Playwright's dragTo(), which targets the drop
// target's geometric CENTER — that hits reliably whether the zone is 8px or 24px
// tall, so it does NOT actually exercise the height fix. This test checks the
// computed height directly, in the REST state (no hover/drag in progress), which
// is the real fix: pre-fix this was 8px, post-fix it's 24px. A real mouse/
// trackpad drag hit-tests against this resting box, not the `.drop-target`
// hover-expanded line (which was already 24px both before and after).
test('drop-zone rest-state height is the real 24px hit target (not just hover-expanded)', async ({ page }) => {
  await page.route('**/negotiate_text', (route) =>
    route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(PLAN_TWO_ATTRACTIONS) }));
  await page.route('**/tile.openstreetmap.org/**', (route) => route.abort());

  await page.addInitScript(() => {
    localStorage.setItem('tg_session', JSON.stringify({ user: null, guest: true, savedAt: Date.now() }));
  });
  await page.goto('/');
  await page.getByTestId('chat-input').fill('4 days in tokyo, two spots one day');
  await page.getByRole('button', { name: 'Send' }).click();
  await expect(page.getByTestId('itinerary')).toContainText('Senso-ji Temple', { timeout: 8000 });
  await expect(page.getByTestId('itinerary')).toContainText('Tokyo Tower');

  // No hover/drag has happened at this point — this is the resting box.
  const midZone = page.getByTestId('drop-0-0-1');
  await expect(midZone).toBeAttached();
  await expect(midZone).toHaveCSS('height', '24px');
});

// ── swap-onto-item-row lock (audit follow-up) ─────────────────────────────────
//
// The MID-list test and the transport-chip test below both drag onto a
// `.drop-zone` — they never drag onto an `.item` ROW directly, so neither
// exercises onDragOverItem/onDropOnItem (the swap path) at all. Pre-fix,
// onDragOverItem only accepted the module-local `dragKind === 'suggestion'`
// flag, which RightRail's onSugDragStart (a separate component) never sets —
// so it never called preventDefault() for a RightRail-sourced drag, the browser
// never delivered a 'drop' event to the item row, and this swap could not
// happen for ANY RightRail-originated drag. This test drags a RightRail
// Suggested card directly onto the Tokyo Tower item row (data-testid
// `item-activity-1`, not a drop-zone testid) and asserts the resulting
// /replan call is the SWAP op-pair onDropOnItem actually sends: remove_place
// (the existing item, at its position) followed by add_place (the dragged
// suggestion, at that SAME position) — see onDropOnItem in Itinerary.svelte.
test('drag a RightRail Suggested card directly onto an item row → /replan swap (remove + add at same position)', async ({ page }) => {
  let capturedBody: string | null = null;
  await page.route('**/negotiate_text', (route) =>
    route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(PLAN_TWO_ATTRACTIONS) }));
  await page.route('**/tile.openstreetmap.org/**', (route) => route.abort());
  await page.route('**/replan', (route) => {
    capturedBody = route.request().postData();
    route.fulfill({
      status: 200, contentType: 'application/json',
      body: JSON.stringify({
        outcome: 'plan_ready',
        idempotency_key: 'trip-drag-mid-1',
        applied: [`swap:${UENO_REF}`],
        rejected: [],
        notes: [],
        plan: REPLAN_AFTER_SWAP,
      }),
    });
  });

  await page.addInitScript(() => {
    localStorage.setItem('tg_session', JSON.stringify({ user: null, guest: true, savedAt: Date.now() }));
  });
  await page.goto('/');
  await page.getByTestId('chat-input').fill('4 days in tokyo, two spots one day');
  await page.getByRole('button', { name: 'Send' }).click();
  await expect(page.getByTestId('itinerary')).toContainText('Senso-ji Temple', { timeout: 8000 });
  await expect(page.getByTestId('itinerary')).toContainText('Tokyo Tower');

  // Drag the suggestion card straight onto the Tokyo Tower ITEM ROW — item-activity-1
  // (Senso-ji is item-activity-0, Tokyo Tower is item-activity-1) — never a drop-zone.
  const tokyoTowerRow = page.getByTestId('item-activity-1');
  await expect(tokyoTowerRow).toBeVisible();
  await page.getByTestId(`sug-item-${UENO_REF}`).dragTo(tokyoTowerRow);

  expect(capturedBody).not.toBeNull();
  const body = JSON.parse(capturedBody!);
  expect(body.ops).toHaveLength(2);
  expect(body.ops[0]).toMatchObject({
    op: 'remove_place', leg_index: 0, day_index: 0, position: 1, attraction_ref: 'Tokyo Tower',
  });
  expect(body.ops[1]).toMatchObject({
    op: 'add_place', leg_index: 0, day_index: 0, position: 1, attraction_ref: UENO_REF, from: 'unscheduled',
  });

  // Server envelope is truth: Tokyo Tower is gone, Ueno Park took its place.
  await expect(page.getByTestId('itinerary')).toContainText('Ueno Park', { timeout: 5000 });
  await expect(page.getByTestId('itinerary')).not.toContainText('Tokyo Tower');
});

// ── Task 3: real-time transport-gap recompute after a drag-add ───────────────
//
// The transport chips (`.transport-gap` / `transport-chip-{ii}`) are pure
// presentation of whatever `day.item_transport_gaps` the server returns —
// Itinerary.svelte never recomputes distances client-side (see the "Pure
// presentation" comment above the chip markup). This test locks that after a
// drag-add the chips reflect the NEW post-add /replan response (2 gaps for the
// new 3-item order), not the stale 1-gap array the day had before the edit —
// i.e. runOps()/onReplanned() really does replace `plans` wholesale from the
// server envelope rather than leaving any client-side item_transport_gaps state
// behind.
test('drag-add recomputes transport chips: new gaps shown, stale pre-add gap gone', async ({ page }) => {
  await page.route('**/negotiate_text', (route) =>
    route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(PLAN_TWO_ATTRACTIONS) }));
  await page.route('**/tile.openstreetmap.org/**', (route) => route.abort());
  await page.route('**/replan', (route) =>
    route.fulfill({
      status: 200, contentType: 'application/json',
      body: JSON.stringify({
        outcome: 'plan_ready',
        idempotency_key: 'trip-drag-mid-1',
        applied: [`add:leg0.day0.pos1:${UENO_REF}`],
        rejected: [],
        notes: [],
        plan: REPLAN_AFTER_MID_ADD,
      }),
    }));

  await page.addInitScript(() => {
    localStorage.setItem('tg_session', JSON.stringify({ user: null, guest: true, savedAt: Date.now() }));
  });
  await page.goto('/');
  await page.getByTestId('chat-input').fill('4 days in tokyo, two spots one day');
  await page.getByRole('button', { name: 'Send' }).click();
  await expect(page.getByTestId('itinerary')).toContainText('Senso-ji Temple', { timeout: 8000 });

  // Before the drag: exactly one chip, the PRE-add taxi/30min gap.
  await expect(page.getByTestId('transport-chip-0')).toContainText('30');
  await expect(page.getByTestId('transport-chip-0')).toContainText('taxi');
  await expect(page.getByTestId('transport-chip-1')).not.toBeVisible();

  await page.getByTestId(`sug-item-${UENO_REF}`).dragTo(page.getByTestId('drop-0-0-1'));
  await expect(page.getByTestId('itinerary')).toContainText('Ueno Park', { timeout: 5000 });

  // After the drag: TWO chips for the new 3-item order, matching the server's
  // recomputed values — the stale 1-gap/"30 taxi" value must be gone entirely.
  await expect(page.getByTestId('transport-chip-0')).toContainText('10');
  await expect(page.getByTestId('transport-chip-0')).toContainText('walk');
  await expect(page.getByTestId('transport-chip-1')).toBeVisible();
  await expect(page.getByTestId('transport-chip-1')).toContainText('20');
  await expect(page.getByTestId('transport-chip-1')).toContainText('metro');
});

test('sug-chip in day view fires add_place on click', async ({ page }) => {
  let capturedBody: string | null = null;
  await page.route('**/replan', (route) => {
    capturedBody = route.request().postData();
    route.fulfill({
      status: 200, contentType: 'application/json',
      body: JSON.stringify({
        outcome: 'plan_ready',
        idempotency_key: 'trip-drag-1',
        applied: [`add:leg0.day0:${UENO_REF}`],
        rejected: [],
        notes: [],
        plan: REPLAN_AFTER_ADD,
      }),
    });
  });

  await setupPlanWithSuggestions(page);

  // Mini suggestion chip is visible inline within day 0 of leg 0
  const chip = page.getByTestId(`sug-chip-0-0-${UENO_REF}`);
  await expect(chip).toBeVisible();

  // Clicking the chip triggers addFromUnscheduled for that day
  await chip.click();

  // "Updated ✓" flash confirms the replan ran
  await expect(page.getByTestId('replan-flash')).toContainText('Updated', { timeout: 5000 });

  // Verify correct add_place op was sent
  expect(capturedBody).not.toBeNull();
  const body = JSON.parse(capturedBody!);
  expect(body.ops[0].op).toBe('add_place');
  expect(body.ops[0].attraction_ref).toBe(UENO_REF);

  // Ueno Park now in the itinerary
  await expect(page.getByTestId('itinerary')).toContainText('Ueno Park', { timeout: 5000 });
});
