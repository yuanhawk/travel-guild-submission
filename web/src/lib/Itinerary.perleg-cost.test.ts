// @vitest-environment jsdom
//
// COMPONENT test — #206: per-leg lodging cost breakdown (legs[].total_cents /
// .cost_basis_label), invisible before this fix.
//
// Task #200's render-coverage sweep flagged legs[].total_cents as a served field
// that's computed per-leg but never rendered anywhere: for a multi-leg trip, the
// UI only ever showed one combined package total, so users couldn't see the
// per-city cost split. Real golden-fixture example (singapore_bangkok_10d_family):
// Singapore total_cents=68000 ($680.00) vs Bangkok total_cents=85000 ($850.00),
// folded into a single package_total_cents=153000 ($1,530.00) aggregate with no
// per-leg breakdown visible anywhere. This fixes the leg header to render each
// leg's OWN total_cents (via the existing centsToUsd() formatter) and, when
// present, cost_basis_label as a dim `.ic-caveat` honesty caveat — mirroring the
// established transport_pricing.basis_label rendering pattern.

import { describe, it, expect } from 'vitest';
import { render } from '@testing-library/svelte';
import Itinerary from './components/Itinerary.svelte';
import type { DayPlan, Leg } from './api';

const basePlan = (legId: string, city: string) => ({
  leg_id: legId, city, country: 'Testland', num_days: 1,
  days: [{ day_index: 0, bad_weather: false, attractions: [], meals: {} }],
});

describe('#206: legs[].total_cents / cost_basis_label render under the leg header', () => {
  it('a multi-leg trip shows each leg its OWN cost, not the aggregate (regression guard)', () => {
    const legs = [
      { leg_id: 'leg-0', city: 'Singapore', total_cents: 68000, cost_basis: 'ucp_prepaid', cost_basis_label: 'Prepaid via secure checkout' },
      { leg_id: 'leg-1', city: 'Bangkok', total_cents: 85000, cost_basis: 'ucp_prepaid', cost_basis_label: 'Prepaid via secure checkout' },
    ] as unknown as Leg[];
    const plans = [basePlan('leg-0', 'Singapore'), basePlan('leg-1', 'Bangkok')] as unknown as DayPlan[];

    const { getByTestId } = render(Itinerary, {
      props: { plans, legs, idempotencyKey: '', editable: true },
    });

    const leg0Amount = getByTestId('leg-cost-amount-0').textContent;
    const leg1Amount = getByTestId('leg-cost-amount-1').textContent;

    expect(leg0Amount).toBe('$680.00');
    expect(leg1Amount).toBe('$850.00');
    // regression guard: the two legs must NOT show the same (e.g. aggregate) total
    expect(leg0Amount).not.toBe(leg1Amount);
  });

  it('a leg with cost_basis_label present renders the label', () => {
    const legs = [
      { leg_id: 'leg-0', city: 'Singapore', total_cents: 68000, cost_basis: 'ucp_prepaid', cost_basis_label: 'Prepaid via secure checkout' },
    ] as unknown as Leg[];
    const plans = [basePlan('leg-0', 'Singapore')] as unknown as DayPlan[];

    const { getByTestId, getByText } = render(Itinerary, {
      props: { plans, legs, idempotencyKey: '', editable: true },
    });

    expect(getByTestId('leg-cost-basis-0')).toBeTruthy();
    expect(getByText('Prepaid via secure checkout')).toBeTruthy();
  });

  it('a leg missing total_cents renders nothing for the leg cost (no fabricated $0/placeholder)', () => {
    const legs = [
      { leg_id: 'leg-0', city: 'Singapore' },
    ] as unknown as Leg[];
    const plans = [basePlan('leg-0', 'Singapore')] as unknown as DayPlan[];

    const { queryByTestId } = render(Itinerary, {
      props: { plans, legs, idempotencyKey: '', editable: true },
    });

    expect(queryByTestId('leg-cost-0')).toBeNull();
    expect(queryByTestId('leg-cost-amount-0')).toBeNull();
    expect(queryByTestId('leg-cost-basis-0')).toBeNull();
  });
});
