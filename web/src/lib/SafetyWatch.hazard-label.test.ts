// @vitest-environment jsdom
//
// The Global Safety Watch overlay rendered EmergencyCountry.hazard verbatim — a raw served
// snake_case key (e.g. "tropical_cyclone", from emergency_feed.py's
// _GDACS_HAZARD) — with no text-transform/humanizer, unlike the per-leg
// advisory chip (RightRail.svelte) which already routes its category through
// prettyCategory(). This locks the fix: the overlay must humanize c.hazard too.

import { describe, it, expect } from 'vitest';
import { render } from '@testing-library/svelte';
import SafetyWatch from './components/SafetyWatch.svelte';
import type { EmergenciesResponse } from './api';

describe('Global Safety Watch overlay humanizes hazard type (no raw snake_case keys)', () => {
  it('renders "tropical_cyclone" as "Tropical Cyclone", not the raw key', async () => {
    const data: EmergenciesResponse = {
      status: 'ok',
      source: 'GDACS',
      as_of: '2026-07-06',
      countries: [
        { iso2: 'PK', hazard: 'tropical_cyclone', severity: 'high', headline: 'Active cyclone warning' },
      ],
    };
    render(SafetyWatch, { props: { data, open: true } });
    const card = document.querySelector('.card .t');
    expect(card?.textContent).toContain('Tropical Cyclone');
    expect(card?.textContent).not.toContain('tropical_cyclone');
  });

  it('renders a monitoring-tier hazard humanized as well', async () => {
    const data: EmergenciesResponse = {
      status: 'ok',
      countries: [
        { iso2: 'JP', hazard: 'seismic_activity', severity: 'monitoring' },
      ],
    };
    render(SafetyWatch, { props: { data, open: true } });
    const card = document.querySelector('.card .t');
    expect(card?.textContent).toContain('Seismic Activity');
    expect(card?.textContent).not.toContain('seismic_activity');
  });
});
