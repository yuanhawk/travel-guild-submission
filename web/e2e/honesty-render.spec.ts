import { test, expect } from '@playwright/test';

// honesty-render.spec.ts — consolidated interaction-level e2e gate for 6 honesty-render
// fields that each already had a genuine COMPONENT test (hand-mounted prop, e.g.
// RightRail.planning-note.test.ts) but no proof the served field actually reaches
// the DOM through the real negotiate/replan app flow. Structural template:
// e2e/assumption-notes.spec.ts — same page.route mock-through-App.svelte pattern,
// same guest-session seeding, same data-testid assertion style.
//
// Covers: planning_note, day_plans notes, hop disambiguation, result.advisories[],
// legs[].total_cents, and the forbiddenMessage/SESSION_INVALID_MESSAGE re-login copy.
//
// Both fixtures are hermetic: /negotiate_text and /refine are mocked via page.route.
// OSM tiles are aborted (offline-safe). A guest session is pre-seeded so the
// SessionPicker view is bypassed.

// ── realistic field values (reused from the corresponding component tests) ──

// #189 — RightRail.planning-note.test.ts
const PLANNING_NOTE =
  'US State Dept Level 3 (Reconsider Travel) — travel is generally manageable ' +
  'with advance planning and a reputable local guide. Avoid remote and border areas.';

// #190 — Itinerary.dayplan-notes.test.ts
const DAYPLAN_NOTE =
  'no restaurant matching requested cuisine for some meal slot; fell back to the ' +
  'best available venue (not fabricated)';

// #197 — Itinerary.transport-gaps.test.ts ("disambiguates two hotel-endpoint hops
// whose server-formatted labels are byte-identical")
const HOP_LABEL = '~20 min, metro, est. (from city centre — exact hotel location unavailable)';

// #205 — RightRail.advisories.test.ts
const ADVISORY_VISA =
  'Entry requirements not verified for this nationality/destination pair — confirm visa ' +
  'eligibility with the destination government before travel.';
const ADVISORY_HEALTH =
  'Required vaccination status not verified — confirm yellow fever certificate requirements ' +
  'before travel.';

// #206 — Itinerary.perleg-cost.test.ts
const LEG_TOTAL_CENTS = 68000;              // $680.00
const LEG_COST_BASIS_LABEL = 'Prepaid via secure checkout';

// #203/#207 — api.test.ts:
// forbiddenMessage({reason:'session_invalid'}) === SESSION_INVALID_MESSAGE
const SESSION_INVALID_MESSAGE = 'Your session expired — log in again and retry.';
const NOT_TRIP_OWNER_MESSAGE = 'This trip belongs to another session.'; // current main's ONLY forbidden copy

// ── consolidated plan_ready envelope carrying all 5 render-site fields ──────
const PLAN_WITH_HONESTY_FIELDS: Record<string, unknown> = {
  outcome: 'plan_ready',
  payment_status: 'held',
  booking_ref: null,
  idempotency_key: 'trip-hr-1',
  package_total_with_fees_cents: LEG_TOTAL_CENTS,
  total_budget_cents: 200000,
  wallet: { balance_cents: 300000, held_cents: LEG_TOTAL_CENTS, debited: false },
  legs: [{
    leg_id: 'leg-0', city: 'Kyoto', checkin: '2027-04-10', checkout: '2027-04-14',
    // #206 — per-leg lodging cost + honesty basis
    total_cents: LEG_TOTAL_CENTS,
    cost_basis: 'ucp_prepaid',
    cost_basis_label: LEG_COST_BASIS_LABEL,
  }],
  day_plans: [{
    leg_id: 'leg-0', city: 'Kyoto', country: 'Japan',
    checkin: '2027-04-10', checkout: '2027-04-14', num_days: 1,
    // #190 — day-planner honesty disclosure
    notes: [DAYPLAN_NOTE],
    days: [{
      day_index: 0,
      bad_weather: false,
      attractions: [
        { name: 'Riverside Park', category: 'tourism=attraction', lat: 34.99, lon: 135.77 },
        { name: 'Old Town Market', category: 'tourism=attraction', lat: 34.98, lon: 135.76 },
      ],
      meals: {},
      // #197 — two hotel-endpoint hops with intentionally byte-identical raw labels
      // but different from/to, proving the disambiguation prefix reaches the real DOM.
      intracity_hops: [
        {
          from: 'Hotel', to: 'Riverside Park', from_kind: 'hotel', to_kind: 'attraction',
          mode: 'metro', minutes: 20, label: HOP_LABEL,
        },
        {
          from: 'Old Town Market', to: 'Hotel', from_kind: 'attraction', to_kind: 'hotel',
          mode: 'metro', minutes: 20, label: HOP_LABEL,
        },
      ],
    }],
  }],
  // #189 — L3 guide-recommendation note, sibling of advisory[]/decisions/alert_tier
  risk_signals: {
    consolidator: 'risk_agent',
    per_leg: [{
      leg_id: 'leg-0', alert_tier: 'L3', decisions: { flag: true },
      planning_note: PLANNING_NOTE,
    }],
  },
  // #205 — orchestrator-pooled trip-level honesty advisories
  advisories: [ADVISORY_VISA, ADVISORY_HEALTH],
};

/** Pre-seed guest session and abort OSM tiles. */
async function seedGuestSession(page: import('@playwright/test').Page): Promise<void> {
  await page.addInitScript(() => {
    localStorage.setItem('tg_session', JSON.stringify({ user: null, guest: true, savedAt: Date.now() }));
  });
  await page.route('**/tile.openstreetmap.org/**', (route) => route.abort());
}

// ── Consolidated render-coverage sweep ───────────────────────────────────────
// All 5 fields are asserted with expect.soft() so a single run reports every
// field's pass/fail independently, instead of aborting at the first failure —
// this file IS the coverage sweep, so partial-pass visibility matters more than
// fail-fast here (assumption-notes.spec.ts's fixtures, by contrast, use hard
// expect() because they test one fully-shipped feature end-to-end).
test('consolidated honesty envelope: planning_note, day_plans notes, hop disambiguation, advisories, per-leg cost all reach the real DOM', async ({ page }) => {
  await seedGuestSession(page);

  await page.route('**/negotiate_text', (route) =>
    route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify(PLAN_WITH_HONESTY_FIELDS),
    })
  );

  await page.goto('/');
  await page.getByTestId('chat-input').fill('4 days in Kyoto, Apr 10-14 2027, budget $2000 USD');
  await page.getByRole('button', { name: 'Send' }).click();

  await expect(page.getByTestId('itinerary')).toBeVisible({ timeout: 8000 });

  // ── #190: day_plans[].notes[] — Itinerary.svelte, leg 0's header (leg is open by
  // default), no tab interaction required. ──────────────────────────────────────
  await test.step('#190 day_plans[].notes[] renders under the leg header', async () => {
    const notes = page.getByTestId('dayplan-notes-0');
    await expect.soft(notes).toBeVisible();
    await expect.soft(notes).toContainText(DAYPLAN_NOTE);
  });

  // ── #197: hop-endpoint disambiguation — two hotel-endpoint hops whose raw
  // server label is byte-identical must render as two DISTINCT DOM lines because
  // the component prefixes each with its own from/to. ───────────────────────────
  await test.step('#197 hotel-endpoint hop disambiguation renders two distinct lines', async () => {
    const hops = page.locator('.hop');
    await expect.soft(hops).toHaveCount(2);
    const [hop0, hop1] = await hops.allTextContents();
    expect.soft(hop0).not.toBe(hop1);
    expect.soft(hop0 ?? '').toContain('Riverside Park');
    expect.soft(hop1 ?? '').toContain('Old Town Market');
    expect.soft(hop0 ?? '').toContain(HOP_LABEL);
    expect.soft(hop1 ?? '').toContain(HOP_LABEL);
  });

  // ── #206: legs[].total_cents / cost_basis_label — Itinerary.svelte leg header. ─
  await test.step('#206 per-leg cost total + cost_basis_label render under the leg header', async () => {
    const legCostAmount = page.getByTestId('leg-cost-amount-0');
    await expect.soft(legCostAmount).toHaveText('$680.00');
    await expect.soft(page.getByTestId('leg-cost-basis-0')).toContainText(LEG_COST_BASIS_LABEL);
  });

  // Switch to the Safety tab (RightRail) for planning_note + advisories. Scoped to
  // right-rail: the header also has an unrelated "🌐 Safety Watch" badge button
  // (data-testid="safety-badge") whose accessible name also matches /Safety/.
  await page.getByTestId('right-rail').getByRole('button', { name: 'Safety' }).click();

  // ── #189: risk_signals.per_leg[].planning_note — RightRail Safety tab. ────────
  await test.step('#189 risk_signals.per_leg[].planning_note renders in the Safety tab', async () => {
    const note = page.getByTestId('planning-note');
    await expect.soft(note).toBeVisible();
    await expect.soft(note).toContainText(PLANNING_NOTE);
  });

  // ── #205: result.advisories[] — RightRail Safety tab, trip-level (above the
  // per-leg risk cards). ─────────────────────────────────────────────────────
  await test.step('#205 result.advisories[] renders every entry in the Safety tab', async () => {
    const advisories = page.getByTestId('advisory-item');
    await expect.soft(advisories).toHaveCount(2);
    const container = page.getByTestId('trip-advisories');
    await expect.soft(container).toContainText(ADVISORY_VISA);
    await expect.soft(container).toContainText(ADVISORY_HEALTH);
  });
});

// ── #203/#207: 403 session_invalid → actionable message via a real /refine ──────
// Drives the ACTUAL user action (typing a follow-up on a held plan, which App.svelte
// routes to /refine since activePlan.idempotency_key is set and uiState==='plan_ready')
// rather than hand-calling forbiddenMessage()/isForbidden() in isolation. Distinguishes
// the actionable SESSION_INVALID_MESSAGE re-login copy from the generic
// NOT_TRIP_OWNER_MESSAGE, which App.svelte renders for every other {outcome:'forbidden'}
// body regardless of `reason`.
test('403 forbidden reason:session_invalid shows the actionable re-login message via a real /refine action', async ({ page }) => {
  await seedGuestSession(page);

  await page.route('**/negotiate_text', (route) =>
    route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify(PLAN_WITH_HONESTY_FIELDS),
    })
  );
  await page.route('**/refine', (route) =>
    route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        outcome: 'forbidden',
        reason: 'session_invalid',
        idempotency_key: 'trip-hr-1',
      }),
    })
  );

  await page.goto('/');
  await page.getByTestId('chat-input').fill('4 days in Kyoto, Apr 10-14 2027, budget $2000 USD');
  await page.getByRole('button', { name: 'Send' }).click();
  await expect(page.getByTestId('itinerary')).toBeVisible({ timeout: 8000 });

  // Plan active → chat collapses to the floating 🤖 assistant (same pattern as
  // assumption-notes.spec.ts Fixture A) — open the flyout, then submit the
  // follow-up that triggers /refine on the held plan.
  await page.getByTestId('assistant-trigger').click();
  await expect(page.getByTestId('assistant-flyout')).toBeVisible({ timeout: 3000 });
  await page.getByTestId('chat-input').fill('Actually make it 5 nights instead.');
  await page.getByTestId('chat-input').press('Enter');

  await expect(page.getByTestId('chat-msgs')).toContainText(SESSION_INVALID_MESSAGE, { timeout: 8000 });
  // Regression guard: the OLD generic copy — which is honest-but-wrong for an
  // expired session (not an actual cross-user ownership violation) — must NOT
  // be what the user sees for this specific reason.
  await expect(page.getByTestId('chat-msgs')).not.toContainText(NOT_TRIP_OWNER_MESSAGE);
});
