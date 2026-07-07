// @vitest-environment jsdom
//
// COMPONENT test — #163 locate-pin card-open wiring.
//
// Bug 1 fix: locateItem() now carries {name, city, category} on mapFocus (not just
// lat/lng/label) so Map.svelte can open the SAME place-detail card a direct pin
// click opens. This test asserts the kind → category mapping at the source
// (Itinerary.svelte's locateItem), independent of Map.svelte's consumption of it
// (covered separately in map.test.ts's "mapFocus opens the place card" suite).

import { describe, it, expect, beforeEach } from 'vitest';
import { render, fireEvent } from '@testing-library/svelte';
import { get } from 'svelte/store';
import Itinerary from './components/Itinerary.svelte';
import { mapFocus } from './mapStore';
import type { DayPlan } from './api';

const LEGS = [{ leg_id: 'leg-0', city: 'Kyoto' }];

function makePlan(): DayPlan[] {
  return [{
    leg_id: 'leg-0',
    city: 'Kyoto',
    num_days: 1,
    days: [{
      day_index: 0,
      bad_weather: false,
      attractions: [
        { name: 'Fushimi Inari', category: 'tourism=shrine', lat: 34.9671, lon: 135.7727, fee: null },
      ],
      meals: {
        breakfast: { name: 'Morning Cafe', cuisine: 'coffee', lat: 35.01, lon: 135.76 },
      },
    }],
  }] as unknown as DayPlan[];
}

beforeEach(() => mapFocus.set(null));

describe('Itinerary locate button (#163) — mapFocus carries name/city/category', () => {
  it('an activity item locates with category "attraction"', async () => {
    const { getByTestId } = render(Itinerary, {
      props: { plans: makePlan(), legs: LEGS, idempotencyKey: '' },
    });
    // Timeline order: breakfast (ii=0), then the activity (ii=1).
    await fireEvent.click(getByTestId('locate-0-0-1'));

    const focus = get(mapFocus);
    expect(focus).toMatchObject({
      name: 'Fushimi Inari',
      city: 'Kyoto',
      category: 'attraction',
      lat: 34.9671,
      lng: 135.7727,
    });
  });

  it('a meal item locates with category "restaurant"', async () => {
    const { getByTestId } = render(Itinerary, {
      props: { plans: makePlan(), legs: LEGS, idempotencyKey: '' },
    });
    await fireEvent.click(getByTestId('locate-0-0-0')); // breakfast

    const focus = get(mapFocus);
    expect(focus).toMatchObject({
      name: 'Morning Cafe',
      city: 'Kyoto',
      category: 'restaurant',
      lat: 35.01,
      lng: 135.76,
    });
  });
});

// Finding #3 (map-pin-bug sweep): curated seed entries can carry null lat/lon
// (society/poi_supplement.json) — the locate button used to render as if it
// were live and silently no-op on click. It must now be disabled and NOT
// mutate mapFocus.
function makePlanNoCoords(): DayPlan[] {
  return [{
    leg_id: 'leg-0',
    city: 'Lisbon',
    num_days: 1,
    days: [{
      day_index: 0,
      bad_weather: false,
      attractions: [
        { name: 'Belem Tower', category: 'tourism=monument', lat: null, lon: null, fee: null },
      ],
      meals: {},
    }],
  }] as unknown as DayPlan[];
}

describe('Itinerary locate button — disabled honest-degradation when coords are missing', () => {
  it('renders the locate button disabled for a no-coords item', () => {
    const { getByTestId } = render(Itinerary, {
      props: { plans: makePlanNoCoords(), legs: [{ leg_id: 'leg-0', city: 'Lisbon' }], idempotencyKey: '' },
    });
    const btn = getByTestId('locate-0-0-0') as HTMLButtonElement;
    expect(btn.disabled).toBe(true);
    expect(btn.title).toMatch(/not available/i);
  });

  it('clicking a disabled/no-coords locate button does not set mapFocus', async () => {
    const { getByTestId } = render(Itinerary, {
      props: { plans: makePlanNoCoords(), legs: [{ leg_id: 'leg-0', city: 'Lisbon' }], idempotencyKey: '' },
    });
    await fireEvent.click(getByTestId('locate-0-0-0'));
    expect(get(mapFocus)).toBeNull();
  });
});
