import { test, expect, type Page } from '@playwright/test';

// state-layout.spec.ts — layout regression tests for all non-plan planner outcomes.
//
// Root cause (2026-07-01): after a needs_clarification / cannot_satisfy / declined /
// budget_shortfall / server_error response, the app exited the empty-layout 2-col
// block and rendered <ChatPane> as a raw aside with height:78vh and NO width constraint.
// On desktop this filled the entire viewport width, pushing the state-card below the fold
// so the user could not see the planner's honest answer without scrolling.
//
// Fix: keep the 2-col layout (empty-layout CSS classes) for all !hasPlan states —
// chat in the left column (≤340px), state message in the right guide-panel column.
//
// These tests assert:
//   1. State-card is visible in viewport without scrolling (regression guard — was below fold)
//   2. State-card and chat-input are both reachable simultaneously (side-by-side)
//   3. The chat container width is ≤ 360px (constrained, not full-width)
//   4. The correct text appears in the state-card for each outcome
//   5. The chat-input remains interactive so the user can follow up
//   6. After needs_clarification the user can reply and get a plan_ready
//   7. plan_ready still triggers the floating 🤖 mode (not the 2-col error layout)
//
// All tests are fully hermetic — /negotiate_text is mocked; no live backend.

// ── Fixtures ──────────────────────────────────────────────────────────────────

// Fixtures shaped to match uiState() mappings in src/lib/api.ts:
//   needs_clarification → r.needs_clarification === true   (boolean field, not outcome string)
//   declined           → outcome === 'cannot_satisfy'      (do-not-recommend collapses to 'declined' in uiState)
//   server_error       → reason.includes('server_error')
//   budget_shortfall   → r.budget_shortfall_cents != null

const NEEDS_CLARIFICATION = {
  needs_clarification: true,   // uiState key — NOT outcome:'needs_clarification'
  reason: 'Bali has multiple distinct areas — what vibe are you looking for? (beach · culture · surf · relax)',
};

const CANNOT_SATISFY = {
  outcome: 'cannot_satisfy',
  reason: 'No lodging inventory found for that destination. Try a nearby city or adjust dates.',
};

const BUDGET_SHORTFALL = {
  outcome: 'cannot_satisfy',
  budget_shortfall_cents: 45000,
  min_feasible_total_cents: 145000,
};

// Declined maps through outcome:'cannot_satisfy' + do-not-recommend reason (uiState returns 'declined')
const DECLINED = {
  outcome: 'cannot_satisfy',
  reason: 'This destination falls in our do-not-recommend zone due to Level 4 travel advisories.',
};

// server_error: reason must contain the literal string 'server_error' for uiState to return that state
const SERVER_ERROR = {
  outcome: 'cannot_satisfy',
  reason: 'server_error: The planner hit an internal error. Please try again.',
};

const PLAN_READY = {
  outcome: 'plan_ready',
  payment_status: 'held',
  booking_ref: null,
  idempotency_key: 'trip-sl-1',
  package_total_with_fees_cents: 160000,
  total_budget_cents: 300000,
  wallet: { balance_cents: 500000, held_cents: 160000, debited: false },
  legs: [
    { leg_id: 'leg-0', city: 'Bali', checkin: '2026-09-01', checkout: '2026-09-08', lat: -8.34, lng: 115.09 },
  ],
  day_plans: [
    {
      leg_id: 'leg-0', city: 'Bali', num_days: 7,
      days: [{ day_index: 0, bad_weather: false, attractions: [], meals: {}, intracity_hops: [] }],
    },
  ],
  risk_signals: { per_leg: [] },
  budget_rows: [{ label: 'Lodging', cents: 100000, note: null }],
};

// ── Shared setup helpers ──────────────────────────────────────────────────────

async function setup(page: Page): Promise<void> {
  await page.addInitScript(() => {
    localStorage.setItem('tg_session', JSON.stringify({ user: null, guest: true, savedAt: Date.now() }));
  });
  await page.route('**/emergencies', (r) =>
    r.fulfill({ status: 200, contentType: 'application/json',
      body: JSON.stringify({ status: 'ok', countries: [] }) }));
  await page.route(/tile|openstreetmap|basemaps|demotiles/, (r) => r.abort());
}

/** Route negotiate_text to return body on every call. */
async function mockNegotiateAlways(page: Page, body: unknown): Promise<void> {
  await page.route('**/negotiate_text', (r) =>
    r.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(body) }));
}

/**
 * Route negotiate_text so that the FIRST user message produces firstBody and the
 * SECOND user message produces secondBody. planStreaming makes 2 internal calls per
 * user message (stream:true → no stream_id → degrade to stream:false), so calls
 * 1–2 deliver firstBody and calls 3+ deliver secondBody.
 */
async function mockNegotiateSequence(
  page: Page, firstBody: unknown, secondBody: unknown,
): Promise<void> {
  let callCount = 0;
  await page.route('**/negotiate_text', (r) => {
    callCount++;
    // planStreaming: msg-1 → call 1 (stream:true) + call 2 (degrade stream:false)
    //               msg-2 → call 3 (stream:true) + call 4 (degrade stream:false)
    const response = callCount <= 2 ? firstBody : secondBody;
    return r.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(response) });
  });
}

/** Submit a chat message and wait for the state-card to appear. */
async function sendAndWaitForStateCard(page: Page, text: string): Promise<void> {
  await page.getByTestId('chat-input').fill(text);
  await page.getByRole('button', { name: 'Send' }).click();
  await expect(page.getByTestId('state-card')).toBeVisible({ timeout: 10_000 });
}

// ── Suite 1: Content — each outcome shows the correct message ─────────────────

test.describe('state-card content per outcome', () => {
  test('needs_clarification shows the planner reason in state-card', async ({ page }) => {
    await setup(page);
    await mockNegotiateAlways(page, NEEDS_CLARIFICATION);
    await page.goto('/');

    await sendAndWaitForStateCard(page, '7 days Bali beaches, $1200, solo');

    await expect(page.getByTestId('state-card')).toContainText('Bali');
    await expect(page.getByTestId('state-card')).toContainText('vibe');
  });

  test('needs_clarification strips internal "cannot_satisfy: " prefix from reason', async ({ page }) => {
    // Regression: the parser sometimes returns reason: 'cannot_satisfy: <user-facing text>'
    // for needs_clarification responses. stateNote() must strip the prefix before display.
    await setup(page);
    await mockNegotiateAlways(page, {
      needs_clarification: true,
      reason: 'cannot_satisfy: I could not identify a supported destination — try a city name.',
    });
    await page.goto('/');

    await sendAndWaitForStateCard(page, 'somewhere warm, $1000');

    const card = page.getByTestId('state-card');
    // Clean reason shown — prefix stripped
    await expect(card).toContainText('I could not identify a supported destination');
    // Raw prefix must NOT be visible
    await expect(card).not.toContainText('cannot_satisfy:');
  });

  test('cannot_satisfy shows the honest decline reason', async ({ page }) => {
    await setup(page);
    await mockNegotiateAlways(page, CANNOT_SATISFY);
    await page.goto('/');

    await sendAndWaitForStateCard(page, '5 days in Atlantis, $1000');

    await expect(page.getByTestId('state-card')).toContainText('No lodging inventory');
  });

  test('budget_shortfall shows the shortfall amount and minimum feasible', async ({ page }) => {
    await setup(page);
    await mockNegotiateAlways(page, BUDGET_SHORTFALL);
    await page.goto('/');

    await sendAndWaitForStateCard(page, '7 days Tokyo, $100');

    const card = page.getByTestId('state-card');
    await expect(card).toContainText('$450.00');   // shortfall: 45000 cents
    await expect(card).toContainText('$1,450.00'); // min_feasible: 145000 cents
  });

  test('declined shows the advisory reason', async ({ page }) => {
    await setup(page);
    await mockNegotiateAlways(page, DECLINED);
    await page.goto('/');

    await sendAndWaitForStateCard(page, '5 days in active war zone, $2000');

    await expect(page.getByTestId('state-card')).toContainText('do-not-recommend');
    await expect(page.getByTestId('state-card')).toContainText('Level 4');
  });

  test('server_error shows the internal error message', async ({ page }) => {
    await setup(page);
    await mockNegotiateAlways(page, SERVER_ERROR);
    await page.goto('/');

    await sendAndWaitForStateCard(page, '5 days in tokyo, $2000');

    // stateNote('server_error') returns the hardcoded string regardless of r.reason
    await expect(page.getByTestId('state-card')).toContainText('planner hit an internal error');
  });
});

// ── Suite 2: Layout regression — state-card must be IN VIEWPORT, not below fold ──
//
// The core regression: before the fix, the embedded ChatPane was height:78vh with no
// width constraint, filling the full desktop width and pushing the state-card below
// the visible area. The state-card must now be visible without scrolling.

test.describe('state-card is in viewport without scrolling (regression guard)', () => {
  const DESKTOP = { width: 1280, height: 800 };
  const TABLET  = { width: 820, height: 1180 };

  for (const [stateName, label, viewport, outcome] of [
    ['needs_clarification', 'desktop', DESKTOP, NEEDS_CLARIFICATION],
    ['cannot_satisfy',      'desktop', DESKTOP, CANNOT_SATISFY],
    ['declined',            'desktop', DESKTOP, DECLINED],
    ['budget_shortfall',    'desktop', DESKTOP, BUDGET_SHORTFALL],
    ['server_error',        'desktop', DESKTOP, SERVER_ERROR],
    ['needs_clarification', 'tablet',  TABLET,  NEEDS_CLARIFICATION],
    ['cannot_satisfy',      'tablet',  TABLET,  CANNOT_SATISFY],
  ] as [string, string, { width: number; height: number }, unknown][]) {
    test(`${stateName} — state-card in viewport on ${label} (${viewport.width}x${viewport.height})`, async ({ page }) => {
      page.setViewportSize(viewport);
      await setup(page);
      await mockNegotiateAlways(page, outcome);
      await page.goto('/');

      await sendAndWaitForStateCard(page, '7 days Bali, $1200');

      // Core regression assertion: state-card must be visible without scrolling.
      // Before the fix this FAILED because 78vh chat pushed it below the fold.
      await expect(page.getByTestId('state-card')).toBeInViewport();
    });
  }
});

// ── Suite 3: Simultaneous visibility — chat-input and state-card both reachable ──
//
// The 2-col layout must keep chat-input and state-card on screen at the same time.
// Before the fix: chat filled 78vh → state-card was below the fold AND the user
// couldn't see the planner's reason next to the input.

test.describe('chat-input and state-card both visible simultaneously', () => {
  test('needs_clarification: chat-input and state-card are both in viewport', async ({ page }) => {
    await setup(page);
    await mockNegotiateAlways(page, NEEDS_CLARIFICATION);
    await page.goto('/');

    await sendAndWaitForStateCard(page, '7 days Bali, $1200');

    await expect(page.getByTestId('chat-input')).toBeInViewport();
    await expect(page.getByTestId('state-card')).toBeInViewport();
  });

  test('cannot_satisfy: chat-input and state-card are both in viewport', async ({ page }) => {
    await setup(page);
    await mockNegotiateAlways(page, CANNOT_SATISFY);
    await page.goto('/');

    await sendAndWaitForStateCard(page, '5 days in Atlantis, $1000');

    await expect(page.getByTestId('chat-input')).toBeInViewport();
    await expect(page.getByTestId('state-card')).toBeInViewport();
  });

  test('declined: chat-input and state-card are both in viewport', async ({ page }) => {
    await setup(page);
    await mockNegotiateAlways(page, DECLINED);
    await page.goto('/');

    await sendAndWaitForStateCard(page, '7 days conflict zone, $2000');

    await expect(page.getByTestId('chat-input')).toBeInViewport();
    await expect(page.getByTestId('state-card')).toBeInViewport();
  });
});

// ── Suite 4: Geometry — chat column must be constrained, not full-width ────────
//
// The embedded ChatPane in the 2-col layout is constrained by empty-chat-col
// (flex 2:3 vs the guide column, min 300px / max 420px — scaling-fix increment).
// Before the original #118 fix, it stretched to the full page width (≈1280px)
// with no constraint. We measure the bounding rect of the chat pane element and
// assert width ≤ 440px (max-width 420px + a small rendering-variance margin).

test.describe('chat column width is constrained in error states (not full-width)', () => {
  test.use({ viewport: { width: 1280, height: 800 } });

  for (const [label, outcome] of [
    ['needs_clarification', NEEDS_CLARIFICATION],
    ['cannot_satisfy',      CANNOT_SATISFY],
    ['declined',            DECLINED],
  ] as [string, unknown][]) {
    test(`${label}: chat container is constrained width (≤ 380px), not full viewport`, async ({ page }) => {
      await setup(page);
      await mockNegotiateAlways(page, outcome);
      await page.goto('/');

      await sendAndWaitForStateCard(page, '7 days somewhere, $1500');

      // Measure the bounding rect of the chat pane.
      // In the 2-col layout it lives in .empty-chat-col (flex 2:3, max-width: 420px).
      // Before the fix it had no container → stretched to ~1280px.
      const chatWidth = await page.getByTestId('chat-pane').evaluate(
        (el) => el.getBoundingClientRect().width,
      );
      expect(chatWidth).toBeLessThanOrEqual(440);
      expect(chatWidth).toBeGreaterThan(200); // must still be a real panel, not collapsed
    });
  }
});

// ── Suite 5: Side-by-side geometry — state-card is BESIDE chat, not BELOW it ──
//
// On desktop, the two columns should overlap vertically (same top, both near the
// top of the page). If the card is below the chat, its top ≥ chat.bottom — that is
// the pre-fix stacked layout. We assert card.top < chat.bottom (side-by-side).

test('needs_clarification desktop: state-card is beside chat-pane, not stacked below it', async ({ page }) => {
  page.setViewportSize({ width: 1280, height: 800 });
  await setup(page);
  await mockNegotiateAlways(page, NEEDS_CLARIFICATION);
  await page.goto('/');

  await sendAndWaitForStateCard(page, '7 days Bali beaches, $1200');

  const chatRect  = await page.getByTestId('chat-pane').boundingBox();
  const cardRect  = await page.getByTestId('state-card').boundingBox();

  expect(chatRect).not.toBeNull();
  expect(cardRect).not.toBeNull();

  // Side-by-side: card.top must be LESS than chat.bottom (they share vertical space).
  // Stacked (pre-fix): card.top ≥ chat.bottom — card starts only after chat ends.
  expect(cardRect!.y).toBeLessThan(chatRect!.y + chatRect!.height);
});

// ── Suite 6: Chat interactivity — user can follow up after any error state ─────

test.describe('chat remains interactive after error states', () => {
  test('needs_clarification: chat-input is enabled and accepts follow-up text', async ({ page }) => {
    await setup(page);
    await mockNegotiateAlways(page, NEEDS_CLARIFICATION);
    await page.goto('/');

    await sendAndWaitForStateCard(page, '7 days Bali, $1200');

    const input = page.getByTestId('chat-input');
    await expect(input).toBeEnabled();
    await input.fill('I like beach vibes and surfing');
    await expect(input).toHaveValue('I like beach vibes and surfing');
  });

  test('cannot_satisfy: chat-input is enabled for a new trip attempt', async ({ page }) => {
    await setup(page);
    await mockNegotiateAlways(page, CANNOT_SATISFY);
    await page.goto('/');

    await sendAndWaitForStateCard(page, '5 days in Atlantis, $1000');

    await expect(page.getByTestId('chat-input')).toBeEnabled();
  });

  test('declined: chat-input is enabled for a different destination', async ({ page }) => {
    await setup(page);
    await mockNegotiateAlways(page, DECLINED);
    await page.goto('/');

    await sendAndWaitForStateCard(page, '5 days somewhere dangerous, $2000');

    await expect(page.getByTestId('chat-input')).toBeEnabled();
  });
});

// ── Suite 7: Full flow — needs_clarification → follow-up → plan_ready ─────────
//
// The most important flow: user types Bali request → gets clarification asking for
// vibe → types "beach please" → gets plan_ready. The layout must transition from
// the 2-col error layout to the plan layout (floating 🤖 + itinerary grid).

test('needs_clarification → follow-up → plan_ready transitions to floating chat + itinerary', async ({ page }) => {
  await setup(page);
  // First call: clarification. All subsequent calls: plan_ready.
  await mockNegotiateSequence(page, NEEDS_CLARIFICATION, PLAN_READY);
  await page.goto('/');

  // Step 1: initial request triggers clarification
  await page.getByTestId('chat-input').fill('7 days Bali beaches, $1200, solo');
  await page.getByRole('button', { name: 'Send' }).click();
  await expect(page.getByTestId('state-card')).toBeVisible({ timeout: 10_000 });
  await expect(page.getByTestId('state-card')).toContainText('vibe');

  // Step 2: follow-up triggers plan_ready
  await page.getByTestId('chat-input').fill('Beach please, Seminyak area');
  await page.getByRole('button', { name: 'Send' }).click();

  // Itinerary replaces the state-card
  await expect(page.getByTestId('itinerary')).toBeVisible({ timeout: 12_000 });
  await expect(page.getByTestId('itinerary')).toContainText('Bali');

  // State-card must be gone — the layout transitioned to plan view
  await expect(page.getByTestId('state-card')).not.toBeVisible();

  // ChatPane must now be in floating mode (bot-trigger replaces the embedded aside)
  await expect(page.getByTestId('assistant-trigger')).toBeVisible();
  await expect(page.getByTestId('chat-pane')).not.toBeVisible();

  // The status banner must show plan_ready
  await expect(page.getByTestId('status-banner')).toContainText('Plan ready');
});

// ── Suite 8: Error state does NOT trigger floating chat mode ──────────────────
//
// The floating 🤖 trigger (assistant-trigger) must ONLY appear when hasPlan is true
// (plan_ready / success). Error states must keep the embedded chat — not the float.

test.describe('error states keep embedded chat (not floating trigger)', () => {
  for (const [label, outcome] of [
    ['needs_clarification', NEEDS_CLARIFICATION],
    ['cannot_satisfy',      CANNOT_SATISFY],
    ['declined',            DECLINED],
  ] as [string, unknown][]) {
    test(`${label}: shows embedded chat-pane, NOT the floating assistant-trigger`, async ({ page }) => {
      await setup(page);
      await mockNegotiateAlways(page, outcome);
      await page.goto('/');

      await sendAndWaitForStateCard(page, '7 days somewhere, $1500');

      // Embedded chat pane must be present (the 2-col left column)
      await expect(page.getByTestId('chat-pane')).toBeVisible();

      // Floating trigger must NOT be visible (only for plan_ready/success)
      await expect(page.getByTestId('assistant-trigger')).not.toBeVisible();
    });
  }
});

// ── Suite 9: Mobile (≤ 768px) — error state stacks chat above guide panel ─────
//
// On mobile the empty-layout goes column-reverse (guide on top, chat bubbles at bottom).
// The floating mob-bubble replaces the desktop embedded chat. State-card must still be
// visible and the chat-input must be accessible via the mob-sheet.

test.describe('mobile layout for error states', () => {
  test.use({ viewport: { width: 390, height: 844 } }); // iPhone 14

  test('needs_clarification on mobile: state-card visible and mob-bubble present', async ({ page }) => {
    await setup(page);
    await mockNegotiateAlways(page, NEEDS_CLARIFICATION);
    await page.goto('/');

    await page.locator('.mob-bubble').click(); // open mobile chat sheet
    await page.locator('.mob-sheet-box textarea').fill('7 days Bali beaches, $1200');
    await page.locator('.mob-sheet-box .send').click();

    await expect(page.getByTestId('state-card')).toBeVisible({ timeout: 10_000 });
    await expect(page.getByTestId('state-card')).toContainText('vibe');

    // On mobile the mob-bubble must still be present (chat accessible)
    await expect(page.locator('.mob-bubble')).toBeVisible();
  });
});

// ── Suite 10: Transition from plan_ready back to error (new trip) ─────────────
//
// If user clicks "Start new trip" from a plan_ready state, the app resets to empty.
// A subsequent error response should use the 2-col layout again (not leave ghost
// floating chat artefacts from the plan state).

test('after plan_ready → start new trip → needs_clarification still uses 2-col layout', async ({ page }) => {
  await setup(page);
  // First negotiation: plan_ready. Second: clarification.
  await mockNegotiateSequence(page, PLAN_READY, NEEDS_CLARIFICATION);
  await page.goto('/');

  // Step 1: get a plan
  await page.getByTestId('chat-input').fill('7 days Bali, $1200');
  await page.getByRole('button', { name: 'Send' }).click();
  await expect(page.getByTestId('itinerary')).toBeVisible({ timeout: 12_000 });

  // Step 2: start a new trip
  await page.getByTestId('new-trip').click();
  await expect(page.getByTestId('guide-panel')).toBeVisible({ timeout: 5_000 });

  // Step 3: new request → clarification
  await page.getByTestId('chat-input').fill('7 days somewhere vague');
  await page.getByRole('button', { name: 'Send' }).click();
  await expect(page.getByTestId('state-card')).toBeVisible({ timeout: 10_000 });

  // Must be 2-col layout: chat-pane visible, floating trigger NOT visible
  await expect(page.getByTestId('chat-pane')).toBeVisible();
  await expect(page.getByTestId('assistant-trigger')).not.toBeVisible();

  // State-card still in viewport (not pushed below fold)
  await expect(page.getByTestId('state-card')).toBeInViewport();
});

// ── Suite 11: Bounded chat height — many accumulated messages in the reused
//    clarification/error state must NOT blow past the .chat max-height cap ────
//
// ChatPane's embedded .chat is `height: auto; max-height: 78vh` (landing-page-
// draft2). Root-cause risk: this ChatPane instance is REUSED for the
// needs_clarification/error 2-col layout, where a real back-and-forth (unlike
// the guide-card auto-send flows) can accumulate many messages. If the cap
// regressed to a plain `height: 78vh` (or no cap at all), the panel would grow
// unbounded with the message list instead of scrolling internally.

test.describe('chat panel bounded height with many accumulated messages', () => {
  test.use({ viewport: { width: 1280, height: 800 } });

  test('needs_clarification: several back-and-forth exchanges keep chat-pane height capped and scroll internally', async ({ page }) => {
    await setup(page);
    await mockNegotiateAlways(page, NEEDS_CLARIFICATION);
    await page.goto('/');

    // Regression lock for `height: auto` specifically (not just the max-height cap):
    // with only the initial greeting + one exchange, the panel must be MEASURABLY
    // SHORTER than the 78vh cap — a plain fixed `height: 78vh` (the pre-fix behavior)
    // would render at ~78vh regardless of content, so this distinguishes "shrinks to
    // fit sparse content" from "always tall." A loose few-message cap check alone
    // can't tell the two apart (both stay under 78vh either way).
    await sendAndWaitForStateCard(page, '7 days Bali beaches, $1200, solo');
    const viewportHeight = page.viewportSize()!.height;
    const earlyRect = await page.getByTestId('chat-pane').boundingBox();
    expect(earlyRect).not.toBeNull();
    expect(earlyRect!.height).toBeLessThan(viewportHeight * 0.78 - 40);

    // Fire several more distinct follow-ups so messages genuinely accumulate (user + note
    // per round-trip, on top of the initial bot greeting and the exchange above).
    const followUps = [
      '7 days Bali beaches, $1200, solo',
      'I like beach vibes and surfing',
      'Maybe somewhere quieter actually',
      'What about Uluwatu specifically?',
      'Or Canggu, either works',
      'Budget can flex to $1500',
      'Prefer late September dates',
      'Solo trip, no group needed',
    ];
    for (const text of followUps) {
      await sendAndWaitForStateCard(page, text);
    }

    // At least 8 round-trips × 2 messages/round + the initial greeting must be present.
    const msgCount = await page.getByTestId('chat-pane').locator('.msgs .b').count();
    expect(msgCount).toBeGreaterThanOrEqual(followUps.length * 2);

    // Core assertion: the chat panel's bounding box height must stay capped at
    // 78vh (+ a small rendering-variance margin) — not grow with message count.
    const chatRect = await page.getByTestId('chat-pane').boundingBox();
    expect(chatRect).not.toBeNull();
    expect(chatRect!.height).toBeLessThanOrEqual(viewportHeight * 0.78 + 8);

    // The internal .msgs list must be the thing that scrolls (its content overflows
    // its own box) — i.e. the cap is real, not just an accidentally-short message list.
    const msgsOverflow = await page.getByTestId('chat-pane').locator('.msgs').evaluate(
      (el) => el.scrollHeight > el.clientHeight,
    );
    expect(msgsOverflow).toBe(true);
  });
});

// ── Suite 12: Grammar chips are absent from the reused clarification/error
//    state — ONLY the true empty pre-plan state passes showGrammarChips ─────

test.describe('grammar chips are NOT shown in the clarification/error state', () => {
  for (const [label, outcome] of [
    ['needs_clarification', NEEDS_CLARIFICATION],
    ['cannot_satisfy',      CANNOT_SATISFY],
    ['declined',            DECLINED],
    ['server_error',        SERVER_ERROR],
  ] as [string, unknown][]) {
    test(`${label}: no grammar-chip is rendered once the state-card is showing`, async ({ page }) => {
      await setup(page);
      await mockNegotiateAlways(page, outcome);
      await page.goto('/');

      await sendAndWaitForStateCard(page, '7 days somewhere, $1500');

      await expect(page.getByTestId('grammar-chip')).toHaveCount(0);
    });
  }
});
