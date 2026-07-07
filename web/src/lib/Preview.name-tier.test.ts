// @vitest-environment jsdom
//
// COMPONENT test — #181 (G3): Preview.svelte (trip-summary / save-to-phone export)
// wires the SAME honest name-tier pipeline (namePresentation()) into its timeline
// rows as the main itinerary view. Before this fix, `<span class="tname">{it.name}</span>`
// used the already-displayName()-collapsed TimelineItem.name with no local-companion
// or unreadable-primary indicator — a user exporting/saving their itinerary would see
// a DIFFERENT (less honest) name presentation than the one they saw while planning.

import { describe, it, expect } from 'vitest';
import { render } from '@testing-library/svelte';
import Preview from './components/Preview.svelte';
import type { NegotiateResult, DayPlan } from './api';

const UNREADABLE_TEXT = 'shown in original script — no English name available';

function resultWithActivity(attraction: Record<string, unknown>): NegotiateResult {
  const day_plans = [{
    leg_id: 'leg-0', city: 'Kyoto', country: 'Japan', checkin: '2026-08-01', num_days: 1,
    days: [{ day_index: 0, bad_weather: false, attractions: [attraction], meals: {} }],
  }] as unknown as DayPlan[];
  return { day_plans, legs: [{ leg_id: 'leg-0', city: 'Kyoto', checkin: '2026-08-01' }] } as unknown as NegotiateResult;
}

describe('G3 (#181): Preview.svelte trip-summary timeline honors the name tier', () => {
  it('an activity with only a non-Latin name (no name_en) shows the unreadable indicator', () => {
    const result = resultWithActivity({ name: '伏見稲荷大社', category: 'tourism=shrine', lat: 1, lon: 1 });
    const { getByText } = render(Preview, { props: { result } });
    expect(getByText('伏見稲荷大社')).toBeTruthy();
    expect(getByText(UNREADABLE_TEXT)).toBeTruthy();
  });

  it('an activity with name_en shows the English primary + local-script companion, no unreadable indicator', () => {
    const result = resultWithActivity({ name: '伏見稲荷大社', name_en: 'Fushimi Inari Taisha', category: 'tourism=shrine', lat: 1, lon: 1 });
    const { getByText, queryByText } = render(Preview, { props: { result } });
    expect(getByText('Fushimi Inari Taisha')).toBeTruthy();
    expect(getByText('伏見稲荷大社')).toBeTruthy();
    expect(queryByText(UNREADABLE_TEXT)).toBeNull();
  });

  it('an ordinary Latin-name activity renders unchanged (no regression)', () => {
    const result = resultWithActivity({ name: 'Nijo Castle', category: 'tourism=castle', lat: 1, lon: 1 });
    const { getByText, queryByText } = render(Preview, { props: { result } });
    expect(getByText('Nijo Castle')).toBeTruthy();
    expect(queryByText(UNREADABLE_TEXT)).toBeNull();
  });
});
