import { test, expect } from '@playwright/test';

// place-photos.spec.ts — Itinerary's per-item/leg photo thumbnail (Part B, "Item 3":
// mockups/image-placement-draft2.html).
//
// HISTORY: this file used to test PlacePhotos.svelte's own click-to-reveal "📷 Photo"
// toggle, embedded once per leg-header/item-row inside Itinerary.svelte. That embedding
// was REMOVED (itinerary-scale redesign, bug 2 of the bundled fix): Itinerary.svelte no
// longer mounts <PlacePhotos> at all. It now renders its OWN always-visible thumbnail
// button (.leg-thumb / .item-thumb, defined directly in Itinerary.svelte) which eagerly
// fetches POST /place_card on mount (no click needed to reveal) and opens a shared
// lightbox (.lb-overlay/.lb-img, mirroring PlacePhotos.svelte's own lightbox markup/
// naming but implemented separately) on tap. PlacePhotos.svelte's own toggle path/gallery
// is unchanged; a new banner variant was added and is used by Preview.svelte's leg-hero
// banner (variant="banner") — that usage is covered by preview.spec.ts, not here.
//
// This file's tests were rewritten to protect the same real user-facing guarantees
// (affordance presence, tap-to-view, honest "no photo" vs honest "loading" states,
// graceful failure, no runaway/duplicate fetching) through the NEW .item-thumb/.leg-thumb
// UI. One capability genuinely regressed in the process — see the flagged test below.
//
// The Google Places key is NEVER sent to the browser (server-proxied). All tests mock
// /place_card.

const PLAN_WITH_ACTIVITIES = {
  outcome: 'plan_ready',
  payment_status: 'held',
  booking_ref: null,
  idempotency_key: 'trip-ph-1',
  package_total_with_fees_cents: 150000,
  total_budget_cents: 400000,
  wallet: { balance_cents: 500000, held_cents: 150000, debited: false },
  legs: [{
    leg_id: 'leg-0',
    city: 'Sapporo',
    checkin: '2026-10-01',
    checkout: '2026-10-04',
    lat: 43.06,
    lng: 141.35,
    hotel_title: 'Test Hotel Sapporo',
  }],
  day_plans: [{
    leg_id: 'leg-0',
    city: 'Sapporo',
    num_days: 3,
    days: [{
      day_index: 0,
      bad_weather: false,
      attractions: [{
        name: 'Mt. Moiwa Trail',
        category: 'tourism=viewpoint',
        weather_exposure: 'outdoor',
        lat: 43.04,
        lon: 141.32,
        fee: null,
      }],
      meals: {
        lunch: { name: 'Ramen Counter', category: 'amenity=restaurant', cuisine: 'ramen' },
      },
      intracity_hops: [],
    }],
  }],
  risk_signals: { per_leg: [] },
};

const PLACE_CARD_OK = {
  status: 'ok',
  source: 'live:google_places',
  place: {
    display_name: 'Mt. Moiwa Trail',
    rating: 4.5,
    user_rating_count: 1234,
    open_now: true,
    photos: ['/place_photo?ref=abc123def456abc123def456abc12345'],
    reviews: [],
  },
};

const PLACE_CARD_UNAVAILABLE = {
  status: 'unavailable',
  source: 'live:google_places',
  reason: 'Places not enabled (PLACES_ENABLED=1 and GOOGLE_PLACES_KEY required)',
};

async function setupAndPlan(page, placeCardRoute?: (route: any) => void) {
  await page.addInitScript(() => {
    localStorage.setItem('tg_session', JSON.stringify({ user: null, guest: true, savedAt: Date.now() }));
  });
  await page.route('**/negotiate_text', (route) =>
    route.fulfill({ status: 200, contentType: 'application/json',
      body: JSON.stringify(PLAN_WITH_ACTIVITIES) }));
  await page.route('**/tile.openstreetmap.org/**', (route) => route.abort());
  await page.route('**/emergencies', (route) =>
    route.fulfill({ status: 200, contentType: 'application/json',
      body: JSON.stringify({ status: 'ok', countries: [] }) }));
  if (placeCardRoute) {
    await page.route('**/place_card', placeCardRoute);
  }
  await page.route('**/place_photo**', (route) =>
    route.fulfill({ status: 200, contentType: 'image/jpeg', body: Buffer.alloc(100) }));

  await page.goto('/');
  await page.getByTestId('chat-input').fill('3 days in sapporo');
  await page.getByRole('button', { name: 'Send' }).click();
  await expect(page.getByTestId('itinerary')).toBeVisible({ timeout: 8000 });
}

/** The Mt. Moiwa Trail activity's always-visible thumbnail button (day 0, item 0 — this
 *  fixture's leg has no day-tabs, so day 0's panel is on-screen from the start). */
function moiwaThumb(page) {
  return page.getByTestId('day-panel-0-0').getByTestId('item-activity-0').locator('.item-thumb');
}

test('item thumbnail affordance present per activity; no lightbox open by default', async ({ page }) => {
  await setupAndPlan(page);

  // The always-visible thumbnail button for the Mt. Moiwa Trail activity item is on-screen
  // with no click needed to reveal it (unlike the old strip's 📷 toggle).
  await expect(moiwaThumb(page)).toBeVisible({ timeout: 5000 });

  // But nothing auto-opens — the lightbox stays closed until the user actually taps it.
  await expect(page.locator('.lb-overlay')).toHaveCount(0);
});

test('item thumbnail opens lightbox on click and renders the image fetched from /place_card', async ({ page }) => {
  await setupAndPlan(page, (route) =>
    route.fulfill({ status: 200, contentType: 'application/json',
      body: JSON.stringify(PLACE_CARD_OK) }));

  const thumb = moiwaThumb(page);
  // Eager mount-time fetch resolves without any click — the thumb ends up in its
  // "has a real photo" state (honest "View photos of…" label, not the icon-tile fallback).
  await expect(thumb).toHaveAttribute('aria-label', 'View photos of Mt. Moiwa Trail', { timeout: 5000 });

  await thumb.click();

  // Lightbox opens showing the fetched photo. (Asserting the `src` attribute rather than
  // toBeVisible(): the mocked /place_photo route below returns dummy zero-byte image data,
  // so the <img> decodes to a 0×0 intrinsic size — .lb-img has no explicit CSS box, unlike
  // .ph-img/.item-thumb img, which always do — an unrelated fixture quirk, not a real
  // visibility bug.)
  await expect(page.locator('.lb-overlay')).toBeVisible({ timeout: 5000 });
  const lbImg = page.locator('.lb-img');
  await expect(lbImg).toHaveAttribute('src', /place_photo\?ref=abc123def456abc123def456abc12345/);

  // Security: no googleapis.com URL in page content
  const content = await page.content();
  expect(content).not.toContain('googleapis.com');
  expect(content).not.toContain('maps.google.com');
});

// CAPABILITY REGRESSION, flagged rather than silently dropped or faked into a pass: the old
// PlacePhotos toggle strip rendered ⭐ rating / review-count / open-now meta beneath the
// expanded photo (PlacePhotos.svelte's .ph-meta block — still alive for Preview's banner
// variant). The new item-thumb + shared lightbox is image-only: Itinerary.svelte's
// ThumbState type (`{ photos, fetched, loading }`) never even stores rating/open_now, so
// there is nothing left to render. This is a real, intentional-but-unaddressed loss of
// per-item detail, not a test bug.
test('item thumbnail lightbox shows only the photo — rating/open-now meta no longer surfaces per item (known regression)', async ({ page }) => {
  await setupAndPlan(page, (route) =>
    route.fulfill({ status: 200, contentType: 'application/json',
      body: JSON.stringify(PLACE_CARD_OK) }));

  const thumb = moiwaThumb(page);
  await expect(thumb).toHaveAttribute('aria-label', 'View photos of Mt. Moiwa Trail', { timeout: 5000 });
  await thumb.click();
  await expect(page.locator('.lb-overlay')).toBeVisible({ timeout: 5000 });

  // The fetched rating (4.5) / review count (1,234) / open-now status render NOWHERE in
  // the itinerary surface — PlacePhotos.svelte's .ph-rating/.ph-count/.ph-status classes
  // simply don't exist on this page at all now.
  await expect(page.locator('.ph-rating')).toHaveCount(0);
  await expect(page.locator('.ph-status')).toHaveCount(0);
  await expect(page.getByText('4.5', { exact: false })).toHaveCount(0);
});

test('item thumbnail shows the "No photo available" fallback when /place_card returns unavailable — no error state', async ({ page }) => {
  await setupAndPlan(page, (route) =>
    route.fulfill({ status: 200, contentType: 'application/json',
      body: JSON.stringify(PLACE_CARD_UNAVAILABLE) }));

  const thumb = moiwaThumb(page);
  // Honest "confirmed absent" label — only once the fetch has actually resolved.
  await expect(thumb).toHaveAttribute('aria-label', 'No photo available for Mt. Moiwa Trail', { timeout: 5000 });
  await expect(thumb).toHaveClass(/icon-tile/);
  await expect(thumb).toBeDisabled();
  await expect(thumb.locator('img')).toHaveCount(0);

  // No error state thrown at the user
  await expect(page.locator('[role="alert"]')).toHaveCount(0);
});

test('item thumbnail lightbox can be closed and the same thumbnail reopens it (repeat-tap is idempotent, not a collapsing toggle)', async ({ page }) => {
  await setupAndPlan(page, (route) =>
    route.fulfill({ status: 200, contentType: 'application/json',
      body: JSON.stringify(PLACE_CARD_OK) }));

  const thumb = moiwaThumb(page);
  await expect(thumb).toHaveAttribute('aria-label', 'View photos of Mt. Moiwa Trail', { timeout: 5000 });

  await thumb.click();
  await expect(page.locator('.lb-overlay')).toBeVisible({ timeout: 5000 });

  // Close via the lightbox's own ✕ button — unlike the old strip, the thumbnail itself is
  // not a toggle; it always means "open the lightbox," never "collapse in place."
  await page.getByRole('button', { name: 'Close photo' }).click();
  await expect(page.locator('.lb-overlay')).toHaveCount(0);

  // Tapping the SAME thumbnail again reopens the lightbox with the cached photo — no
  // re-fetch needed, and the affordance isn't left in a dead/unusable state after closing.
  // (Asserting `src` rather than toBeVisible() — see the note in the previous test about
  // this fixture's dummy zero-byte /place_photo response.)
  await thumb.click();
  await expect(page.locator('.lb-overlay')).toBeVisible({ timeout: 5000 });
  await expect(page.locator('.lb-img')).toHaveAttribute('src', /place_photo\?ref=abc123def456abc123def456abc12345/);
});

test('hotel_title renders a leg-hero thumbnail affordance', async ({ page }) => {
  await setupAndPlan(page);

  // The leg has hotel_title: 'Test Hotel Sapporo' → renders the always-visible leg-hero
  // thumbnail button (whatever its fetch state: loading, real photo, or confirmed-absent —
  // the point here is just that the affordance itself renders for a leg with a hotel).
  const legThumb = page.getByTestId('leg-h-0').locator('.leg-thumb');
  await expect(legThumb).toBeVisible({ timeout: 5000 });
  await expect(legThumb).toHaveAttribute('aria-label', /Test Hotel Sapporo/);
});

// NOTE (itinerary-scale + Item 3 thumbnails redesign): the OLD PlacePhotos toggle used to
// only fetch /place_card once clicked; that lazy-vs-eager distinction no longer applies
// since the toggle itself is gone — Itinerary.svelte's own .item-thumb IS the only thing
// that ever calls /place_card for this item now (see itinerary-scale.spec.ts for the
// day-tab-scoped fetch/cache invariants; the two tests below are scoped narrowly to what's
// specific to THIS single-day, no-tabs fixture: the eager fetch is a one-shot, and repeat
// taps on an already-resolved thumb never re-fetch).
test('item thumbnail eager mount-time fetch is a single one-shot call — it does not keep polling on its own', async ({ page }) => {
  const calls: string[] = [];
  await setupAndPlan(page, (route) => {
    const body = JSON.parse(route.request().postData() || '{}');
    calls.push(body.name);
    route.fulfill({ status: 200, contentType: 'application/json',
      body: JSON.stringify(PLACE_CARD_OK) });
  });

  await expect(page.getByTestId('itinerary')).toBeVisible({ timeout: 8000 });
  await page.waitForTimeout(500);

  // Exactly one eager mount-time call for this item's thumbnail.
  expect(calls.filter((n) => n === 'Mt. Moiwa Trail').length).toBe(1);

  // That count does not keep climbing over time on its own — a single one-shot mount
  // fetch, not a poll/retry loop.
  const callsAfterMount = calls.filter((n) => n === 'Mt. Moiwa Trail').length;
  await page.waitForTimeout(300);
  expect(calls.filter((n) => n === 'Mt. Moiwa Trail').length).toBe(callsAfterMount);
});

test('item thumbnail taps (open/close/reopen the lightbox) never trigger additional /place_card calls beyond the single eager fetch', async ({ page }) => {
  const calls: string[] = [];
  await setupAndPlan(page, (route) => {
    const body = JSON.parse(route.request().postData() || '{}');
    calls.push(body.name);
    route.fulfill({ status: 200, contentType: 'application/json',
      body: JSON.stringify(PLACE_CARD_OK) });
  });

  const thumb = moiwaThumb(page);
  await expect(thumb).toHaveAttribute('aria-label', 'View photos of Mt. Moiwa Trail', { timeout: 5000 });
  const callsAfterMount = calls.filter((n) => n === 'Mt. Moiwa Trail').length; // the single eager fetch
  expect(callsAfterMount).toBe(1);

  // Open
  await thumb.click();
  await expect(page.locator('.lb-overlay')).toBeVisible({ timeout: 5000 });
  expect(calls.filter((n) => n === 'Mt. Moiwa Trail').length).toBe(callsAfterMount);

  // Close
  await page.getByRole('button', { name: 'Close photo' }).click();
  await expect(page.locator('.lb-overlay')).toHaveCount(0);

  // Reopen — thumbCache's `fetched` guard prevents any further call.
  await thumb.click();
  await expect(page.locator('.lb-overlay')).toBeVisible({ timeout: 5000 });
  expect(calls.filter((n) => n === 'Mt. Moiwa Trail').length).toBe(callsAfterMount);
});

// ── #168 B5 spillover (item 4): item-thumb lightbox vs. the floating assistant
// trigger, on mobile. .lb-overlay is position:fixed/inset:0/z-index 9999 — the SAME
// class of element as Map.svelte's mobile pin-sheet, which fix/assistant-bubble-
// overlap already found and fixed covering ChatPane's floating trigger (z-index 300).
// Confirmed via document.elementFromPoint + an actual blocked Playwright click that
// this lightbox had the identical collision and was never wired into hideMobileBubble.
// App.svelte now ORs a small `itineraryLightboxOpen` store (mapStore.ts) into
// hideMobileBubble, mirroring the mechanism already used for Preview's .budget-bar
// (showPreview) and Map.svelte's pin-sheet (placeSheetOpen). ──
test.describe('mobile: item-thumb lightbox does not cover the assistant trigger (≤768px)', () => {
  test.use({ viewport: { width: 390, height: 844 } });

  async function setupAndPlanMobile(page, placeCardRoute?: (route: any) => void) {
    await page.addInitScript(() => {
      localStorage.setItem('tg_session', JSON.stringify({ user: null, guest: true, savedAt: Date.now() }));
    });
    await page.route('**/negotiate_text', (route) =>
      route.fulfill({ status: 200, contentType: 'application/json',
        body: JSON.stringify(PLAN_WITH_ACTIVITIES) }));
    await page.route('**/tile.openstreetmap.org/**', (route) => route.abort());
    await page.route('**/emergencies', (route) =>
      route.fulfill({ status: 200, contentType: 'application/json',
        body: JSON.stringify({ status: 'ok', countries: [] }) }));
    if (placeCardRoute) await page.route('**/place_card', placeCardRoute);
    await page.route('**/place_photo**', (route) =>
      route.fulfill({ status: 200, contentType: 'image/jpeg', body: Buffer.alloc(100) }));

    await page.goto('/');
    await page.locator('.mob-bubble').click();
    await page.locator('.mob-sheet-box textarea').fill('3 days in sapporo');
    await page.locator('.mob-sheet-box .send').click();
    await expect(page.getByTestId('itinerary')).toBeVisible({ timeout: 8000 });
  }

  test('assistant trigger is hidden while the lightbox is open, and restored when it closes', async ({ page }) => {
    await setupAndPlanMobile(page, (route) =>
      route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(PLACE_CARD_OK) }));

    const trigger = page.getByTestId('assistant-trigger');
    await expect(trigger).toBeVisible();

    const thumb = moiwaThumb(page);
    await expect(thumb).toBeVisible({ timeout: 5000 });
    await thumb.click();
    await expect(page.locator('.lb-overlay')).toBeVisible({ timeout: 5000 });

    // Genuinely hidden (display:none via .mob-hide), not merely covered by a higher
    // z-index element -- see place-card.spec.ts's identical B5 regression test for the
    // pin-sheet, and the elementFromPoint/click-intercepted proof in the commit message.
    await expect(trigger).toBeHidden();

    await page.locator('.lb-close').click();
    await expect(page.locator('.lb-overlay')).toHaveCount(0);
    await expect(trigger).toBeVisible();
  });

  // NOTE: there is deliberately no "leave the plan WITHOUT closing the lightbox first"
  // regression test here (unlike place-card.spec.ts's equivalent for Map.svelte's
  // pin-sheet). Unlike the pin-sheet -- a partial bottom sheet that leaves the rest of
  // the page reachable, so a user genuinely CAN tap the RightRail "Budget" tab without
  // closing it -- .lb-overlay is a full-screen modal (position:fixed, inset:0) that
  // captures every click, including "Start new trip"; there is no UI-reachable path
  // that unmounts Itinerary.svelte while the lightbox is still open. The onDestroy
  // reset (Itinerary.svelte) is still added defensively, matching the established
  // pattern, and is locked directly at the component level in
  // Itinerary.lightbox-store.test.ts (unmount → store resets) rather than via an
  // unreachable E2E path.
});
