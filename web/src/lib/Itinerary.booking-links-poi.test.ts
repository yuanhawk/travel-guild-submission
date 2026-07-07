// @vitest-environment jsdom
//
// COMPONENT test — #212: per-attraction/per-meal booking_links (day_plans[].days[]
// .attractions[].link / .meals.{slot}.link).
//
// #211 (branch feat/booking-links-trip-leg, PR #33 — not yet merged) rendered the
// LOW-cardinality trip/leg-level booking_links block (lodging/transport/visa/health/
// insurance) in RightRail.svelte's Safety tab. This task covers the remaining
// HIGH-cardinality per-entity portion: attraction_link()/restaurant_link() output
// mutated in place onto each attraction/meal entity (society/utils/booking_links.py
// build_booking_links). Real observed density (singapore_bangkok_10d_family fixture):
// 26 attraction links + 45 meal links in one trip — far too many for a full label+
// button per item, so this renders a COMPACT icon-only "↗" affordance (mirroring
// #211's RightRail.svelte glyph choice) appended to each attraction/meal row, only
// when a genuinely safe .link.booking_url is present. The link's own `.label` (the
// prose honesty caveat, e.g. "Official site — … (not a confirmed booking)") is
// carried as the title/aria-label tooltip since there's no room for full text at
// this density.

import { describe, it, expect } from 'vitest';
import { render } from '@testing-library/svelte';
import Itinerary from './components/Itinerary.svelte';
import type { DayPlan, Leg } from './api';

const LEGS = [{ leg_id: 'leg-0', city: 'Singapore' }] as unknown as Leg[];

const REF_LINK = {
  booking_url: 'https://www.wikidata.org/wiki/Q123',
  kind: 'reference',
  label: 'Reference — Gardens by the Bay (Wikidata)',
  providers: null,
  provenance: { source: 'booking_links: attraction wikidata reference (constructed)', tier: 'seeded' },
};

const MEAL_LINK = {
  booking_url: 'https://alchemy.example.com',
  kind: 'official_site',
  label: 'Official site — Alchemy',
  providers: null,
  provenance: { source: 'booking_links: restaurant official_site (constructed)', tier: 'seeded' },
};

function planWith(attractions: unknown[], meals: Record<string, unknown>): DayPlan[] {
  return [{
    leg_id: 'leg-0', city: 'Singapore', country: 'Singapore', num_days: 1,
    days: [{ day_index: 0, bad_weather: false, attractions, meals }],
  }] as unknown as DayPlan[];
}

describe('#212: day_plans[].days[].attractions[].link / meals.*.link render as a compact icon affordance', () => {
  it('multiple attractions AND meals carrying .link all render their icon, not just the first', () => {
    const plans = planWith(
      [
        { name: 'Gardens by the Bay', name_en: 'Gardens by the Bay', link: REF_LINK },
        { name: 'Marina Bay Sands', name_en: 'Marina Bay Sands', link: { ...REF_LINK, booking_url: 'https://www.wikidata.org/wiki/Q456', label: 'Reference — Marina Bay Sands (Wikidata)' } },
      ],
      {
        breakfast: { name: 'Alchemy', name_en: 'Alchemy', link: MEAL_LINK },
        dinner: { name: 'Odette', name_en: 'Odette', link: { ...MEAL_LINK, booking_url: 'https://odette.example.com', label: 'Official site — Odette' } },
      },
    );

    const { getAllByTestId } = render(Itinerary, {
      props: { plans, legs: LEGS, idempotencyKey: '', editable: true },
    });

    // 2 attraction icons + 2 meal icons = 4, none of them collapsed/deduped to one.
    const icons = getAllByTestId(/^poi-link-/);
    expect(icons.length).toBe(4);
  });

  it('an attraction/meal with NO .link renders no icon for it, no fabrication', () => {
    const plans = planWith(
      [
        { name: 'Gardens by the Bay', name_en: 'Gardens by the Bay', link: REF_LINK },
        { name: 'Free Walking Tour', name_en: 'Free Walking Tour' }, // no link field at all
      ],
      {
        breakfast: { name: 'Alchemy', name_en: 'Alchemy' }, // no link
        dinner: { name: 'Odette', name_en: 'Odette', link: MEAL_LINK },
      },
    );

    const { getAllByTestId, queryByTestId } = render(Itinerary, {
      props: { plans, legs: LEGS, idempotencyKey: '', editable: true },
    });

    // Only the 2 items that genuinely carry .link get the icon.
    const icons = getAllByTestId(/^poi-link-/);
    expect(icons.length).toBe(2);
    // The specific per-item testids for the linkless items must be absent.
    // item ordering: Breakfast(ii=0, no link), Gardens(ii=1, link), Free Walking Tour(ii=2, no link), Dinner(ii=3, link)
    expect(queryByTestId('poi-link-0-0-0')).toBeNull();
  });

  it('an .link object with NO booking_url renders no icon (label alone is not enough)', () => {
    const plans = planWith(
      [{ name: 'Compare-note attraction', name_en: 'Compare-note attraction', link: { booking_url: null, kind: 'compare_note', label: 'Compare independently' } }],
      {},
    );
    const { queryByTestId } = render(Itinerary, {
      props: { plans, legs: LEGS, idempotencyKey: '', editable: true },
    });
    expect(queryByTestId(/^poi-link-/)).toBeNull();
  });

  it('safeHref() security guard: a javascript: booking_url never becomes a clickable anchor', () => {
    const plans = planWith(
      [{ name: 'Malicious POI', name_en: 'Malicious POI', link: { booking_url: 'javascript:alert(1)', kind: 'maps', label: 'Find on Google Maps' } }],
      {},
    );
    const { container, queryByTestId } = render(Itinerary, {
      props: { plans, legs: LEGS, idempotencyKey: '', editable: true },
    });
    expect(queryByTestId(/^poi-link-/)).toBeNull();
    // Belt-and-braces: no anchor anywhere in the render carries the raw javascript: URL.
    const anchors = Array.from(container.querySelectorAll('a'));
    expect(anchors.some((a) => a.getAttribute('href')?.startsWith('javascript:'))).toBe(false);
  });

  it('the icon carries the link label as its title/aria-label tooltip (no room for prose at this density)', () => {
    const plans = planWith(
      [{ name: 'Gardens by the Bay', name_en: 'Gardens by the Bay', link: REF_LINK }],
      {},
    );
    const { getByTestId } = render(Itinerary, {
      props: { plans, legs: LEGS, idempotencyKey: '', editable: true },
    });
    const anchor = getByTestId(/^poi-link-/) as unknown as HTMLAnchorElement;
    expect(anchor.getAttribute('title')).toBe(REF_LINK.label);
    expect(anchor.getAttribute('aria-label')).toBe(REF_LINK.label);
    expect(anchor.getAttribute('href')).toBe(REF_LINK.booking_url);
    expect(anchor.getAttribute('target')).toBe('_blank');
    expect(anchor.getAttribute('rel')).toBe('noopener noreferrer');
  });

  it('scope guard: exactly one icon per linked item, no double-render (distinct from #211s trip/leg-level block)', () => {
    // #211 (unmerged) renders result.booking_links.{lodging,transport,visa,health,insurance}
    // inside RightRail.svelte's Safety tab, from a `result` prop RightRail alone consumes.
    // Itinerary.svelte (this component) never takes a `result`/`booking_links` prop and
    // never reads anything but the per-entity attractions[].link/meals.*.link fields —
    // so there is no shared render surface for the two tasks to collide on.
    const plans = planWith(
      [{ name: 'Gardens by the Bay', name_en: 'Gardens by the Bay', link: REF_LINK }],
      {},
    );
    const { getAllByTestId } = render(Itinerary, {
      props: { plans, legs: LEGS, idempotencyKey: '', editable: true },
    });
    expect(getAllByTestId(/^poi-link-/).length).toBe(1);
  });
});
