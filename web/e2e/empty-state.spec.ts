import { test, expect } from '@playwright/test';

// #118: Guide panel / empty-state hermetic E2E spec.
// Covers the pre-plan 2-column split layout (guide-panel + chat).
// All network calls are mocked; OSM tiles are aborted.

const BASE_PLAN = {
  outcome: 'plan_ready',
  payment_status: 'held',
  booking_ref: null,
  idempotency_key: 'trip-cov-1',
  package_total_with_fees_cents: 140000,
  total_budget_cents: 300000,
  wallet: { balance_cents: 500000, held_cents: 140000, debited: false },
  legs: [
    {
      leg_id: 'leg-0',
      city: 'Lisbon',
      checkin: '2026-10-01',
      checkout: '2026-10-05',
      lat: 38.72,
      lng: -9.14,
      hotel_title: 'Test Hotel Lisbon',
    },
  ],
  day_plans: [
    {
      leg_id: 'leg-0',
      city: 'Lisbon',
      num_days: 4,
      days: [
        {
          day_index: 0,
          bad_weather: false,
          attractions: [
            {
              name: 'Belem Tower',
              category: 'tourism=attraction',
              weather_exposure: 'outdoor',
              lat: 38.69,
              lon: -9.22,
              fee: null,
            },
          ],
          meals: {
            lunch: {
              name: 'Tasca da Esquina',
              category: 'amenity=restaurant',
              cuisine: 'portuguese',
            },
          },
          intracity_hops: [],
        },
      ],
      unscheduled_attractions: [
        {
          name: 'Jerónimos Monastery',
          ua_ref: 'ua-jeronimos',
          category: 'tourism=attraction',
        },
      ],
    },
  ],
  risk_signals: { per_leg: [] },
  budget_rows: [
    { label: 'Lodging', cents: 80000, note: null },
    { label: 'Insurance', cents: 30000, note: null },
    { label: 'Visa fees', cents: 30000, note: null },
  ],
};

/** Preseed a guest session in localStorage so the picker is bypassed. */
function preseedGuest(page: Parameters<Parameters<typeof test>[1]>[0]): Promise<void> {
  return page.addInitScript(() => {
    localStorage.setItem('tg_session', JSON.stringify({ user: null, guest: true, savedAt: Date.now() }));
  });
}

/** Abort OSM tiles and mock the emergency feed. */
async function mockStandardRoutes(page: Parameters<Parameters<typeof test>[1]>[0]): Promise<void> {
  await page.route('**/emergencies', (route) =>
    route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ status: 'ok', countries: [] }),
    }),
  );
  await page.route('**/tile.openstreetmap.org/**', (route) => route.abort());
}

/**
 * Mock /negotiate_text via the degrade path:
 *   call 1 (stream:true) → {} (no stream_id → degrade)
 *   call 2 (stream:false) → plan after optional delay
 * Returns a getter for the first call's parsed body.
 */
function mockNegotiate(
  page: Parameters<Parameters<typeof test>[1]>[0],
  plan: unknown,
  secondCallDelayMs = 0,
): { getFirstBody: () => Record<string, unknown> | null } {
  let firstBody: Record<string, unknown> | null = null;
  let callCount = 0;
  page.route('**/negotiate_text', async (route) => {
    callCount++;
    const body = JSON.parse(route.request().postData() || '{}') as Record<string, unknown>;
    if (callCount === 1) {
      firstBody = body;
      await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({}) });
    } else {
      if (secondCallDelayMs > 0) {
        await new Promise<void>((resolve) => setTimeout(resolve, secondCallDelayMs));
      }
      await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(plan) });
    }
  });
  return { getFirstBody: () => firstBody };
}

// ─────────────────────────────────────────────────────────────────────────────
// Test 1: empty state on fresh load
// ─────────────────────────────────────────────────────────────────────────────
test('empty state shows guide-panel with heading and popular trip cards on first load', async ({
  page,
}) => {
  // Clear localStorage so there is no restored session choice.
  await page.addInitScript(() => localStorage.clear());

  // Mock /session so the picker can load its user list.
  await page.route('**/session', (route) =>
    route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ demo_users: [] }),
    }),
  );
  await mockStandardRoutes(page);

  await page.goto('/');

  // With no localStorage session, the session picker appears. Navigate past it.
  await expect(page.getByTestId('continue-guest')).toBeVisible({ timeout: 6000 });
  await page.getByTestId('continue-guest').click();

  // Guide panel must be visible in the empty state.
  const guidePanel = page.getByTestId('guide-panel');
  await expect(guidePanel).toBeVisible({ timeout: 5000 });

  // The heading inside the guide panel must contain the expected text.
  await expect(guidePanel.locator('.guide-heading')).toContainText(
    'Where would you like to go?',
  );

  // At least one popular trip card must be visible.
  await expect(page.locator('.guide-card').first()).toBeVisible();
});

// ─────────────────────────────────────────────────────────────────────────────
// Test 2: split layout — chat-input and guide-panel are visible simultaneously
// ─────────────────────────────────────────────────────────────────────────────
test('empty state: chat-input is present and focusable alongside the guide panel', async ({
  page,
}) => {
  await preseedGuest(page);
  await mockStandardRoutes(page);

  await page.goto('/');

  const guidePanel = page.getByTestId('guide-panel');
  const chatInput = page.getByTestId('chat-input');

  // Both must be visible at the same time (the split 2-column layout).
  await expect(guidePanel).toBeVisible({ timeout: 5000 });
  await expect(chatInput).toBeVisible({ timeout: 5000 });

  // The chat input must be focusable.
  await chatInput.focus();
  await expect(chatInput).toBeFocused();
});

// ─────────────────────────────────────────────────────────────────────────────
// Test 3: clicking a guide card sends the trip prompt via negotiate_text
// ─────────────────────────────────────────────────────────────────────────────
test('guide card click fills chat-input with the trip prompt and sends', async ({ page }) => {
  await preseedGuest(page);
  await mockStandardRoutes(page);
  const { getFirstBody } = mockNegotiate(page, BASE_PLAN);

  await page.goto('/');

  // Wait for the guide panel and its cards to appear.
  await expect(page.getByTestId('guide-panel')).toBeVisible({ timeout: 5000 });
  await expect(page.locator('.guide-card').first()).toBeVisible();

  // Click the first guide card (Japan — first card in the Asia tab).
  await page.locator('.guide-card').first().click();

  // negotiate_text must have been called with the prompt from that card.
  await expect.poll(() => getFirstBody()?.text, { timeout: 5000 }).toBe(
    '10 days Japan, $2500',
  );
  expect(getFirstBody()?.plan).toBe(true);
});

// ─────────────────────────────────────────────────────────────────────────────
// Test 4: loading state shows "Planning your trip" inside the guide panel
// ─────────────────────────────────────────────────────────────────────────────
test('loading state shows Planning your trip message in guide-panel', async ({ page }) => {
  await preseedGuest(page);
  await mockStandardRoutes(page);
  // Delay the second (degrade) call so we can observe the loading UI.
  mockNegotiate(page, BASE_PLAN, 1000);

  await page.goto('/');

  await page.getByTestId('chat-input').fill('5 days in lisbon, culture');
  await page.getByRole('button', { name: 'Send' }).click();

  // While the degrade call is pending, the guide panel shows "Planning your trip".
  const guidePanel = page.getByTestId('guide-panel');
  await expect(guidePanel).toBeVisible({ timeout: 5000 });
  await expect(guidePanel).toContainText('Planning your trip', { timeout: 3000 });
});

// ─────────────────────────────────────────────────────────────────────────────
// Test 5: guide-panel disappears once the plan is ready
// ─────────────────────────────────────────────────────────────────────────────
test('guide-panel disappears after plan is ready and itinerary is shown', async ({ page }) => {
  await preseedGuest(page);
  await mockStandardRoutes(page);
  mockNegotiate(page, BASE_PLAN);

  await page.goto('/');

  await page.getByTestId('chat-input').fill('5 days in lisbon');
  await page.getByRole('button', { name: 'Send' }).click();

  // The itinerary must appear after the plan is ready.
  await expect(page.getByTestId('itinerary')).toBeVisible({ timeout: 8000 });
  await expect(page.getByTestId('itinerary')).toContainText('Lisbon');

  // The guide panel must no longer be visible (empty-layout is removed from DOM).
  await expect(page.getByTestId('guide-panel')).not.toBeVisible();
});

// ─────────────────────────────────────────────────────────────────────────────
// Test 6: region tabs — switching changes the displayed destinations
// ─────────────────────────────────────────────────────────────────────────────
test('region tabs are rendered and switching changes destination cards', async ({ page }) => {
  await preseedGuest(page);  // guest → no nationality → defaults to asia tab
  await mockStandardRoutes(page);

  await page.goto('/');

  const guidePanel = page.getByTestId('guide-panel');
  await expect(guidePanel).toBeVisible({ timeout: 5000 });

  // Asia tab should be active by default (no nationality → defaultRegion returns 'asia')
  const asiaDests = guidePanel.locator('.guide-card');
  await expect(asiaDests.first()).toBeVisible();

  // Click the Europe region tab
  await guidePanel.locator('.rtab', { hasText: 'Europe' }).click();

  // After switching, a European destination should appear (e.g. Paris or Rome)
  const europeDests = guidePanel.locator('.guide-card');
  await expect(europeDests.first()).toBeVisible();
  // Verify a Europe card sends a Europe-specific prompt (not Asia)
  const europeText = await europeDests.first().innerText();
  expect(europeText).toBeTruthy();
  expect(europeText).not.toContain('Japan'); // Japan is in Asia tab, not Europe
});

// ─────────────────────────────────────────────────────────────────────────────
// Test 7: defaultRegion — EU nationality pre-selects Europe tab
// ─────────────────────────────────────────────────────────────────────────────
test('EU user nationality pre-selects the Europe region tab', async ({ page }) => {
  // Preseed a session with a GB user so defaultRegion('GB') → 'europe'
  await page.addInitScript(() => {
    localStorage.setItem('tg_session', JSON.stringify({
      user: { user_id: 'demo-gb', display_name: 'Test', persona: 'culture', nationality: 'GB', home_currency: 'GBP', wallet_balance_cents: 500000 },
      guest: false,
      savedAt: Date.now(),
    }));
  });
  await mockStandardRoutes(page);

  await page.goto('/');

  const guidePanel = page.getByTestId('guide-panel');
  await expect(guidePanel).toBeVisible({ timeout: 5000 });

  // Europe tab must be active (aria-selected) for a GB user
  await expect(
    guidePanel.locator('.rtab[aria-selected="true"]'),
  ).toContainText('Europe');

  // Asia tab must NOT be active
  await expect(
    guidePanel.locator('.rtab', { hasText: 'Asia' }),
  ).not.toHaveAttribute('aria-selected', 'true');
});

// ─────────────────────────────────────────────────────────────────────────────
// Test 8: persona recommendations — shown for logged-in user with known persona
// ─────────────────────────────────────────────────────────────────────────────
test('persona recommendations section visible for logged-in user with persona', async ({
  page,
}) => {
  // Preseed a foodie user — PERSONA_RECS['foodie'] exists in App.svelte
  await page.addInitScript(() => {
    localStorage.setItem('tg_session', JSON.stringify({
      user: { user_id: 'demo-mei', display_name: 'Mei', persona: 'foodie', nationality: 'SG', home_currency: 'SGD', wallet_balance_cents: 500000 },
      guest: false,
      savedAt: Date.now(),
    }));
  });
  await mockStandardRoutes(page);

  await page.goto('/');

  const guidePanel = page.getByTestId('guide-panel');
  await expect(guidePanel).toBeVisible({ timeout: 5000 });

  // Persona section label must appear
  await expect(guidePanel.locator('.guide-section-label')).toContainText('Recommended for foodie');

  // At least one persona recommendation card must be visible
  await expect(guidePanel.locator('.guide-card').first()).toBeVisible();
});

// ─────────────────────────────────────────────────────────────────────────────
// Test 9: persona recommendations — NOT shown for guest (no persona)
// ─────────────────────────────────────────────────────────────────────────────
test('persona recommendations section not shown for guest user', async ({ page }) => {
  await preseedGuest(page);
  await mockStandardRoutes(page);

  await page.goto('/');

  const guidePanel = page.getByTestId('guide-panel');
  await expect(guidePanel).toBeVisible({ timeout: 5000 });

  // No persona label for guests
  await expect(guidePanel.locator('.guide-section-label')).not.toBeVisible();
});

// ─────────────────────────────────────────────────────────────────────────────
// Grammar-teaching chips (landing-page-draft2): the true-empty pre-plan
// ChatPane instance passes showGrammarChips={true}. Chips show the exact
// typeable FORMAT (destination, duration, budget, style) so a first-time user
// learns the input grammar. They must:
//   (b) appear in the true-empty state,
//   (c) POPULATE the composer on click WITHOUT sending,
//   (d) disappear once any message exists (real exchange, not just the
//       initial greeting).
// ─────────────────────────────────────────────────────────────────────────────

test('grammar chips are visible in the true-empty pre-plan state', async ({ page }) => {
  await preseedGuest(page);
  await mockStandardRoutes(page);

  await page.goto('/');

  await expect(page.getByTestId('chat-pane')).toBeVisible({ timeout: 5000 });

  // Scope to the desktop chat-pane: the same chip markup is also duplicated (CSS
  // display:none-hidden, still in the DOM) inside the mobile bubble sheet, so an
  // unscoped page-wide count would double-count.
  const chips = page.getByTestId('chat-pane').getByTestId('grammar-chip');
  await expect(chips).toHaveCount(3);
  await expect(chips.first()).toBeVisible();
});

test('clicking a grammar chip populates the composer WITHOUT sending a message', async ({ page }) => {
  await preseedGuest(page);
  await mockStandardRoutes(page);
  // Any call to /negotiate_text here would mean a chip click auto-sent — fail loudly.
  let negotiateCalled = false;
  await page.route('**/negotiate_text', (route) => {
    negotiateCalled = true;
    return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({}) });
  });

  await page.goto('/');

  const chatInput = page.getByTestId('chat-input');
  // Scope to the desktop chat-pane — the same markup is duplicated (CSS-hidden)
  // in the mobile bubble sheet.
  const chip = page.getByTestId('chat-pane').getByTestId('grammar-chip').first();
  await expect(chip).toBeVisible({ timeout: 5000 });
  const chipFullText = await chip.innerText();

  const msgCountBefore = await page.getByTestId('chat-pane').locator('.msgs .b').count();

  await chip.click();

  // The composer now holds the chip's text — populated, not cleared/sent.
  const inputValue = await chatInput.inputValue();
  expect(inputValue.length).toBeGreaterThan(0);
  expect(chipFullText).toContain(inputValue);

  // No new message appeared, and /negotiate_text was never called — the click
  // must NOT have auto-sent (distinct from the guide-panel's destination cards).
  await page.waitForTimeout(200); // let any accidental async send settle
  const msgCountAfter = await page.getByTestId('chat-pane').locator('.msgs .b').count();
  expect(msgCountAfter).toBe(msgCountBefore);
  expect(negotiateCalled).toBe(false);

  // Only an explicit Send click actually sends.
  await page.getByRole('button', { name: 'Send' }).click();
  await expect.poll(() => negotiateCalled, { timeout: 5000 }).toBe(true);
  const msgCountAfterSend = await page.getByTestId('chat-pane').locator('.msgs .b').count();
  expect(msgCountAfterSend).toBeGreaterThan(msgCountBefore);
});

test('grammar chips disappear once a message exists', async ({ page }) => {
  await preseedGuest(page);
  await mockStandardRoutes(page);
  mockNegotiate(page, BASE_PLAN);

  await page.goto('/');

  await expect(page.getByTestId('chat-pane').getByTestId('grammar-chip').first()).toBeVisible({ timeout: 5000 });

  await page.getByTestId('chat-input').fill('5 days in lisbon, culture');
  await page.getByRole('button', { name: 'Send' }).click();

  // Once a real user/bot exchange exists, the chips are removed (not just hidden)
  // — both the desktop and mobile-sheet copies of the markup.
  await expect(page.getByTestId('grammar-chip')).toHaveCount(0);
});
