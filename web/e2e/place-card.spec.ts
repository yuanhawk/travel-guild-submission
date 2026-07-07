import { test, expect } from '@playwright/test';

// place-card.spec.ts — Map popup: MapLibre marker click → /place_card → place-card overlay.
//
// The Map tab renders inside RightRail. After a plan renders, clicking the Map tab mounts
// MapLibre. When a non-lodging pin (.tg-pin) is clicked, Map.svelte calls placeCard() →
// POST /place_card. The result is rendered in the [data-testid="place-card"] overlay.
//
// Security: the FE never holds the Google Places key — /place_card and /place_photo are
// server-proxied. We assert no googleapis.com URL appears in the page.
//
// NOTE: MapLibre needs WebGL (available via SwiftShader in Playwright's headless Chromium).
// Tiles are aborted (offline). The 'load' event fires from the JS style spec before tiles.
// We wait up to 10s for .tg-pin markers to appear in the DOM after the map tab is clicked.

const PLAN_WITH_PINS = {
  outcome: 'plan_ready',
  payment_status: 'held',
  booking_ref: null,
  idempotency_key: 'trip-pc-1',
  package_total_with_fees_cents: 100000,
  total_budget_cents: 400000,
  wallet: { balance_cents: 500000, held_cents: 100000, debited: false },
  legs: [{ leg_id: 'leg-0', city: 'Tokyo', checkin: '2026-10-01', checkout: '2026-10-04', lat: 35.68, lng: 139.69 }],
  day_plans: [{
    leg_id: 'leg-0', city: 'Tokyo', num_days: 3,
    days: [{
      day_index: 0, bad_weather: false,
      // Attraction with finite lat/lon → becomes a .tg-pin (category=attraction)
      attractions: [{
        name: 'Senso-ji', category: 'tourism=temple',
        lat: 35.714, lon: 139.796,
        fee: null, weather_exposure: 'outdoor',
      }],
      meals: { lunch: { name: 'Sushi Dai', category: 'amenity=restaurant', cuisine: 'sushi', lat: 35.666, lon: 139.769 } },
    }],
  }],
  risk_signals: { per_leg: [] },
};

async function planAndGoToMap(page, opts: { mobile?: boolean } = {}) {
  await page.addInitScript(() => {
    localStorage.setItem('tg_session', JSON.stringify({ user: null, guest: true, savedAt: Date.now() }));
  });
  await page.route('**/negotiate_text', (route) =>
    route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(PLAN_WITH_PINS) }));
  await page.route('**/tile.openstreetmap.org/**', (route) => route.abort());

  await page.goto('/');
  if (opts.mobile) {
    // ≤768px: the embedded desktop chat-input is display:none, replaced by the
    // floating 🤖 bubble + slide-up sheet — see preview.spec.ts's mobilePlan()
    // for the same pattern (mobile-chat.spec.ts is the source of truth).
    await page.locator('.mob-bubble').click();
    await page.locator('.mob-sheet-box textarea').fill('3 days in tokyo');
    await page.locator('.mob-sheet-box .send').click();
  } else {
    await page.getByTestId('chat-input').fill('3 days in tokyo');
    await page.getByRole('button', { name: 'Send' }).click();
  }
  await expect(page.getByTestId('itinerary')).toBeVisible({ timeout: 8000 });

  // Navigate to the Map tab in RightRail
  await page.getByTestId('right-rail').getByRole('button', { name: 'Map' }).click();
  await expect(page.getByTestId('tab-map')).toBeVisible();

  // Wait for MapLibre to initialise and render pins (requires WebGL + 'load' event)
  await page.waitForSelector('.tg-pin', { timeout: 10000 });
}

test('map pin click → /place_card ok → rating and review render', async ({ page }) => {
  // Mock /place_card to return a rich card
  // Real wire envelope (society/utils/places_card.py): fields live NESTED under
  // `place`, using the server's own names (display_name, reviews[].author) —
  // NOT the flat shape this mock used to send (see Finding #2, map-pin-bug sweep:
  // the FE/BE contract had silently drifted apart and every field rendered blank).
  await page.route('**/place_card', (route) =>
    route.fulfill({
      status: 200, contentType: 'application/json',
      body: JSON.stringify({
        status: 'ok',
        place: {
          display_name: 'Senso-ji',
          rating: 4.6,
          user_rating_count: 12500,
          open_now: true,
          reviews: [{
            author: 'Alice',
            text: 'Stunning temple — a must-visit in Tokyo.',
          }],
        },
      }),
    }));

  await planAndGoToMap(page);

  // Click the first attraction pin (not the lodging centroid)
  const pin = page.locator('.tg-pin').first();
  await pin.click();

  // place-card overlay mounts
  await expect(page.getByTestId('place-card')).toBeVisible({ timeout: 5000 });

  // Rating from /place_card renders (not fabricated by FE)
  const rating = page.getByTestId('place-card-rating');
  await expect(rating).toBeVisible();
  await expect(rating).toContainText('4.6');

  // Review text renders
  await expect(page.getByTestId('place-card-review')).toContainText('Stunning temple');

  // Security: no Google key or googleapis URL anywhere in the DOM
  const content = await page.content();
  expect(content).not.toContain('googleapis.com');
  expect(content).not.toContain('maps.google.com');
});

test('map pin click → /place_card unavailable → honest panel, no Google URL', async ({ page }) => {
  // Mock /place_card to return unavailable (key off / no Places match)
  await page.route('**/place_card', (route) =>
    route.fulfill({
      status: 200, contentType: 'application/json',
      body: JSON.stringify({ status: 'unavailable' }),
    }));

  await planAndGoToMap(page);

  await page.locator('.tg-pin').first().click();

  // Honest "unavailable" panel renders — NOT an error or missing element
  await expect(page.getByTestId('place-card-unavailable')).toBeVisible({ timeout: 5000 });
  await expect(page.getByTestId('place-card-unavailable')).toContainText('unavailable');

  // No rating, no review visible on unavailable
  await expect(page.getByTestId('place-card-rating')).toHaveCount(0);

  // Security: even on unavailable, no googleapis.com URL in page
  const content = await page.content();
  expect(content).not.toContain('googleapis.com');
});

// ─────────────────────────────────────────────────────────────────────────────
// #163 — Itinerary "📍 Locate" button: not just a pan. Itinerary.svelte's
// locateItem() now carries {name, city, category} on mapFocus (previously just
// lat/lng/label), so Map.svelte's mapFocus effect opens the SAME place-detail
// card a direct pin click opens (see Map.svelte's reactive mapFocus block +
// map.test.ts's "mapFocus opens the place card" unit suite, and
// src/lib/Itinerary.locate.test.ts for the mapFocus-payload unit assertion).
//
// This is the one test in the suite that drives the WHOLE real chain end to
// end — click the itinerary's locate button (never touching the Map tab or
// the MapLibre pin by hand) and prove the map's place-card/selectedPin state
// actually opens with the right content. A pan-only regression would leave
// [data-testid="place-card"] absent forever, so asserting its content is a
// genuine "selected/highlighted", not merely "panned-to", check.
// ─────────────────────────────────────────────────────────────────────────────

async function planOnItinerary(page) {
  await page.addInitScript(() => {
    localStorage.setItem('tg_session', JSON.stringify({ user: null, guest: true, savedAt: Date.now() }));
  });
  await page.route('**/negotiate_text', (route) =>
    route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(PLAN_WITH_PINS) }));
  await page.route('**/tile.openstreetmap.org/**', (route) => route.abort());

  await page.goto('/');
  await page.getByTestId('chat-input').fill('3 days in tokyo');
  await page.getByRole('button', { name: 'Send' }).click();
  await expect(page.getByTestId('itinerary')).toBeVisible({ timeout: 8000 });
}

test.describe('itinerary locate button (#163) — genuinely selects the map pin, not just a pan', () => {
  test('clicking 📍 on an attraction auto-switches to the Map tab AND opens the place card for that exact place', async ({ page }) => {
    await page.route('**/place_card', (route) =>
      route.fulfill({
        status: 200, contentType: 'application/json',
        body: JSON.stringify({
          status: 'ok',
          place: { display_name: 'Senso-ji', rating: 4.6, user_rating_count: 12500, open_now: true },
        }),
      }));

    await planOnItinerary(page);

    // Before locating: the Map tab/pane isn't even on screen — <Map> isn't
    // mounted (RightRail only mounts it under `{#if tab === 'map'}`), so there
    // is no pre-existing selectedPin this test could be accidentally riding on.
    await expect(page.getByTestId('tab-map')).toHaveCount(0);

    // The itinerary's own 📍 button, not the MapLibre pin.
    await page.getByRole('button', { name: 'Show Senso-ji on map' }).click();

    // mapFocus flips RightRail to the Map tab automatically (no manual tab click).
    await expect(page.getByTestId('tab-map')).toBeVisible({ timeout: 5000 });

    // The place card is the real proof of "selected", not "panned": selectedPin
    // (and the /place_card fetch it triggers) only ever gets set from a pin
    // click or from a mapFocus payload that carries name+city+category — a
    // flyTo-only pan produces no such DOM node at all.
    await expect(page.getByTestId('place-card')).toBeVisible({ timeout: 5000 });
    await expect(page.getByTestId('place-card')).toContainText('Senso-ji');
    await expect(page.getByTestId('place-card-rating')).toContainText('4.6');
  });

  test('clicking 📍 on a meal item selects the restaurant pin\'s card, not the attraction\'s', async ({ page }) => {
    await page.route('**/place_card', (route) => {
      const { place } = JSON.parse(route.request().postData() ?? '{}');
      const body = place === 'Sushi Dai'
        ? { status: 'ok', place: { display_name: 'Sushi Dai', rating: 4.2, user_rating_count: 890 } }
        : { status: 'ok', place: { display_name: 'Senso-ji', rating: 4.6, user_rating_count: 12500 } };
      route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(body) });
    });

    await planOnItinerary(page);
    await page.getByRole('button', { name: 'Show Sushi Dai on map' }).click();

    await expect(page.getByTestId('place-card')).toBeVisible({ timeout: 5000 });
    await expect(page.getByTestId('place-card')).toContainText('Sushi Dai');
    await expect(page.getByTestId('place-card-rating')).toContainText('4.2');
  });
});

// ─────────────────────────────────────────────────────────────────────────────
// Item thumbnail inline (itinerary items) — Itinerary.svelte's own always-visible
// .item-thumb button in the day-plan cards.
//
// HISTORY: these 4 tests (PH-01..PH-04) used to drive PlacePhotos.svelte's embedded
// click-to-reveal "📷 Photo" toggle + expandable gallery, which Itinerary.svelte no
// longer mounts at all (removed as part of the itinerary-scale redesign's bug-2 fix —
// see place-photos.spec.ts's file header for the full history). Rewritten below to
// drive the SAME real behaviors — affordance visible without a click, tap-to-view,
// close, and graceful "no photo" handling — through the new .item-thumb + shared
// .lb-overlay lightbox. PH-02 documents a genuine capability regression (no more
// "show more" multi-photo gallery) rather than faking a pass for a feature that's gone.
// ─────────────────────────────────────────────────────────────────────────────

const JPEG_1X1 = Buffer.from('FFD8FFE000104A46494600010100000100010000FFDB004300080606070605080707070909080A0C140D0C0B0B0C1912130F141D1A1F1E1D1A1C1C20242E2720222C231C1C2837292C30313434341F27393D38323C2E333432FFDB0043010909090C0B0C180D0D1832211C213232323232323232323232323232323232323232323232323232323232323232323232323232323232323232323232323232FFC0001108000100010301220002110103110100FFC40014000100000000000000000000000000000000FFC40014100100000000000000000000000000000000FFDA000C03010002110311003F00BE000FFFD9', 'hex');

async function planAndOpenItinerary(page) {
  await page.addInitScript(() => {
    localStorage.setItem('tg_session', JSON.stringify({ user: null, guest: true, savedAt: Date.now() }));
  });
  await page.route(/tile|openstreetmap/, (r) => r.abort());
  await page.route('**/emergencies', (r) =>
    r.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ status: 'ok', countries: [] }) }));
  await page.route('**/negotiate_text', async (route) => {
    const body = JSON.parse(route.request().postData() ?? '{}');
    if (body.stream) {
      await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({}) });
    } else {
      await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(PLAN_WITH_PINS) });
    }
  });
  await page.goto('/');
  await page.getByTestId('chat-input').fill('3 days Tokyo');
  await page.getByRole('button', { name: 'Send' }).click();
  await expect(page.getByTestId('itinerary')).toBeVisible({ timeout: 10000 });
}

/** The Senso-ji attraction item's always-visible thumbnail button (day 0, item 0 — this
 *  fixture's leg has no day-tabs, so day 0's panel is on-screen from the start). */
function sensojiThumb(page) {
  return page.getByTestId('day-panel-0-0').getByTestId('item-activity-0').locator('.item-thumb');
}

test.describe('PlacePhotos inline (itinerary items)', () => {

  test('PH-01: item thumbnail visible on itinerary attraction without any click; click (once resolved) opens the lightbox with photos[0]', async ({ page }) => {
    await page.route('**/place_card', (route) =>
      route.fulfill({
        status: 200, contentType: 'application/json',
        body: JSON.stringify({
          status: 'ok',
          place: {
            photos: ['/place_photo?ref=t1', '/place_photo?ref=t2'],
            rating: 4.3, user_rating_count: 50, open_now: false,
          },
        }),
      }));
    await page.route('**/place_photo', (route) =>
      route.fulfill({ status: 200, contentType: 'image/jpeg', body: JPEG_1X1 }));

    await planAndOpenItinerary(page);

    // Thumbnail is visible without needing any click to reveal it, and the eager
    // mount-time fetch resolves to a real photo (photos[0]).
    const thumb = sensojiThumb(page);
    await expect(thumb).toHaveAttribute('aria-label', 'View photos of Senso-ji', { timeout: 5000 });
    await expect(thumb.locator('img')).toHaveAttribute('src', /place_photo\?ref=t1/);

    // Click → lightbox opens showing that same first photo.
    await thumb.click();
    await expect(page.locator('.lb-overlay')).toBeVisible({ timeout: 4000 });
    await expect(page.locator('.lb-img')).toHaveAttribute('src', /place_photo\?ref=t1/);
  });

  // CAPABILITY REGRESSION, flagged rather than faked into a pass: the old strip's
  // "Show N more photos" button (PlacePhotos.svelte's .ph-more, slicing up to 5 fetched
  // photos) is gone from this surface. Itinerary.svelte's lightbox always opens exactly
  // `thumb.photos[0]` (see the leg-thumb/item-thumb button handlers) with no index state
  // and no gallery affordance — even when /place_card returns multiple photos, only the
  // first is ever reachable from the itinerary list.
  test('PH-02: lightbox shows only the first photo — no "show more" gallery expansion in the new UI (known regression)', async ({ page }) => {
    await page.route('**/place_card', (route) =>
      route.fulfill({
        status: 200, contentType: 'application/json',
        body: JSON.stringify({
          status: 'ok',
          place: {
            photos: ['/place_photo?ref=t1', '/place_photo?ref=t2'],
            rating: 4.3, user_rating_count: 50, open_now: false,
          },
        }),
      }));
    await page.route('**/place_photo', (route) =>
      route.fulfill({ status: 200, contentType: 'image/jpeg', body: JPEG_1X1 }));

    await planAndOpenItinerary(page);

    const thumb = sensojiThumb(page);
    await expect(thumb).toHaveAttribute('aria-label', 'View photos of Senso-ji', { timeout: 5000 });
    await thumb.click();
    await expect(page.locator('.lb-overlay')).toBeVisible({ timeout: 4000 });

    // Only the first photo is shown, and there is no affordance anywhere to reach the
    // second one — no .ph-more, no second .lb-img, exactly one photo element total.
    await expect(page.locator('.lb-img')).toHaveAttribute('src', /place_photo\?ref=t1/);
    await expect(page.locator('.lb-img')).toHaveCount(1);
    await expect(page.locator('.ph-more')).toHaveCount(0);

    // Re-clicking the thumbnail (the only remaining interaction) still shows the same
    // first photo — there's no way to cycle to ref=t2 from this surface at all.
    await page.getByRole('button', { name: 'Close photo' }).click();
    await thumb.click();
    await expect(page.locator('.lb-img')).toHaveAttribute('src', /place_photo\?ref=t1/);
  });

  test('PH-03: item thumbnail opens the lightbox directly on click (no intermediate expand-strip step); ✕ closes it', async ({ page }) => {
    await page.route('**/place_card', (route) =>
      route.fulfill({
        status: 200, contentType: 'application/json',
        body: JSON.stringify({
          status: 'ok',
          place: { photos: ['/place_photo?ref=t1'], rating: 4.0, user_rating_count: 10, open_now: null },
        }),
      }));
    await page.route('**/place_photo', (route) =>
      route.fulfill({ status: 200, contentType: 'image/jpeg', body: JPEG_1X1 }));

    await planAndOpenItinerary(page);

    const thumb = sensojiThumb(page);
    await expect(thumb).toHaveAttribute('aria-label', 'View photos of Senso-ji', { timeout: 5000 });

    // A SINGLE click on the thumbnail is now the whole interaction (old flow needed a
    // 📷-button click to expand the strip, then a second click on the photo itself).
    await thumb.click();
    await expect(page.locator('.lb-overlay')).toBeVisible({ timeout: 3000 });

    // Close via ✕
    await page.getByRole('button', { name: 'Close photo' }).click();
    await expect(page.locator('.lb-overlay')).toHaveCount(0);
  });

  test('PH-04: unavailable response → item thumbnail shows the "No photo available" fallback gracefully', async ({ page }) => {
    await page.route('**/place_card', (route) =>
      route.fulfill({
        status: 200, contentType: 'application/json',
        body: JSON.stringify({ status: 'unavailable' }),
      }));

    await planAndOpenItinerary(page);

    const thumb = sensojiThumb(page);
    await expect(thumb).toHaveAttribute('aria-label', 'No photo available for Senso-ji', { timeout: 5000 });
    await expect(thumb).toHaveClass(/icon-tile/);
    await expect(thumb).toBeDisabled();
    await expect(thumb.locator('img')).toHaveCount(0);

    // Security: no googleapis.com URL anywhere in page
    const content = await page.content();
    expect(content).not.toContain('googleapis.com');
  });

});

// ─────────────────────────────────────────────────────────────────────────────
// Mobile bottom-sheet pin-detail (image-placement #2) — Map.svelte's .pin-sheet.
//
// At ≤768px the desktop floating .tg-card is CSS-hidden (display:none, still
// mounted in the DOM) and a fixed slide-up .pin-sheet takes over instead. Both
// containers render the SAME selectedPin/cardState via the shared
// <PinDetailContent> component — desktop uses testid prefix "place-card-*"
// (unchanged), mobile uses "place-sheet-*" (new). Lodging pins collapse to the
// same text-only "Approximate location" state in both containers (no live
// fetch), same as before this refactor.
//
// The sheet is toggled via `transform: translateY(...)`, not display:none, so
// toBeVisible()/toBeHidden() alone can't tell open from collapsed (an
// off-screen-but-mounted element still reports visible=true). Assert the real
// on-screen position via boundingBox() instead, same technique as
// preview.spec.ts's budget-sheet tests.
// ─────────────────────────────────────────────────────────────────────────────

async function sheetOnScreen(page): Promise<boolean> {
  const box = await page.locator('.pin-sheet').boundingBox();
  const vp = page.viewportSize();
  return !!box && !!vp && box.y < vp.height - 1;   // -1px tolerance for rounding
}

test.describe('mobile bottom-sheet pin-detail (≤768px)', () => {
  test.use({ viewport: { width: 390, height: 844 } });   // iPhone 14

  test('tapping an attraction pin opens the mobile sheet (not the desktop card) with the same content', async ({ page }) => {
    await page.route('**/place_card', (route) =>
      route.fulfill({
        status: 200, contentType: 'application/json',
        body: JSON.stringify({
          status: 'ok',
          place: {
            display_name: 'Senso-ji',
            rating: 4.6,
            user_rating_count: 12500,
            open_now: true,
            reviews: [{
              author: 'Alice',
              text: 'Stunning temple — a must-visit in Tokyo.',
            }],
          },
        }),
      }));

    await planAndGoToMap(page, { mobile: true });
    await page.locator('.tg-pin[title="Senso-ji"]').click();

    // The desktop floating card DOM node still mounts (selectedPin drives both
    // containers) but is CSS-hidden at this viewport — genuinely not what the
    // user sees.
    await expect(page.getByTestId('place-card')).toBeHidden();

    // The mobile sheet is the one actually on-screen.
    const sheet = page.getByTestId('place-sheet');
    await expect(sheet).toHaveClass(/ps-open/);
    await expect.poll(() => sheetOnScreen(page)).toBe(true);
    await expect(sheet.locator('.ps-ttl')).toContainText('Senso-ji');

    // Same content the desktop card would show (see the 'rating and review
    // render' test above), via the shared PinDetailContent — just under the
    // place-sheet-* testid namespace instead of place-card-*.
    await expect(page.getByTestId('place-sheet-rating')).toContainText('4.6');
    await expect(page.getByTestId('place-sheet-review')).toContainText('Stunning temple');

    // Security: no Google key/URL leak in the mobile path either.
    const content = await page.content();
    expect(content).not.toContain('googleapis.com');
  });

  test('tapping a restaurant pin also opens the mobile sheet with live content', async ({ page }) => {
    await page.route('**/place_card', (route) =>
      route.fulfill({
        status: 200, contentType: 'application/json',
        body: JSON.stringify({
          status: 'ok',
          place: { display_name: 'Sushi Dai', rating: 4.2, user_rating_count: 890, open_now: false },
        }),
      }));

    await planAndGoToMap(page, { mobile: true });
    await page.locator('.tg-pin[title="Sushi Dai"]').click();

    await expect(page.getByTestId('place-card')).toBeHidden();
    await expect.poll(() => sheetOnScreen(page)).toBe(true);
    await expect(page.getByTestId('place-sheet').locator('.ps-ttl')).toContainText('Sushi Dai');
    await expect(page.getByTestId('place-sheet-rating')).toContainText('4.2');
  });

  test('tapping a lodging pin still shows the unchanged text-only approximate-location state', async ({ page }) => {
    let placeCardCalls = 0;
    await page.route('**/place_card', (route) => {
      placeCardCalls++;
      route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ status: 'ok', rating: 4.9 }) });
    });

    await planAndGoToMap(page, { mobile: true });
    // Reset the counter here: Itinerary.svelte's separate, pre-existing
    // "per-item photo thumbnail" feature (Part B Item 3) already eagerly calls
    // /place_card for the itinerary's own visible attraction/meal items (Senso-ji,
    // Sushi Dai) while planAndGoToMap() was still on the itinerary view, before
    // ever switching to the Map tab. That's unrelated to this test — only calls
    // made from clicking the MAP pin below are the signal.
    placeCardCalls = 0;

    // Lodging pin label is "<city> — approx (city centre)" (see planPins()).
    const lodgingPin = page.locator('.tg-pin[title*="approx"]');
    await expect(lodgingPin).toBeVisible();
    await lodgingPin.click();

    await expect.poll(() => sheetOnScreen(page)).toBe(true);
    await expect(page.getByTestId('place-sheet-approx')).toBeVisible();
    await expect(page.getByTestId('place-sheet-approx')).toContainText('Approximate location');

    // No live rating/review — lodging never had a live-card state, on mobile
    // either.
    await expect(page.getByTestId('place-sheet-rating')).toHaveCount(0);
    // Unchanged behavior: lodging pins never call /place_card (no specific
    // place to query — see Map.svelte's openCard()).
    expect(placeCardCalls).toBe(0);
  });

  test("the sheet's own close button collapses it back", async ({ page }) => {
    await page.route('**/place_card', (route) =>
      route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ status: 'ok', place: { rating: 4.6 } }) }));

    await planAndGoToMap(page, { mobile: true });
    await page.locator('.tg-pin[title="Senso-ji"]').click();
    await expect.poll(() => sheetOnScreen(page)).toBe(true);

    await page.getByTestId('place-sheet-close').click();

    await expect.poll(() => sheetOnScreen(page)).toBe(false);
  });

  // ── B5 regression: the sheet (.pin-sheet, position:fixed, bottom:0, z-index 315)
  // and ChatPane's floating assistant trigger (.bot-trigger, position:fixed,
  // bottom:16px, z-index 300) share the same fixed bottom-left corner of the
  // viewport. Before the fix, opening the sheet left the trigger mounted right
  // underneath it — fully covered, but still "visible" per naive DOM checks.
  // App.svelte now ORs Map.svelte's placeSheetOpen store into ChatPane's
  // hideMobileBubble prop (the same mechanism already used for Preview's
  // .budget-bar — see preview.spec.ts's mobile-viewport describe block). ──
  test('assistant trigger is hidden while the mobile pin-sheet is open, and restored when it closes', async ({ page }) => {
    await page.route('**/place_card', (route) =>
      route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ status: 'ok', place: { rating: 4.6 } }) }));

    await planAndGoToMap(page, { mobile: true });

    const trigger = page.getByTestId('assistant-trigger');
    await expect(trigger).toBeVisible();

    await page.locator('.tg-pin[title="Senso-ji"]').click();
    await expect.poll(() => sheetOnScreen(page)).toBe(true);

    // The trigger must be genuinely hidden (display:none via .mob-hide), not
    // merely covered — otherwise it's still in the tab/hit-test order underneath
    // an opaque sheet.
    await expect(trigger).toBeHidden();

    await page.getByTestId('place-sheet-close').click();
    await expect.poll(() => sheetOnScreen(page)).toBe(false);
    await expect(trigger).toBeVisible();
  });

  test('assistant trigger is restored after navigating away from the Map tab without closing the sheet', async ({ page }) => {
    await page.route('**/place_card', (route) =>
      route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ status: 'ok', place: { rating: 4.6 } }) }));

    await planAndGoToMap(page, { mobile: true });
    await page.locator('.tg-pin[title="Senso-ji"]').click();
    await expect.poll(() => sheetOnScreen(page)).toBe(true);

    const trigger = page.getByTestId('assistant-trigger');
    await expect(trigger).toBeHidden();

    // Switch tabs — RightRail's {#if tab === 'map'} unmounts Map.svelte (and its
    // sheet) WITHOUT ever calling closeCard(). The placeSheetOpen store must be
    // reset in onDestroy, or the trigger stays stuck hidden with nothing left
    // to explain why.
    await page.getByTestId('right-rail').getByRole('button', { name: 'Budget' }).click();
    await expect(trigger).toBeVisible();
  });
});

test.describe('desktop floating card is unchanged at a desktop viewport', () => {
  test.use({ viewport: { width: 1280, height: 800 } });

  test('desktop card renders as before; the mobile sheet stays entirely off (display:none)', async ({ page }) => {
    await page.route('**/place_card', (route) =>
      route.fulfill({
        status: 200, contentType: 'application/json',
        body: JSON.stringify({
          status: 'ok',
          place: { display_name: 'Senso-ji', rating: 4.6, user_rating_count: 12500, open_now: true },
        }),
      }));

    await planAndGoToMap(page);
    await page.locator('.tg-pin[title="Senso-ji"]').click();

    // Desktop floating card — unchanged behavior (same assertions as the
    // pre-existing 'rating and review render' test above).
    await expect(page.getByTestId('place-card')).toBeVisible();
    await expect(page.getByTestId('place-card-rating')).toContainText('4.6');

    // At >768px .pin-sheet is `display: none` outright (not just transformed
    // off-screen) — boundingBox() returns null for a non-rendered element,
    // proving the mobile container contributes nothing at desktop widths.
    const sheetBox = await page.locator('.pin-sheet').boundingBox();
    expect(sheetBox).toBeNull();
  });
});
