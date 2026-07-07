// @vitest-environment jsdom
//
// COMPONENT test — #168 honest name-tier fix (B1).
//
// Two things locked here:
//  1. Lodging (`leg.hotel_title`) is now routed through the SAME namePresentation()
//     pipeline as activities/meals instead of being dumped as a raw string — a plain
//     Latin hotel name renders unchanged (no behaviour regression), a non-Latin-only
//     hotel name (the only field the catalog has — no name_en split exists server-side
//     yet, see report) gets the new honest "unreadable primary" indicator.
//  2. The new "unreadable primary" indicator itself: fires ONLY when the displayed
//     primary name is non-Latin script AND there's no name_en fallback at all (an
//     English-reading user would otherwise see unexplained unreadable text). It must
//     NOT fire when name_en exists (local-script companion case) and must NOT fire for
//     ordinary Latin names.

import { describe, it, expect } from 'vitest';
import { render } from '@testing-library/svelte';
import Itinerary from './components/Itinerary.svelte';
import type { DayPlan, Leg } from './api';

const UNREADABLE_TEXT = 'shown in original script — no English name available';

function planWithHotel(hotelTitle: string): { plans: DayPlan[]; legs: Leg[] } {
  const plans = [{
    leg_id: 'leg-0',
    city: 'Tokyo',
    country: 'Japan',
    num_days: 1,
    days: [{ day_index: 0, bad_weather: false, attractions: [], meals: {} }],
  }] as unknown as DayPlan[];
  const legs = [{ leg_id: 'leg-0', city: 'Tokyo', hotel_title: hotelTitle }] as unknown as Leg[];
  return { plans, legs };
}

function planWithActivity(attraction: Record<string, unknown>): DayPlan[] {
  return [{
    leg_id: 'leg-0',
    city: 'Tokyo',
    country: 'Japan',
    num_days: 1,
    days: [{ day_index: 0, bad_weather: false, attractions: [attraction], meals: {} }],
  }] as unknown as DayPlan[];
}

describe('#168 lodging routed through namePresentation() instead of a raw string dump', () => {
  it('a plain Latin hotel_title renders unchanged, no unreadable indicator', () => {
    const { plans, legs } = planWithHotel('Park Hyatt Tokyo');
    const { getByTestId, queryByText } = render(Itinerary, {
      props: { plans, legs, idempotencyKey: '' },
    });
    const hotelName = getByTestId('hotel-name-0');
    expect(hotelName.textContent).toContain('Park Hyatt Tokyo');
    expect(queryByText(UNREADABLE_TEXT)).toBeNull();
  });

  it('a non-Latin-only hotel_title (no name_en available server-side) shows the honest unreadable indicator', () => {
    // Real catalog row (ucp-merchant/catalog_supplement.json, id "akishima-akishima-a") —
    // fully non-Latin, no English fallback available anywhere in the record.
    const { plans, legs } = planWithHotel('東京昭島迎賓館a');
    const { getByTestId, getByText } = render(Itinerary, {
      props: { plans, legs, idempotencyKey: '' },
    });
    const hotelName = getByTestId('hotel-name-0');
    expect(hotelName.textContent).toContain('東京昭島迎賓館');
    expect(getByText(UNREADABLE_TEXT)).toBeTruthy();
  });
});

describe('#168 "unreadable primary" indicator — new, data-driven, never fabricated', () => {
  it('does NOT fire for an ordinary Latin-name activity', () => {
    const plans = planWithActivity({ name: 'Fushimi Inari', category: 'tourism=shrine', lat: 1, lon: 1 });
    const { queryByText } = render(Itinerary, { props: { plans, legs: [{ leg_id: 'leg-0', city: 'Tokyo' }] as unknown as Leg[], idempotencyKey: '' } });
    expect(queryByText(UNREADABLE_TEXT)).toBeNull();
  });

  it('does NOT fire when a non-Latin name has a name_en fallback (local-script-companion case instead)', () => {
    const plans = planWithActivity({ name: '伏見稲荷大社', name_en: 'Fushimi Inari Taisha', category: 'tourism=shrine', lat: 1, lon: 1 });
    const { queryByText, getByText } = render(Itinerary, { props: { plans, legs: [{ leg_id: 'leg-0', city: 'Tokyo' }] as unknown as Leg[], idempotencyKey: '' } });
    expect(queryByText(UNREADABLE_TEXT)).toBeNull();
    // the existing local-script companion still renders
    expect(getByText('伏見稲荷大社')).toBeTruthy();
  });

  it('DOES fire when an activity has only a non-Latin name and no name_en at all', () => {
    const plans = planWithActivity({ name: '伏見稲荷大社', category: 'tourism=shrine', lat: 1, lon: 1 });
    const { getByText } = render(Itinerary, { props: { plans, legs: [{ leg_id: 'leg-0', city: 'Tokyo' }] as unknown as Leg[], idempotencyKey: '' } });
    expect(getByText('伏見稲荷大社')).toBeTruthy(); // rendered AS the primary (no fallback exists)
    expect(getByText(UNREADABLE_TEXT)).toBeTruthy();
  });
});
