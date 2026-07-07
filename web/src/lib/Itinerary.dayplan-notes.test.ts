// @vitest-environment jsdom
//
// COMPONENT test — #190: day-planner honesty disclosures (day_plans[].notes[]).
//
// The backend's day-planner (society/agents/day_planner_agent.py ~L855-876) appends
// per-leg honesty notes covering cuisine-fallback, supper-left-empty, and
// meal-chain-cap-reached cases — always with an explicit "(not fabricated)" qualifier
// in the text itself. DayPlan.notes was already typed on the frontend (api.ts) but
// never rendered anywhere persistent — only the transient replan-response notes flash
// was shown. This fixes the itinerary leg header to render dp.notes[] as a dim,
// non-alarming caveat list (`.dayplan-notes`), matching the "hole-free itinerary,
// honestly flag every slot" quality bar.

import { describe, it, expect } from 'vitest';
import { render } from '@testing-library/svelte';
import Itinerary from './components/Itinerary.svelte';
import type { DayPlan, Leg } from './api';

const LEGS = [{ leg_id: 'leg-0', city: 'Kyoto' }] as unknown as Leg[];

const basePlan = {
  leg_id: 'leg-0', city: 'Kyoto', country: 'Japan', num_days: 1,
  days: [{ day_index: 0, bad_weather: false, attractions: [], meals: {} }],
};

describe('#190: day_plans[].notes[] honesty disclosures render under the leg header', () => {
  it('a leg with a non-empty notes array renders the note visibly', () => {
    const plans = [{
      ...basePlan,
      notes: ['no restaurant matching requested cuisine for some meal slot; fell back to the best available venue (not fabricated)'],
    }] as unknown as DayPlan[];
    const { getByTestId, getByText } = render(Itinerary, {
      props: { plans, legs: LEGS, idempotencyKey: '', editable: true },
    });
    expect(getByTestId('dayplan-notes-0')).toBeTruthy();
    expect(getByText(/fell back to the best available venue \(not fabricated\)/)).toBeTruthy();
  });

  it('a leg with an empty notes array renders no container and no fabricated text', () => {
    const plans = [{ ...basePlan, notes: [] }] as unknown as DayPlan[];
    const { queryByTestId } = render(Itinerary, {
      props: { plans, legs: LEGS, idempotencyKey: '', editable: true },
    });
    expect(queryByTestId('dayplan-notes-0')).toBeNull();
  });

  it('a leg with notes absent (undefined) renders no container', () => {
    const plans = [{ ...basePlan }] as unknown as DayPlan[];
    delete (plans[0] as Record<string, unknown>).notes;
    const { queryByTestId } = render(Itinerary, {
      props: { plans, legs: LEGS, idempotencyKey: '', editable: true },
    });
    expect(queryByTestId('dayplan-notes-0')).toBeNull();
  });

  it('multiple notes on the same leg all render, not just the first', () => {
    const plans = [{
      ...basePlan,
      notes: [
        'no late-opening venue known for supper on day(s) 2; supper left empty (not fabricated)',
        'meal-chain cap reached for lunch on day(s) 1; repeated the best available venue (not fabricated)',
      ],
    }] as unknown as DayPlan[];
    const { getByText } = render(Itinerary, {
      props: { plans, legs: LEGS, idempotencyKey: '', editable: true },
    });
    expect(getByText(/supper left empty \(not fabricated\)/)).toBeTruthy();
    expect(getByText(/repeated the best available venue \(not fabricated\)/)).toBeTruthy();
  });
});
