// @vitest-environment jsdom
//
// COMPONENT test — review follow-up alongside assumption-notes:
// risk_agent serves `planning_note` as a top-level sibling of advisory[]/decisions/
// alert_tier on each per-leg risk signal — the actual L3/L4 guide-recommendation
// text that implements "FLAG + guide recommendation, not a hard block." The Safety
// tab never rendered it. This locks the fix:
//  1. An L3 leg with a planning_note renders the note visibly.
//  2. A leg with no planning_note (null/absent) renders nothing extra — no empty
//     line, no literal "null", no fabricated guide-recommendation text.
//  3. An L4 leg with the armed-conflict-specific note text renders correctly too.

import { describe, it, expect } from 'vitest';
import { render, fireEvent } from '@testing-library/svelte';
import RightRail from './components/RightRail.svelte';
import type { NegotiateResult, RiskSignal } from './api';

const L3_NOTE =
  'US State Dept Level 3 (Reconsider Travel) — travel is generally manageable ' +
  'with advance planning and a reputable local guide. Avoid remote and border areas.';

const L4_WAR_NOTE =
  'US State Dept Level 4 (Do Not Travel) — insurance coverage is not extended ' +
  'to armed conflict zones (EXC-WAR-2). This destination is outside current booking scope.';

function resultWithRisk(risk: RiskSignal, city = 'Karachi'): NegotiateResult {
  return {
    outcome: 'success',
    legs: [{ leg_id: risk.leg_id ?? 'leg-0', city }],
    risk_signals: {
      consolidator: 'risk_agent',
      per_leg: [risk],
    },
  } as unknown as NegotiateResult;
}

async function openSafetyTab(getByText: (t: string) => HTMLElement) {
  await fireEvent.click(getByText('Safety'));
}

describe('Safety tab renders planning_note (guide-recommendation text)', () => {
  it('an L3 leg with a planning_note renders the note visibly', async () => {
    const result = resultWithRisk({
      leg_id: 'leg-0',
      alert_tier: 'L3',
      decisions: { flag: true },
      planning_note: L3_NOTE,
    });
    const { getByTestId, getByText } = render(RightRail, { props: { result } });
    await openSafetyTab(getByText);
    expect(getByTestId('tab-safety')).toBeTruthy();
    expect(getByText(L3_NOTE)).toBeTruthy();
  });

  it('a leg with no planning_note renders nothing extra — no empty line or fabricated text', async () => {
    const result = resultWithRisk({
      leg_id: 'leg-0',
      alert_tier: 'LOW',
    });
    const { queryByTestId, getByText } = render(RightRail, { props: { result } });
    await openSafetyTab(getByText);
    expect(queryByTestId('planning-note')).toBeNull();
  });

  it('an L4 leg with the armed-conflict-specific note renders correctly', async () => {
    const result = resultWithRisk({
      leg_id: 'leg-0',
      alert_tier: 'L4',
      decisions: { flag: true },
      planning_note: L4_WAR_NOTE,
    });
    const { getByText, getByTestId } = render(RightRail, { props: { result } });
    await openSafetyTab(getByText);
    expect(getByTestId('planning-note')).toBeTruthy();
    expect(getByText(L4_WAR_NOTE)).toBeTruthy();
  });
});

// Finding #6 (map/mobile UX sweep): risk_signals.per_leg[].advisory[].type is a raw
// served snake_case key (e.g. "median_delay", "seismic_resilience") — the Safety tab
// used to render it verbatim ("Median_delay· Info"), violating the #201 user-facing
// message-register bar (no raw server keys in copy). Fixed via prettyCategory(), the
// SAME humanizer already used for suggestion-card categories.
describe('Safety tab humanizes advisory[].type (no raw snake_case keys)', () => {
  it('renders "median_delay" as "Median Delay", not the raw key', async () => {
    const result = resultWithRisk({
      leg_id: 'leg-0',
      alert_tier: 'MED',
      advisory: [{ type: 'median_delay', severity: 'info', detail: '' }],
    });
    const { getByTestId, getByText } = render(RightRail, { props: { result } });
    await openSafetyTab(getByText);
    const chip = getByTestId('advisory');
    expect(chip.textContent).toContain('Median Delay');
    expect(chip.textContent).not.toContain('median_delay');
  });

  it('renders "seismic_resilience" as "Seismic Resilience"', async () => {
    const result = resultWithRisk({
      leg_id: 'leg-0',
      alert_tier: 'MED',
      advisory: [{ type: 'seismic_resilience', severity: 'info', detail: '' }],
    });
    const { getByTestId, getByText } = render(RightRail, { props: { result } });
    await openSafetyTab(getByText);
    expect(getByTestId('advisory').textContent).toContain('Seismic Resilience');
  });

  it('puts a real space before the "·" severity separator (not trimmed to "Median Delay· info")', async () => {
    const result = resultWithRisk({
      leg_id: 'leg-0',
      alert_tier: 'MED',
      advisory: [{ type: 'median_delay', severity: 'info', detail: '' }],
    });
    const { getByTestId, getByText } = render(RightRail, { props: { result } });
    await openSafetyTab(getByText);
    expect(getByTestId('advisory').textContent).toContain('Median Delay · info');
  });
});

// orchestrator.py's D2 block lifts every risk_signals.per_leg[].advisory[].detail verbatim into the
// trip-level result.advisories[] "so a flag is never buried inside risk_signals" —
// but the Safety tab then rendered BOTH the trip-level list AND the per-leg detail,
// so every advisory paragraph appeared twice (the "wall of text" in the footage).
describe('Safety tab does not duplicate advisories already shown per-leg', () => {
  it('drops a trip-level advisory whose text exactly matches a per-leg advisory detail', async () => {
    const detail = 'No seeded median-delay data for region pk; delay is UNKNOWN.';
    const result: NegotiateResult = {
      outcome: 'success',
      legs: [{ leg_id: 'leg-0', city: 'Islamabad' }],
      advisories: [detail],
      risk_signals: {
        consolidator: 'risk_agent',
        per_leg: [{
          leg_id: 'leg-0',
          alert_tier: 'MED',
          advisory: [{ type: 'median_delay', severity: 'info', detail }],
        }],
      },
    } as unknown as NegotiateResult;
    const { queryByTestId, getByText, getAllByText } = render(RightRail, { props: { result } });
    await openSafetyTab(getByText);
    // the per-leg detail still renders once (inside the leg-risk card)...
    expect(getAllByText(detail)).toHaveLength(1);
    // ...but the trip-level "Advisories" block itself must not appear, since its
    // only entry was the duplicate.
    expect(queryByTestId('trip-advisories')).toBeNull();
  });

  it('still shows a trip-level advisory that has no per-leg counterpart (e.g. a compliance/health note)', async () => {
    const compliance = 'Visa on arrival available for this nationality; confirm before departure.';
    const perLegDetail = 'No seeded median-delay data for region pk; delay is UNKNOWN.';
    const result: NegotiateResult = {
      outcome: 'success',
      legs: [{ leg_id: 'leg-0', city: 'Islamabad' }],
      advisories: [compliance, perLegDetail],
      risk_signals: {
        consolidator: 'risk_agent',
        per_leg: [{
          leg_id: 'leg-0',
          alert_tier: 'MED',
          advisory: [{ type: 'median_delay', severity: 'info', detail: perLegDetail }],
        }],
      },
    } as unknown as NegotiateResult;
    const { getByTestId, getByText, getAllByText } = render(RightRail, { props: { result } });
    await openSafetyTab(getByText);
    expect(getByTestId('trip-advisories')).toBeTruthy();
    expect(getAllByText(compliance)).toHaveLength(1);
    // the duplicated one is still filtered even though a non-duplicate is present
    expect(getAllByText(perLegDetail)).toHaveLength(1);
  });
});
