// @vitest-environment jsdom
/// <reference types="@testing-library/jest-dom" />
//
// COMPONENT test — drag-drop edit-lane reorder → plan-only /replan.
// Mounts the real <Itinerary> in editable (plan_ready) mode and drives the HTML5
// drag-drop handlers, asserting:
//   • the drag→drop maps to the correct atomic remove+add /replan op pair (moveOps);
//   • a successful /replan dispatches `replanned` with the SERVER envelope (server is truth);
//   • the honest "Updated ✓" flash renders;
//   • a same-position drop is a no-op (NO network call — no fabricated change);
//   • a server `noop` shows the honest "Couldn't apply" flash and emits NOTHING.
// This is the unit/component counterpart to e2e/edit-lane.spec.ts (Playwright).

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, fireEvent, within } from '@testing-library/svelte';
import Itinerary from './components/Itinerary.svelte';
import type { Attraction } from './api';

// Mock ONLY the network seam; keep every pure helper (feeLabel, mealAnchoredTimeline…) real.
vi.mock('./api', async (orig) => {
  const actual = await (orig() as Promise<Record<string, unknown>>);
  return { ...actual, replanTrip: vi.fn() };
});
import { replanTrip } from './api';
const replanMock = replanTrip as unknown as ReturnType<typeof vi.fn>;

// ── fixture: one Kyoto day with two draggable activities ─────────────────────
const dayWith = (attractions: Attraction[]) => ({
  day_index: 0,
  bad_weather: false,
  attractions,
  meals: {},
});
const A = { name: 'Nishiki Market', category: 'tourism=attraction', lat: 35.005, lon: 135.765, fee: 0, weather_exposure: 'indoor' };
const B = { name: 'Fushimi Inari', category: 'tourism=attraction', lat: 34.967, lon: 135.772, fee: null, weather_exposure: 'outdoor' };

const PLANS = [{
  leg_id: 'leg-0', city: 'Kyoto', checkin: '2026-11-01', checkout: '2026-11-03', num_days: 2,
  days: [dayWith([A, B])],
}];
const LEGS = [{ leg_id: 'leg-0', city: 'Kyoto' }];

// Reordered plan the server returns after A is moved to the end (B now first).
const PLAN_AFTER = [{ ...PLANS[0], days: [dayWith([B, A])] }];

// A shared fake DataTransfer (jsdom has no real one). The same object must flow from
// dragstart → drop so the JSON payload set on dragstart is readable on drop.
function makeDataTransfer() {
  const store: Record<string, string> = {};
  return {
    setData: (k: string, v: string) => { store[k] = v; },
    getData: (k: string) => store[k] ?? '',
    setDragImage: () => {},
    effectAllowed: 'move',
    dropEffect: 'move',
  };
}

function mount() {
  const result = render(Itinerary, {
    props: { plans: PLANS, legs: LEGS, idempotencyKey: 'trip-reorder-1', editable: true },
  });
  const replanned = vi.fn();
  result.component.$on('replanned', (e: CustomEvent) => replanned(e.detail));
  return { ...result, replanned };
}

beforeEach(() => replanMock.mockReset());
afterEach(() => vi.restoreAllMocks());

describe('Itinerary edit-lane — drag-drop reorder', () => {
  it('renders draggable activity rows in editable mode', () => {
    const { getByTestId } = mount();
    const itin = getByTestId('itinerary');
    expect(within(itin).getByText('Nishiki Market')).toBeInTheDocument();
    expect(within(itin).getByText('Fushimi Inari')).toBeInTheDocument();
    // The end drop-zone exists only when editable.
    expect(getByTestId('drop-end-0-0')).toBeInTheDocument();
  });

  it('drag→drop-at-end maps to the correct remove+add op pair and dispatches replanned', async () => {
    replanMock.mockResolvedValue({
      outcome: 'plan_ready', idempotency_key: 'trip-reorder-1',
      applied: ['move:Nishiki Market'], rejected: [], notes: [],
      plan: { day_plans: PLAN_AFTER, legs: LEGS, idempotency_key: 'trip-reorder-1' },
    });
    const { getByTestId, replanned } = mount();
    const dt = makeDataTransfer();

    // Drag the first activity (raw index 0) ...
    const firstItem = getByTestId('item-activity-0');
    await fireEvent.dragStart(firstItem, { dataTransfer: dt });
    // ... and drop it at the END of the same day (dst raw index = 2).
    await fireEvent.drop(getByTestId('drop-end-0-0'), { dataTransfer: dt });

    // moveOps: same-day forward move → insert = dst_pos - 1 = 1.
    expect(replanMock).toHaveBeenCalledTimes(1);
    const [key, ops] = replanMock.mock.calls[0];
    expect(key).toBe('trip-reorder-1');
    expect(ops).toEqual([
      { op: 'remove_place', leg_index: 0, day_index: 0, position: 0, attraction_ref: 'Nishiki Market' },
      { op: 'add_place', leg_index: 0, day_index: 0, position: 1, attraction_ref: 'Nishiki Market', from: 'unscheduled' },
    ]);

    // The SERVER envelope (not a client-side guess) is what gets dispatched upward.
    expect(replanned).toHaveBeenCalledTimes(1);
    expect(replanned.mock.calls[0][0].day_plans).toEqual(PLAN_AFTER);

    // Honest success flash.
    const flash = getByTestId('replan-flash');
    expect(flash).toHaveTextContent('Updated');
  });

  it('same-position drop is a no-op: NO /replan call, no flash, no event', async () => {
    const { getByTestId, queryByTestId, replanned } = mount();
    const dt = makeDataTransfer();
    const firstItem = getByTestId('item-activity-0');
    await fireEvent.dragStart(firstItem, { dataTransfer: dt });
    // Drop on the drop-zone BEFORE itself (raw index 0) → src===dst → early return.
    await fireEvent.drop(getByTestId('drop-0-0-0'), { dataTransfer: dt });

    expect(replanMock).not.toHaveBeenCalled();
    expect(replanned).not.toHaveBeenCalled();
    expect(queryByTestId('replan-flash')).toBeNull();
  });

  it('server noop → honest "Couldn\'t apply" flash and NO replanned event (no fabrication)', async () => {
    replanMock.mockResolvedValue({
      outcome: 'noop', idempotency_key: 'trip-reorder-1',
      applied: [], rejected: [{ op: 'remove_place', reason: 'attraction not found at position' }],
      notes: [], plan: { day_plans: PLANS, legs: LEGS },
    });
    const { getByTestId, replanned } = mount();
    const dt = makeDataTransfer();
    await fireEvent.dragStart(getByTestId('item-activity-0'), { dataTransfer: dt });
    await fireEvent.drop(getByTestId('drop-end-0-0'), { dataTransfer: dt });

    expect(replanMock).toHaveBeenCalledTimes(1);
    // Honest decline — the plan is NOT swapped (no replanned emit).
    expect(replanned).not.toHaveBeenCalled();
    const flash = getByTestId('replan-flash');
    expect(flash).toHaveTextContent("Couldn't apply");
  });

  it('cross-city drop is refused with an honest flash and NO /replan call', async () => {
    // Two legs so a cross-leg drop is possible.
    const twoLeg = [
      PLANS[0],
      { leg_id: 'leg-1', city: 'Osaka', num_days: 1, days: [dayWith([{ ...A, name: 'Dotonbori' }])] },
    ];
    const result = render(Itinerary, {
      props: { plans: twoLeg, legs: [...LEGS, { leg_id: 'leg-1', city: 'Osaka' }], idempotencyKey: 'k', editable: true },
    });
    const dt = makeDataTransfer();
    // Drag from leg 0 (item-activity-1 is unique to leg 0; leg 1 only has index 0) ...
    await fireEvent.dragStart(result.getByTestId('item-activity-1'), { dataTransfer: dt });
    // ... drop on leg 1's day (drop-end-1-0).
    await fireEvent.drop(result.getByTestId('drop-end-1-0'), { dataTransfer: dt });

    expect(replanMock).not.toHaveBeenCalled();
    expect(result.getByTestId('replan-flash')).toHaveTextContent('between cities');
  });
});
