// @vitest-environment jsdom
//
// COMPONENT test — inter-city transport connector rendering.
//
// Mounts the real <Itinerary> with hermetic fixtures that carry `transport_edges`
// (the server-supplied inter-city routing data) and asserts:
//   1. A feasible rail edge → connector renders with mode icon, route, duration,
//      HSR chip, and the transport_pricing caveat between the two legs.
//   2. An infeasible edge → honest reason text is shown (not silently hidden).
//   3. Fallback → when transport_edges is absent but leg.inbound_transport is set,
//      the fallback ribbon renders.
//   4. No edges, no inbound_transport → minimal "Onward" cue renders.
//
// Modelled after src/lib/Itinerary.reorder.test.ts (vitest + @testing-library/svelte).
// Network is NOT mocked because these tests exercise pure rendering — no /replan calls.

import { describe, it, expect } from 'vitest';
import { render, within } from '@testing-library/svelte';
import Itinerary from './components/Itinerary.svelte';
import type { TransportEdge, TransportPricing } from './api';

// ── shared day fixture (minimal — only needs enough for Itinerary to render) ──
const DAY = {
  day_index: 0,
  bad_weather: false,
  attractions: [{ name: 'Senso-ji Temple', category: 'tourism=attraction', lat: 35.71, lon: 139.79, fee: null }],
  meals: { dinner: { name: 'Sushi Dai', category: 'amenity=restaurant', cuisine: 'sushi' } },
};

// ── 2-leg plan_ready fixture ─────────────────────────────────────────────────
const PLANS_2LEG = [
  {
    leg_id: 'leg-0', city: 'Tokyo', checkin: '2026-11-01', checkout: '2026-11-04', num_days: 3,
    days: [DAY],
  },
  {
    leg_id: 'leg-1', city: 'Kyoto', checkin: '2026-11-04', checkout: '2026-11-07', num_days: 3,
    days: [{ day_index: 0, bad_weather: false, attractions: [], meals: {} }],
  },
];
const LEGS_2LEG = [
  { leg_id: 'leg-0', city: 'Tokyo' },
  { leg_id: 'leg-1', city: 'Kyoto' },
];

// ── transport_edges fixtures ─────────────────────────────────────────────────
const EDGE_RAIL_FEASIBLE: TransportEdge = {
  from_leg: 'leg-0', to_leg: 'leg-1',
  from_city: 'Tokyo', to_city: 'Kyoto',
  feasible: true,
  mode: 'rail',
  transfer_minutes: 115,   // ~1h 55m
  rail_tier: 'hsr',
};

const EDGE_INFEASIBLE: TransportEdge = {
  from_leg: 'leg-0', to_leg: 'leg-1',
  from_city: 'Tokyo', to_city: 'Kyoto',
  feasible: false,
  mode: 'flight',
  transfer_minutes: 75,
  reason: 'no land route — flight or ferry required',
};

const PRICING: TransportPricing = {
  basis: 'handoff',
  basis_label: 'Durations estimated (OpenFlights ~2014 vintage); fares not priced — book externally',
};

// ── helpers ──────────────────────────────────────────────────────────────────
function mountWith(overrides: {
  transport_edges?: TransportEdge[];
  transport_pricing?: TransportPricing | null;
  legs?: typeof LEGS_2LEG;
  plans?: typeof PLANS_2LEG;
}) {
  const { transport_edges = [], transport_pricing = null,
          legs = LEGS_2LEG, plans = PLANS_2LEG } = overrides;
  return render(Itinerary, {
    props: { plans, legs, idempotencyKey: 'trip-tc-1', editable: false,
             transport_edges, transport_pricing },
  });
}

// ── tests ────────────────────────────────────────────────────────────────────
describe('Itinerary — inter-city transport connector', () => {

  it('feasible rail edge → connector renders mode icon, route, duration, HSR chip', () => {
    const { getByTestId } = mountWith({ transport_edges: [EDGE_RAIL_FEASIBLE], transport_pricing: PRICING });

    // The connector block exists between leg-0 and leg-1 (li=0 → testid suffix "0")
    const connector = getByTestId('intercity-connector-0');
    expect(connector).toBeTruthy();

    // Mode icon span shows the rail emoji
    const modeSpan = getByTestId('intercity-mode-0');
    expect(modeSpan.textContent).toContain('🚆');

    // Duration is rendered correctly (115 min → ~1h 55m)
    const dur = getByTestId('intercity-duration-0');
    expect(dur.textContent).toBe('~1h 55m');

    // HSR chip is visible
    const hsr = getByTestId('intercity-hsr-0');
    expect(hsr.textContent).toBe('HSR');

    // Route label contains both city names
    const pill = getByTestId('intercity-pill-0');
    expect(pill.textContent).toContain('Tokyo');
    expect(pill.textContent).toContain('Kyoto');

    // transport_pricing caveat is shown
    const caveat = getByTestId('intercity-caveat-0');
    expect(caveat.textContent).toContain('fares not priced');
  });

  it('infeasible edge → honest reason text is rendered', () => {
    const { getByTestId, queryByTestId } = mountWith({ transport_edges: [EDGE_INFEASIBLE], transport_pricing: PRICING });

    // The connector block still exists
    const connector = getByTestId('intercity-connector-0');
    expect(connector).toBeTruthy();

    // The infeasibility warning is shown with the honest reason
    const infeasible = getByTestId('intercity-infeasible-0');
    expect(infeasible.textContent).toContain('no land route');
    expect(infeasible.textContent).toContain('flight or ferry required');

    // Flight mode icon renders (✈️)
    const modeSpan = getByTestId('intercity-mode-0');
    expect(modeSpan.textContent).toContain('✈️');

    // HSR chip must NOT be shown (mode is flight, not hsr rail)
    expect(queryByTestId('intercity-hsr-0')).toBeNull();
  });

  it('no transport_edges + leg.inbound_transport → fallback ribbon renders', () => {
    const legsWithFallback = [
      LEGS_2LEG[0],
      { ...LEGS_2LEG[1], inbound_transport: 'Tokyo → Kyoto · rail · ~1h55m' },
    ];
    const { getByTestId } = mountWith({ transport_edges: [], legs: legsWithFallback });

    const connector = getByTestId('intercity-connector-0');
    expect(connector.textContent).toContain('Tokyo → Kyoto');
  });

  it('no transport_edges, no inbound_transport → minimal "Onward" cue renders', () => {
    const { getByTestId } = mountWith({ transport_edges: [] });

    const connector = getByTestId('intercity-connector-0');
    // Falls through to the onward ribbon
    expect(connector.textContent).toContain('Kyoto');
  });

  it('single-leg plan → no connector rendered (nothing between legs)', () => {
    const singlePlan = [PLANS_2LEG[0]];
    const singleLegs = [LEGS_2LEG[0]];
    const { queryByTestId } = mountWith({
      transport_edges: [EDGE_RAIL_FEASIBLE],
      plans: singlePlan,
      legs: singleLegs,
    });

    // No connector on a single-leg trip
    expect(queryByTestId('intercity-connector-0')).toBeNull();
  });
});
