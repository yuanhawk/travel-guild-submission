// @vitest-environment jsdom
//
// COMPONENT test — #205: result.advisories[] (society/orchestration/orchestrator.py
// ~L2118-2164). The orchestrator pools compliance flagged-leg advisories, health
// flagged-destination advisories, risk per-leg advisory detail text, AND
// day-planner honesty notes into a single flat result["advisories"] list[str] —
// per the code comment, "a single result['advisories'] list the front end must
// show." It was never rendered anywhere. This locks the fix in the Safety tab:
//  1. A non-empty advisories[] renders every entry visibly, not just the first.
//  2. An absent/empty advisories[] renders no container and no fabricated text.
//  3. Distinct advisory "kinds" (visa/compliance, health, flood/risk, day-planner
//     honesty note) each render sensibly — the field is a plain string with no
//     kind/category metadata, so this just confirms varied real-world text renders.

import { describe, it, expect } from 'vitest';
import { render, fireEvent } from '@testing-library/svelte';
import RightRail from './components/RightRail.svelte';
import type { NegotiateResult } from './api';

const VISA_ADVISORY =
  'Entry requirements not verified for this nationality/destination pair — confirm visa ' +
  'eligibility with the destination government before travel.';

const HEALTH_ADVISORY =
  'Required vaccination status not verified — confirm yellow fever certificate requirements ' +
  'before travel.';

const RISK_ADVISORY = 'Elevated flood risk reported for this leg; monitor local conditions.';

const DAYPLAN_ADVISORY =
  'no restaurant matching requested cuisine for some meal slot; fell back to the best ' +
  'available venue (not fabricated)';

function resultWithAdvisories(advisories?: string[]): NegotiateResult {
  return {
    outcome: 'success',
    legs: [{ leg_id: 'leg-0', city: 'Lagos' }],
    advisories,
  } as unknown as NegotiateResult;
}

async function openSafetyTab(getByText: (t: string) => HTMLElement) {
  await fireEvent.click(getByText('Safety'));
}

describe('#205: Safety tab renders result.advisories[] (honesty disclosures)', () => {
  it('a non-empty advisories[] renders every entry, not just the first', async () => {
    const result = resultWithAdvisories([VISA_ADVISORY, HEALTH_ADVISORY, RISK_ADVISORY]);
    const { getByTestId, getAllByTestId, getByText } = render(RightRail, { props: { result } });
    await openSafetyTab(getByText);
    expect(getByTestId('trip-advisories')).toBeTruthy();
    expect(getAllByTestId('advisory-item')).toHaveLength(3);
    expect(getByText(VISA_ADVISORY)).toBeTruthy();
    expect(getByText(HEALTH_ADVISORY)).toBeTruthy();
    expect(getByText(RISK_ADVISORY)).toBeTruthy();
  });

  it('an absent advisories[] renders no container and no fabricated text', async () => {
    const result = resultWithAdvisories(undefined);
    const { queryByTestId, getByText } = render(RightRail, { props: { result } });
    await openSafetyTab(getByText);
    expect(queryByTestId('trip-advisories')).toBeNull();
  });

  it('an empty advisories[] renders no container', async () => {
    const result = resultWithAdvisories([]);
    const { queryByTestId, getByText } = render(RightRail, { props: { result } });
    await openSafetyTab(getByText);
    expect(queryByTestId('trip-advisories')).toBeNull();
  });

  it('a visa/compliance-kind advisory renders sensibly', async () => {
    const result = resultWithAdvisories([VISA_ADVISORY]);
    const { getByText } = render(RightRail, { props: { result } });
    await openSafetyTab(getByText);
    expect(getByText(VISA_ADVISORY)).toBeTruthy();
  });

  it('a health-kind advisory renders sensibly', async () => {
    const result = resultWithAdvisories([HEALTH_ADVISORY]);
    const { getByText } = render(RightRail, { props: { result } });
    await openSafetyTab(getByText);
    expect(getByText(HEALTH_ADVISORY)).toBeTruthy();
  });

  it('a flood/risk-kind advisory renders sensibly', async () => {
    const result = resultWithAdvisories([RISK_ADVISORY]);
    const { getByText } = render(RightRail, { props: { result } });
    await openSafetyTab(getByText);
    expect(getByText(RISK_ADVISORY)).toBeTruthy();
  });

  it('a day-planner honesty-note-kind advisory renders sensibly', async () => {
    const result = resultWithAdvisories([DAYPLAN_ADVISORY]);
    const { getByText } = render(RightRail, { props: { result } });
    await openSafetyTab(getByText);
    expect(getByText(/fell back to the best available venue \(not fabricated\)/)).toBeTruthy();
  });
});
