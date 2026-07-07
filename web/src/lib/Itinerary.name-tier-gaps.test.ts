// @vitest-environment jsdom
//
// COMPONENT test — #181 (G1/G2): honest name-tier spillover fixes.
//
// #168 wired namePresentation() into the main timeline (Itinerary.name-tier.test.ts)
// and RightRail's Suggested tab (RightRail.name-tier.test.ts), but two more render
// sites inside Itinerary.svelte kept bypassing it and rendering raw served names:
//  G1: per-day suggestion chips (`.day-sug-chip`, from dp.unscheduled_attractions)
//      used `{displayName(ua)}` — a plain fallback with no local-companion / badge /
//      unreadable-primary treatment.
//  G2: meal-swap panel alternatives (`.msp-chip`, from day.meal_pool[slot]) used the
//      RAW `{alt.name}` directly — worse than G1, since it didn't even fall back to
//      displayName(), so a served name_en was silently dropped even when present.

import { describe, it, expect } from 'vitest';
import { render, fireEvent } from '@testing-library/svelte';
import Itinerary from './components/Itinerary.svelte';
import type { DayPlan, Leg } from './api';

const UNREADABLE_TEXT = 'shown in original script — no English name available';
const LEGS = [{ leg_id: 'leg-0', city: 'Kyoto' }] as unknown as Leg[];

describe('G1 (#181): per-day suggestion chips (.day-sug-chip) honor the name tier', () => {
  const basePlan = {
    leg_id: 'leg-0', city: 'Kyoto', country: 'Japan', num_days: 1,
    days: [{ day_index: 0, bad_weather: false, attractions: [], meals: {} }],
  };

  it('non-Latin-only suggestion (no name_en) shows the unreadable indicator', () => {
    const plans = [{
      ...basePlan,
      unscheduled_attractions: [{ name: '錦市場', category: 'tourism=attraction', lat: 35.0, lon: 135.7 }],
    }] as unknown as DayPlan[];
    const { getByText } = render(Itinerary, { props: { plans, legs: LEGS, idempotencyKey: '', editable: true } });
    expect(getByText('錦市場')).toBeTruthy();
    expect(getByText(UNREADABLE_TEXT)).toBeTruthy();
  });

  it('suggestion with name_en renders the English primary (no unreadable indicator)', () => {
    const plans = [{
      ...basePlan,
      unscheduled_attractions: [{ name: '錦市場', name_en: 'Nishiki Market', category: 'tourism=attraction', lat: 35, lon: 135.7 }],
    }] as unknown as DayPlan[];
    const { getByText, queryByText } = render(Itinerary, { props: { plans, legs: LEGS, idempotencyKey: '', editable: true } });
    expect(getByText('Nishiki Market')).toBeTruthy();
    expect(queryByText(UNREADABLE_TEXT)).toBeNull();
    // the local-script companion still renders alongside the English primary
    expect(getByText('錦市場')).toBeTruthy();
  });

  it('an ordinary Latin-only suggestion renders unchanged (no regression)', () => {
    const plans = [{
      ...basePlan,
      unscheduled_attractions: [{ name: 'Nijo Castle', category: 'tourism=castle', lat: 35, lon: 135.7 }],
    }] as unknown as DayPlan[];
    const { getByText, queryByText } = render(Itinerary, { props: { plans, legs: LEGS, idempotencyKey: '', editable: true } });
    expect(getByText('Nijo Castle')).toBeTruthy();
    expect(queryByText(UNREADABLE_TEXT)).toBeNull();
  });
});

describe('G2 (#181): meal-swap panel alternatives (.msp-chip) use the name-tier pipeline', () => {
  function planWithLunchPool(pool: Array<Record<string, unknown>>): DayPlan[] {
    return [{
      leg_id: 'leg-0', city: 'Kyoto', country: 'Japan', num_days: 1,
      days: [{
        day_index: 0, bad_weather: false, attractions: [],
        meals: { lunch: { name: 'Ramen Counter', category: 'amenity=restaurant', cuisine: 'ramen', lat: 35, lon: 135.7 } },
        // meal_pool is keyed by the display-cased TimelineItem.slot ('Lunch', per
        // itinerary.ts MEAL_SLOTS), NOT the lowercase Meals key ('lunch') — see
        // Itinerary.svelte getMealPool() / e2e/guide-cards.spec.ts N-03.
        meal_pool: { Lunch: pool },
      }],
    }] as unknown as DayPlan[];
  }

  it('a pool alt with name_en shows the English name (not silently dropped) plus the local companion', async () => {
    const plans = planWithLunchPool([{ name: '一蘭', name_en: 'Ichiran', cuisine: 'ramen' }]);
    const { getByTestId, getByText } = render(Itinerary, {
      props: { plans, legs: LEGS, idempotencyKey: '', editable: true },
    });
    await fireEvent.click(getByTestId('meal-edit-0-0-0')); // lunch is the only timeline item -> ii=0
    expect(getByText('Ichiran')).toBeTruthy();
    expect(getByText('一蘭')).toBeTruthy();
  });

  it('a pool alt with only a non-Latin name (no name_en) shows the unreadable indicator', async () => {
    const plans = planWithLunchPool([{ name: '一蘭' }]);
    const { getByTestId, getByText } = render(Itinerary, {
      props: { plans, legs: LEGS, idempotencyKey: '', editable: true },
    });
    await fireEvent.click(getByTestId('meal-edit-0-0-0'));
    expect(getByText('一蘭')).toBeTruthy();
    expect(getByText(UNREADABLE_TEXT)).toBeTruthy();
  });

  it('an ordinary Latin-only pool alt renders unchanged (no regression)', async () => {
    const plans = planWithLunchPool([{ name: 'Ichiran Ramen', cuisine: 'ramen' }]);
    const { getByTestId, getByText, queryByText } = render(Itinerary, {
      props: { plans, legs: LEGS, idempotencyKey: '', editable: true },
    });
    await fireEvent.click(getByTestId('meal-edit-0-0-0'));
    expect(getByText('Ichiran Ramen')).toBeTruthy();
    expect(queryByText(UNREADABLE_TEXT)).toBeNull();
  });
});
