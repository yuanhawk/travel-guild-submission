// @vitest-environment jsdom
//
// COMPONENT test — B1 spillover (#168), item 1: RightRail's Suggested-tab cards.
//
// RightRail.svelte:131 used to dump `displayName(s.a)` as a raw string — same bypass
// pattern B1 fixed for lodging in Itinerary.svelte. This locks the equivalent honest
// name-tier treatment now applied via the SHARED namePresentation() (src/lib/itinerary.ts):
//  1. An ordinary Latin-name suggestion renders unchanged (no regression).
//  2. A non-Latin name WITH a name_en fallback shows the local-script companion.
//  3. A non-Latin name with NO name_en fallback shows the honest "unreadable primary"
//     indicator (never silently shown as unexplained unreadable text).
//  4. A `name_source` of 'wikidata'/'osm_name_en'/'romanized' shows the matching badge.

import { describe, it, expect } from 'vitest';
import { render } from '@testing-library/svelte';
import RightRail from './components/RightRail.svelte';
import type { NegotiateResult, Attraction } from './api';

const UNREADABLE_TEXT = 'shown in original script — no English name available';

function resultWithSuggestion(attraction: Attraction): NegotiateResult {
  return {
    outcome: 'success',
    legs: [{ leg_id: 'leg-0', city: 'Tokyo' }],
    day_plans: [{
      leg_id: 'leg-0', city: 'Tokyo', country: 'Japan', num_days: 1,
      days: [{ day_index: 0, bad_weather: false, attractions: [], meals: {} }],
      unscheduled_attractions: [attraction],
    }],
  } as unknown as NegotiateResult;
}

describe('#168 B1 spillover: RightRail Suggested-tab cards use namePresentation()', () => {
  it('an ordinary Latin-name suggestion renders unchanged, no unreadable indicator', () => {
    const result = resultWithSuggestion({ name: 'Sensoji Temple', category: 'tourism=attraction', lat: 1, lon: 1 });
    const { getByText, queryByText } = render(RightRail, { props: { result } });
    expect(getByText('Sensoji Temple')).toBeTruthy();
    expect(queryByText(UNREADABLE_TEXT)).toBeNull();
  });

  it('a non-Latin name WITH a name_en fallback shows the local-script companion, no unreadable indicator', () => {
    const result = resultWithSuggestion({
      name: '浅草寺', name_en: 'Sensoji Temple', category: 'tourism=attraction', lat: 1, lon: 1,
    });
    const { getByText, queryByText } = render(RightRail, { props: { result } });
    expect(getByText('Sensoji Temple')).toBeTruthy();
    expect(getByText('浅草寺')).toBeTruthy();
    expect(queryByText(UNREADABLE_TEXT)).toBeNull();
  });

  it('a non-Latin-only name with no name_en fallback shows the honest unreadable indicator', () => {
    const result = resultWithSuggestion({ name: '浅草寺', category: 'tourism=attraction', lat: 1, lon: 1 });
    const { getByText } = render(RightRail, { props: { result } });
    expect(getByText('浅草寺')).toBeTruthy(); // rendered AS the primary (no fallback exists)
    expect(getByText(UNREADABLE_TEXT)).toBeTruthy();
  });

  it('shows the verified badge when name_source is osm_name_en', () => {
    const result = resultWithSuggestion({
      name: 'Sensoji Temple', name_source: 'osm_name_en', category: 'tourism=attraction', lat: 1, lon: 1,
    } as Attraction);
    const { getByText } = render(RightRail, { props: { result } });
    expect(getByText('verified')).toBeTruthy();
  });

  it('shows the romanized badge when name_source is romanized', () => {
    const result = resultWithSuggestion({
      name: 'Sensoji Temple', name_source: 'romanized', category: 'tourism=attraction', lat: 1, lon: 1,
    } as Attraction);
    const { getByText } = render(RightRail, { props: { result } });
    expect(getByText('romanized')).toBeTruthy();
  });
});
