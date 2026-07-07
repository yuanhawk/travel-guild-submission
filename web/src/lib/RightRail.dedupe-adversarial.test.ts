// @vitest-environment jsdom
//
// Adversarial edge-case locks for the dedupedAdvisories fix (adversarial
// hunt follow-up, 2026-07-06). Verified against the actual backend templates:
//  - ADV-1: risk_agent detail templates all embed {region}, so identical details
//    only occur for SAME-region multi-leg trips — D2 then lifts the string once
//    per leg; both lifted copies must be dropped, per-leg copies still render.
//  - ADV-2: the dedupe is deliberately byte-exact. orchestrator.py's D2 block
//    appends advisory[].detail VERBATIM (no trim/prefix/truncate) and
//    _attach_risk_signals shares the same per_leg objects, so a near-duplicate
//    (whitespace/period/nbsp variant) cannot occur today; these tests document
//    that IF the backend ever starts transforming lifted text, the duplicate
//    quietly reappears (fail here first, fix the lift or normalize both sides).
//  - ADV-3: empty/whitespace-only details never false-match unrelated entries
//    (the Set builder filters falsy; backend truthiness guards can't emit '').
//  - ADV-4: contracts.py stale_provenance_note() feeds BOTH compliance/health
//    flag_advisory (lifted trip-level) AND risk_agent's per-leg stale_data
//    detail. The strings only collide if source+fetched_at+ttl all coincide —
//    impossible today because source namespaces are disjoint (seed:risk-* /
//    seed:society-region-profile-* vs seed:immigration.* vs seed:cdc.gov-*).
//    Test 1 documents the eat-on-collision semantics; test 2 locks the real
//    disjoint-source case (both advisories render).

import { describe, it, expect } from 'vitest';
import { render, fireEvent } from '@testing-library/svelte';
import RightRail from './components/RightRail.svelte';
import type { NegotiateResult } from './api';

async function openSafetyTab(getByText: (t: string) => HTMLElement) {
  await fireEvent.click(getByText('Safety'));
}

function count(container: HTMLElement, sel: string, text: string): number {
  return Array.from(container.querySelectorAll(sel)).filter(
    (el) => el.textContent === text
  ).length;
}

describe('ADV-1: multi-leg same-region identical details', () => {
  it('2 legs in region jp, identical detail lifted twice into advisories', async () => {
    const D =
      'No seeded median-delay data for region jp; delay is UNKNOWN. Buffering ' +
      'connections conservatively by 45 min (≥45 min floor) — never silently ' +
      'assume on-time. Advisory-only (data-coverage gap, not a known hazard).';
    const result = {
      outcome: 'success',
      legs: [
        { leg_id: 'leg-0', city: 'Tokyo' },
        { leg_id: 'leg-1', city: 'Osaka' },
      ],
      advisories: [D, D], // D2 lifts each leg's detail verbatim -> twice
      risk_signals: {
        consolidator: 'risk-agent',
        per_leg: [
          { leg_id: 'leg-0', alert_tier: 'MED',
            advisory: [{ type: 'median_delay', severity: 'info', detail: D }] },
          { leg_id: 'leg-1', alert_tier: 'MED',
            advisory: [{ type: 'median_delay', severity: 'info', detail: D }] },
        ],
      },
    } as unknown as NegotiateResult;
    const { container, queryByTestId, getByText } = render(RightRail, { props: { result } });
    await openSafetyTab(getByText);
    // trip block fully suppressed (both lifted copies match a per-leg detail)
    expect(queryByTestId('trip-advisories')).toBeNull();
    // per-leg detail still visible once per leg card
    expect(count(container, '.adv-detail', D)).toBe(2);
  });
});

describe('ADV-2: byte-difference variants are NOT deduped (exact-match contract)', () => {
  const BASE = 'Standing civil-unrest advisory ~4% for region th (protests/riots). Flag and check coverage exclusions.';
  const variants: Array<[string, string]> = [
    ['trailing space', BASE + ' '],
    ['leading space', ' ' + BASE],
    ['nbsp for space', BASE.replace(' advisory ', ' advisory\u00a0')],
    ['trailing period added', BASE + '.'],
  ];
  for (const [name, tripVariant] of variants) {
    it(`variant "${name}" reappears in the trip block (duplicate visible)`, async () => {
      const result = {
        outcome: 'success',
        legs: [{ leg_id: 'leg-0', city: 'Bangkok' }],
        advisories: [tripVariant],
        risk_signals: {
          consolidator: 'risk-agent',
          per_leg: [
            { leg_id: 'leg-0', alert_tier: 'MED',
              advisory: [{ type: 'civil_unrest', severity: 'medium', detail: BASE }] },
          ],
        },
      } as unknown as NegotiateResult;
      const { queryByTestId, getByText } = render(RightRail, { props: { result } });
      await openSafetyTab(getByText);
      // Characterize: does the near-duplicate render in the trip block?
      const trip = queryByTestId('trip-advisories');
      expect(trip).not.toBeNull();
      expect(trip!.textContent).toContain(tripVariant.trim());
    });
  }
});

describe('ADV-3: empty / whitespace-only details', () => {
  it('per-leg detail "" does not eat an unrelated empty-ish trip advisory; empty trip entries render as blank items', async () => {
    const result = {
      outcome: 'success',
      legs: [{ leg_id: 'leg-0', city: 'Lagos' }],
      advisories: ['', 'Real compliance note: confirm visa eligibility before travel.'],
      risk_signals: {
        consolidator: 'risk-agent',
        per_leg: [
          { leg_id: 'leg-0', alert_tier: 'LOW',
            advisory: [{ type: 'median_delay', severity: 'info', detail: '' }] },
        ],
      },
    } as unknown as NegotiateResult;
    const { getByTestId, getAllByTestId, getByText } = render(RightRail, { props: { result } });
    await openSafetyTab(getByText);
    expect(getByTestId('trip-advisories')).toBeTruthy();
    const items = getAllByTestId('advisory-item');
    // characterize: how many items render, and does '' produce a blank row?
    expect(items.map((i) => i.textContent)).toEqual([
      '',
      'Real compliance note: confirm visa eligibility before travel.',
    ]);
  });

  it('whitespace-only per-leg detail " " deduplicates only an identical " " trip entry', async () => {
    const result = {
      outcome: 'success',
      legs: [{ leg_id: 'leg-0', city: 'Lagos' }],
      advisories: [' ', 'Keep me.'],
      risk_signals: {
        consolidator: 'risk-agent',
        per_leg: [
          { leg_id: 'leg-0', alert_tier: 'LOW',
            advisory: [{ type: 'median_delay', severity: 'info', detail: ' ' }] },
        ],
      },
    } as unknown as NegotiateResult;
    const { getAllByTestId, getByText } = render(RightRail, { props: { result } });
    await openSafetyTab(getByText);
    expect(getAllByTestId('advisory-item').map((i) => i.textContent)).toEqual(['Keep me.']);
  });
});

describe('ADV-4: stale-note template collision (compliance flag_advisory vs risk stale_data)', () => {
  it('identical stale_provenance_note strings: compliance advisory is EATEN by the risk per-leg match', async () => {
    // contracts.py stale_provenance_note(source, fetched_at, ttl) — same fn feeds
    // BOTH compliance flag_advisory (lifted to result.advisories) AND risk_agent's
    // per-leg stale_data detail. Collision requires identical source strings.
    const S =
      'This data (source: seed:shared-source-2026, fetched 2026-01-01) is past its ' +
      '180-day freshness window — it may be stale. Please reconfirm before travel.';
    const result = {
      outcome: 'success',
      legs: [{ leg_id: 'leg-0', city: 'Addis Ababa' }],
      advisories: [S], // conceptually the COMPLIANCE stale advisory
      risk_signals: {
        consolidator: 'risk-agent',
        per_leg: [
          { leg_id: 'leg-0', alert_tier: 'MED',
            advisory: [{ type: 'stale_data', severity: 'flag', detail: S }] },
        ],
      },
    } as unknown as NegotiateResult;
    const { container, queryByTestId, getByText } = render(RightRail, { props: { result } });
    await openSafetyTab(getByText);
    // characterize: trip block suppressed, string appears only once (per-leg)
    expect(queryByTestId('trip-advisories')).toBeNull();
    expect(count(container, '.adv-detail', S)).toBe(1);
  });

  it('distinct sources (the real backend case): both render', async () => {
    const compliance =
      'This data (source: seed:immigration.govt.nz-NZeTA-2026, fetched 2026-01-01) is past its ' +
      '180-day freshness window — it may be stale. Please reconfirm before travel.';
    const risk =
      'This data (source: seed:society-region-profile-2026, fetched 2026-01-01) is past its ' +
      '180-day freshness window — it may be stale. Please reconfirm before travel.';
    const result = {
      outcome: 'success',
      legs: [{ leg_id: 'leg-0', city: 'Auckland' }],
      advisories: [compliance, risk],
      risk_signals: {
        consolidator: 'risk-agent',
        per_leg: [
          { leg_id: 'leg-0', alert_tier: 'MED',
            advisory: [{ type: 'stale_data', severity: 'flag', detail: risk }] },
        ],
      },
    } as unknown as NegotiateResult;
    const { container, getByTestId, getByText } = render(RightRail, { props: { result } });
    await openSafetyTab(getByText);
    expect(getByTestId('trip-advisories')).toBeTruthy();
    expect(count(container, '.adv-detail', compliance)).toBe(1);
    expect(count(container, '.adv-detail', risk)).toBe(1); // per-leg only
  });
});
