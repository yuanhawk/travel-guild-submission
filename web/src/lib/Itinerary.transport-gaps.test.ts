// @vitest-environment jsdom
//
// COMPONENT test — inline transport gap chips (UI increment #3).
//
// Asserts:
//   • chips render from item_transport_gaps (pure presentation, server data)
//   • null entries suppress the chip entirely (honest gap — no fabricated output)
//   • absent item_transport_gaps renders no chips (backward compat; older plans)
//   • recomputing class toggles on all chips whenever busy !== null (visual feedback
//     during an in-flight /replan; chips never recompute — they just pulse)
//
// PURE PRESENTATION contract: the component NEVER performs distance/time math.
// All chip content is rendered verbatim from the server-supplied item_transport_gaps.

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, fireEvent } from '@testing-library/svelte';
import Itinerary from './components/Itinerary.svelte';
import type { DayPlan } from './api';

// Mock only the network seam; pure helpers (mealAnchoredTimeline, fmtMinutes…) stay real.
vi.mock('./api', async (orig) => {
  const actual = await (orig() as Promise<Record<string, unknown>>);
  return { ...actual, replanTrip: vi.fn() };
});
import { replanTrip } from './api';
const replanMock = replanTrip as unknown as ReturnType<typeof vi.fn>;

// ── fixtures ─────────────────────────────────────────────────────────────────

const LEGS = [{ leg_id: 'leg-0', city: 'Kyoto' }];

function makePlans(item_transport_gaps: (Record<string, unknown> | null)[] | undefined): DayPlan[] {
  return [{
    leg_id: 'leg-0',
    city: 'Kyoto',
    num_days: 1,
    days: [{
      day_index: 0,
      bad_weather: false,
      attractions: [
        { name: 'Temple A', category: 'tourism=temple', lat: 35.005, lon: 135.765, fee: null },
        { name: 'Museum B', category: 'tourism=museum', lat: 34.99, lon: 135.77, fee: 0 },
      ],
      meals: {
        breakfast: { name: 'Morning Cafe', cuisine: 'coffee', lat: 35.01, lon: 135.76 },
        lunch: { name: 'Noodle Bar', cuisine: 'ramen', lat: 35.0, lon: 135.76 },
      },
      item_transport_gaps,
    }],
  // Test fixtures use Record<string,unknown> for gaps; type-assert to the declared DayPlan.
  }] as unknown as DayPlan[];
}

beforeEach(() => replanMock.mockReset());
afterEach(() => vi.restoreAllMocks());

// ── tests ─────────────────────────────────────────────────────────────────────

describe('Itinerary inline transport gap chips (UI #3)', () => {
  it('renders chips from item_transport_gaps (pure presentation, server data)', () => {
    // Timeline has 4 items (breakfast, att1, att2, lunch) → 3 gaps.
    // We supply 3 gaps: walk(10), metro(15), null.
    const gaps = [
      { mode: 'walk', minutes: 10, estimate: true },
      { mode: 'metro', minutes: 15, estimate: true },
      null, // honest suppression — either endpoint lacked coords
    ];
    const { queryAllByTestId } = render(Itinerary, {
      props: { plans: makePlans(gaps), legs: LEGS, idempotencyKey: '' },
    });
    // 2 non-null gaps → 2 chips
    const chips = queryAllByTestId(/^transport-chip-/);
    expect(chips).toHaveLength(2);
    expect(chips[0].querySelector('.chip-mode')?.textContent).toBe('walk');
    expect(chips[1].querySelector('.chip-mode')?.textContent).toBe('metro');
  });

  it('renders duration text via fmtMinutes (e.g. ~10m, ~1h 5m)', () => {
    const gaps = [
      { mode: 'walk', minutes: 10, estimate: true },
      { mode: 'taxi', minutes: 65, estimate: true },
    ];
    const { queryAllByTestId } = render(Itinerary, {
      props: { plans: makePlans(gaps), legs: LEGS, idempotencyKey: '' },
    });
    const chips = queryAllByTestId(/^transport-chip-/);
    expect(chips[0].querySelector('.chip-dur')?.textContent).toBe('~10m');
    expect(chips[1].querySelector('.chip-dur')?.textContent).toBe('~1h 5m');
  });

  it('always shows "est." caveat on every rendered chip', () => {
    const gaps = [
      { mode: 'metro', minutes: 15, estimate: true },
      { mode: 'taxi', minutes: 25, estimate: true },
    ];
    const { queryAllByTestId } = render(Itinerary, {
      props: { plans: makePlans(gaps), legs: LEGS, idempotencyKey: '' },
    });
    for (const chip of queryAllByTestId(/^transport-chip-/)) {
      expect(chip.querySelector('.chip-est')?.textContent?.trim()).toBe('est.');
    }
  });

  it('null entries suppress chips entirely (honest gap — no fabricated output)', () => {
    const allNull = [null, null, null];
    const { queryAllByTestId } = render(Itinerary, {
      props: { plans: makePlans(allNull), legs: LEGS, idempotencyKey: '' },
    });
    // No transport-gap wrappers and no chips
    expect(queryAllByTestId(/^transport-gap-/)).toHaveLength(0);
    expect(queryAllByTestId(/^transport-chip-/)).toHaveLength(0);
  });

  it('absent item_transport_gaps renders no chips (backward compat for older plans)', () => {
    const { queryAllByTestId } = render(Itinerary, {
      props: { plans: makePlans(undefined), legs: LEGS, idempotencyKey: '' },
    });
    expect(queryAllByTestId(/^transport-chip-/)).toHaveLength(0);
  });

  it('chip has mode-specific CSS class matching the served mode', () => {
    const gaps = [
      { mode: 'walk', minutes: 5, estimate: true },
      { mode: 'metro', minutes: 15, estimate: true },
      { mode: 'taxi', minutes: 20, estimate: true },
    ];
    // Need 4 timeline items to get 3 gaps — add a tea meal via meals override.
    const plan = [{
      leg_id: 'leg-0', city: 'Kyoto', num_days: 1,
      days: [{
        day_index: 0, bad_weather: false,
        attractions: [
          { name: 'Att A', lat: 35.005, lon: 135.765, fee: null },
          { name: 'Att B', lat: 34.99, lon: 135.77, fee: 0 },
        ],
        meals: {
          breakfast: { name: 'Cafe', lat: 35.01, lon: 135.76 },
          lunch: { name: 'Bar', lat: 35.0, lon: 135.76 },
        },
        item_transport_gaps: gaps,
      }],
    }];
    const { queryAllByTestId } = render(Itinerary, {
      props: { plans: plan, legs: LEGS, idempotencyKey: '' },
    });
    const chips = queryAllByTestId(/^transport-chip-/);
    expect(chips[0].classList.contains('mode-walk')).toBe(true);
    expect(chips[1].classList.contains('mode-metro')).toBe(true);
    expect(chips[2].classList.contains('mode-taxi')).toBe(true);
  });

  it('recomputing class is absent on chips when idle (no replan in flight)', () => {
    const gaps = [{ mode: 'walk', minutes: 5, estimate: true }];
    const { queryAllByTestId } = render(Itinerary, {
      props: { plans: makePlans(gaps), legs: LEGS, idempotencyKey: '' },
    });
    const chip = queryAllByTestId(/^transport-chip-/)[0];
    expect(chip).toBeTruthy();
    expect(chip.classList.contains('recomputing')).toBe(false);
  });

  it('recomputing class applied to all chips when busy (replan in flight)', async () => {
    // Set up a replan with a deferred promise so we can check state mid-flight
    // and then cleanly resolve it to avoid hanging the afterEach hook.
    let resolveReplan!: (v: unknown) => void;
    replanMock.mockReturnValue(
      new Promise((r) => { resolveReplan = r; }),
    );

    const gaps = [
      { mode: 'walk', minutes: 5, estimate: true },
      { mode: 'metro', minutes: 15, estimate: true },
    ];
    const { queryAllByTestId, getByTestId } = render(Itinerary, {
      props: {
        plans: makePlans(gaps),
        legs: LEGS,
        idempotencyKey: 'k1',
        editable: true,
      },
    });

    // Confirm chips are present and NOT recomputing before the replan.
    let chips = queryAllByTestId(/^transport-chip-/);
    expect(chips).toHaveLength(2);
    expect(chips[0].classList.contains('recomputing')).toBe(false);

    // Trigger a reflow (sets busy='reflow:0').
    // fireEvent.click awaits tick() internally, so Svelte has flushed reactivity
    // and the recomputing class is applied before we check.
    await fireEvent.click(getByTestId('reflow-0'));

    // All chips should now carry the recomputing class (visual pulsing state).
    chips = queryAllByTestId(/^transport-chip-/);
    expect(chips).toHaveLength(2);
    expect(chips.every((c) => c.classList.contains('recomputing'))).toBe(true);

    // Verify the chip-mode text is still correct (chips render server data, not blanked).
    expect(chips[0].querySelector('.chip-mode')?.textContent).toBe('walk');
    expect(chips[1].querySelector('.chip-mode')?.textContent).toBe('metro');

    // Resolve the pending replan so the async chain completes and afterEach can clean up.
    resolveReplan({ outcome: 'noop', applied: [], rejected: [], notes: [], plan: {} });
    // Allow the microtask queue (busy = null) to flush before teardown.
    await new Promise<void>((r) => setTimeout(r, 0));
  });
});

// ── B3 (label duplication) + B4 (day-end hop-list / inline-chip duplication) ──
//
// B4 root cause: the day-end `intracity_hops` block used to render EVERY hop
// for the day (hotel→act1→act2→…→hotel), fully duplicating the per-item
// item_transport_gaps chips above for every POI-to-POI pair. Fix: the day-end
// block now renders ONLY the hotel-endpoint hops (from_kind/to_kind === 'hotel'),
// which item_transport_gaps never covers (it's POI-to-POI only).
//
// B3 root cause: server-supplied hop.label is already a fully-formatted string
// ("~18 min, tram, est.") that embeds minutes + mode. The old template appended
// " · ~{minutes} min" and " · {mode}" again unconditionally, duplicating the
// duration/mode inside a single line. Fix: only synthesize + append minutes/mode
// when label is absent.
describe('Day-end hop list (B3 label dedup + B4 hop-list/chip dedup)', () => {
  function makePlansWithHops(intracity_hops: Record<string, unknown>[]): DayPlan[] {
    return [{
      leg_id: 'leg-0',
      city: 'Kyoto',
      num_days: 1,
      days: [{
        day_index: 0,
        bad_weather: false,
        attractions: [
          { name: 'Temple A', category: 'tourism=temple', lat: 35.005, lon: 135.765, fee: null },
          { name: 'Museum B', category: 'tourism=museum', lat: 34.99, lon: 135.77, fee: 0 },
        ],
        meals: {},
        intracity_hops,
      }],
    }] as unknown as DayPlan[];
  }

  it('B4: renders only hotel-endpoint hops, dropping the POI-to-POI hops duplicated by item_transport_gaps', () => {
    const hops = [
      { from: 'Hotel', to: 'Temple A', from_kind: 'hotel', to_kind: 'attraction', mode: 'walk', minutes: 10, label: '~10 min, walk, est.' },
      { from: 'Temple A', to: 'Museum B', from_kind: 'attraction', to_kind: 'attraction', mode: 'walk', minutes: 5, label: '~5 min, walk, est.' },
      { from: 'Museum B', to: 'Hotel', from_kind: 'attraction', to_kind: 'hotel', mode: 'walk', minutes: 12, label: '~12 min, walk, est.' },
    ];
    const { container } = render(Itinerary, {
      props: { plans: makePlansWithHops(hops), legs: LEGS, idempotencyKey: '' },
    });
    const rendered = Array.from(container.querySelectorAll('.hop')).map((el) => el.textContent);
    // Only the 2 hotel-endpoint hops render; the middle POI-to-POI hop (already
    // covered by the inline item_transport_gaps chips) is dropped.
    expect(rendered).toHaveLength(2);
    expect(rendered.some((t) => t?.includes('Temple A') && t?.includes('Museum B'))).toBe(false);
  });

  it('B3: does not repeat minutes/mode when hop.label already contains them', () => {
    const hops = [
      { from: 'Hotel', to: 'Temple A', from_kind: 'hotel', to_kind: 'attraction', mode: 'tram', minutes: 18, label: '~18 min, tram, est.' },
    ];
    const { container } = render(Itinerary, {
      props: { plans: makePlansWithHops(hops), legs: LEGS, idempotencyKey: '' },
    });
    const text = container.querySelector('.hop')?.textContent ?? '';
    expect(text).toContain('~18 min, tram, est.');
    // The label already carries "18 min" and "tram" — must not appear a 2nd time.
    expect(text.match(/18 min/g)?.length).toBe(1);
    expect(text.match(/tram/g)?.length).toBe(1);
  });

  it('B3: falls back to raw from/to + minutes/mode only when label is absent', () => {
    const hops = [
      { from: 'Hotel', to: 'Temple A', from_kind: 'hotel', to_kind: 'attraction', mode: 'walk', minutes: 9 },
    ];
    const { container } = render(Itinerary, {
      props: { plans: makePlansWithHops(hops), legs: LEGS, idempotencyKey: '' },
    });
    const text = container.querySelector('.hop')?.textContent ?? '';
    expect(text).toContain('Hotel');
    expect(text).toContain('Temple A');
    expect(text.match(/9 min/g)?.length).toBe(1);
    expect(text.match(/walk/g)?.length).toBe(1);
  });

  // #197/#204: hotel has no real coordinate (intracity_transport.py), so both the
  // morning hotel→first-attraction and evening last-attraction→hotel hops proxy it
  // with the city centroid. When they land in the same distance/mode bucket, the
  // server-formatted label comes back byte-identical for two genuinely different
  // journeys — the live-test-reported duplicate line.
  it('disambiguates two hotel-endpoint hops whose server-formatted labels are byte-identical', () => {
    const identicalLabel = '~20 min, metro, est. (from city centre — exact hotel location unavailable)';
    const hops = [
      {
        from: 'Hotel', to: 'Riverside Park', from_kind: 'hotel', to_kind: 'attraction',
        mode: 'metro', minutes: 20, label: identicalLabel,
      },
      {
        from: 'Old Town Market', to: 'Hotel', from_kind: 'attraction', to_kind: 'hotel',
        mode: 'metro', minutes: 20, label: identicalLabel,
      },
    ];
    const { container } = render(Itinerary, {
      props: { plans: makePlansWithHops(hops), legs: LEGS, idempotencyKey: '' },
    });
    const rendered = Array.from(container.querySelectorAll('.hop')).map((el) => el.textContent ?? '');
    expect(rendered).toHaveLength(2);

    // The raw labels alone were byte-identical; the rendered lines must not be.
    expect(rendered[0]).not.toBe(rendered[1]);

    // Both directions must be recoverable from the rendered text.
    expect(rendered[0]).toContain('Hotel');
    expect(rendered[0]).toContain('Riverside Park');
    expect(rendered[1]).toContain('Old Town Market');
    expect(rendered[1]).toContain('Hotel');

    // The server-formatted label itself is preserved verbatim on both (additive
    // disambiguation, not a rewrite of the label format).
    expect(rendered[0]).toContain(identicalLabel);
    expect(rendered[1]).toContain(identicalLabel);
  });

  // Regression: hops that already have genuinely different label text must keep
  // rendering correctly with their own distinct content (the fix must not clobber
  // the already-working, non-duplicate case).
  it('regression: hotel-endpoint hops with genuinely different labels still render distinctly', () => {
    const hops = [
      {
        from: 'Hotel', to: 'Temple A', from_kind: 'hotel', to_kind: 'attraction',
        mode: 'walk', minutes: 10, label: '~10 min, walk, est.',
      },
      {
        from: 'Museum B', to: 'Hotel', from_kind: 'attraction', to_kind: 'hotel',
        mode: 'taxi', minutes: 22, label: '~22 min, taxi, est.',
      },
    ];
    const { container } = render(Itinerary, {
      props: { plans: makePlansWithHops(hops), legs: LEGS, idempotencyKey: '' },
    });
    const rendered = Array.from(container.querySelectorAll('.hop')).map((el) => el.textContent ?? '');
    expect(rendered).toHaveLength(2);
    expect(rendered[0]).not.toBe(rendered[1]);
    expect(rendered[0]).toContain('Hotel');
    expect(rendered[0]).toContain('Temple A');
    expect(rendered[0]).toContain('~10 min, walk, est.');
    expect(rendered[1]).toContain('Museum B');
    expect(rendered[1]).toContain('Hotel');
    expect(rendered[1]).toContain('~22 min, taxi, est.');
  });

  // Regression: the no-label fallback path (hasLabel false) must render unchanged —
  // this fix only adds a from/to prefix ahead of the label; the fallback branch
  // (raw from/to + synthesized minutes/mode) already carried from/to and is untouched.
  it('regression: no-label fallback path is unchanged by the disambiguation fix', () => {
    const hops = [
      { from: 'Hotel', to: 'Temple A', from_kind: 'hotel', to_kind: 'attraction', mode: 'walk', minutes: 9 },
      { from: 'Museum B', to: 'Hotel', from_kind: 'attraction', to_kind: 'hotel', mode: 'taxi', minutes: 15 },
    ];
    const { container } = render(Itinerary, {
      props: { plans: makePlansWithHops(hops), legs: LEGS, idempotencyKey: '' },
    });
    const rendered = Array.from(container.querySelectorAll('.hop')).map((el) => el.textContent ?? '');
    expect(rendered).toHaveLength(2);
    expect(rendered[0]).toContain('Hotel');
    expect(rendered[0]).toContain('Temple A');
    expect(rendered[0].match(/9 min/g)?.length).toBe(1);
    expect(rendered[0].match(/walk/g)?.length).toBe(1);
    expect(rendered[1]).toContain('Museum B');
    expect(rendered[1]).toContain('Hotel');
    expect(rendered[1].match(/15 min/g)?.length).toBe(1);
    expect(rendered[1].match(/taxi/g)?.length).toBe(1);
  });

  // #222 fix: hop.from/hop.to (often leg.hotel_title, which has no name_en in the
  // schema — same data gap as #168's lodging leg-header) used to render as a raw
  // string with ZERO name-tier handling — the one hotel_title consumer #168 never
  // wired. Now routed through the SAME namePresentation() pipeline as the leg
  // header (Itinerary.name-tier.test.ts) and shares its exact "unreadable primary"
  // indicator/markup. Nested here (not a sibling describe) so it can reuse this
  // block's makePlansWithHops() fixture helper.
  describe('#222 day-end hop endpoints routed through namePresentation() (mirrors #168 lodging)', () => {
    const UNREADABLE_TEXT = 'shown in original script — no English name available';

    it('a plain Latin hop endpoint renders unchanged, no unreadable indicator', () => {
      const hops = [
        { from: 'Hotel', to: 'Temple A', from_kind: 'hotel', to_kind: 'attraction', mode: 'walk', minutes: 10 },
      ];
      const { container, queryByText } = render(Itinerary, {
        props: { plans: makePlansWithHops(hops), legs: LEGS, idempotencyKey: '' },
      });
      const text = container.querySelector('.hop')?.textContent ?? '';
      expect(text).toContain('Hotel');
      expect(text).toContain('Temple A');
      expect(queryByText(UNREADABLE_TEXT)).toBeNull();
    });

    it('a non-Latin-only hop endpoint (raw hotel_title, no name_en available server-side) shows the honest unreadable indicator', () => {
      // Real catalog row (ucp-merchant/catalog_supplement.json, id "akishima-akishima-a") —
      // fully non-Latin, no English fallback available anywhere in the record — same
      // fixture value as Itinerary.name-tier.test.ts's lodging-header case, reused here
      // because it flows into hop.from/hop.to via the SAME leg.hotel_title field
      // (intracity_transport.py's `leg.get("hotel_title")`).
      const hops = [
        { from: '東京昭島迎賓館a', to: 'Temple A', from_kind: 'hotel', to_kind: 'attraction', mode: 'walk', minutes: 10 },
      ];
      const { container, getByText } = render(Itinerary, {
        props: { plans: makePlansWithHops(hops), legs: LEGS, idempotencyKey: '' },
      });
      const text = container.querySelector('.hop')?.textContent ?? '';
      expect(text).toContain('東京昭島迎賓館');
      expect(text).toContain('Temple A');
      expect(getByText(UNREADABLE_TEXT)).toBeTruthy();
    });

    it('does NOT fire the unreadable indicator on the Latin-only "to" side when only "from" is non-Latin', () => {
      const hops = [
        { from: '東京昭島迎賓館a', to: 'Hotel', from_kind: 'attraction', to_kind: 'hotel', mode: 'walk', minutes: 10 },
      ];
      const { container, getAllByText } = render(Itinerary, {
        props: { plans: makePlansWithHops(hops), legs: LEGS, idempotencyKey: '' },
      });
      // exactly one indicator (for the "from" side) — the Latin "to" side must not
      // also fabricate one.
      expect(getAllByText(UNREADABLE_TEXT)).toHaveLength(1);
      expect(container.querySelector('.hop')?.textContent ?? '').toContain('Hotel');
    });

    // dailies-review fix (issue #4): the old inline render gave the "shown in
    // original script" caveat no stated referent — on a hotel-endpoint hop a
    // reader couldn't tell if the caveat was about the hotel or the activity.
    // These three tests cover the "Hotel:" label + per-endpoint referent text.
    it('labels the hotel endpoint explicitly with "Hotel:" in the main hop line', () => {
      const hops = [
        { from: 'Hotel', to: 'Faisal Mosque', from_kind: 'hotel', to_kind: 'attraction', mode: 'taxi', minutes: 18 },
      ];
      const { container } = render(Itinerary, {
        props: { plans: makePlansWithHops(hops), legs: LEGS, idempotencyKey: '' },
      });
      const line = container.querySelector('.hop-line')?.textContent ?? '';
      expect(line).toContain('Hotel: Hotel');
      expect(line).not.toContain('Hotel: Faisal Mosque');
    });

    it('states which endpoint an unreadable caveat refers to (hotel side)', () => {
      const hops = [
        { from: '東京昭島迎賓館a', to: 'Temple A', from_kind: 'hotel', to_kind: 'attraction', mode: 'walk', minutes: 10 },
      ];
      const { getByTestId, queryByTestId } = render(Itinerary, {
        props: { plans: makePlansWithHops(hops), legs: LEGS, idempotencyKey: '' },
      });
      expect(getByTestId('hop-caveat-from').textContent ?? '').toContain('Hotel name');
      expect(queryByTestId('hop-caveat-to')).toBeNull();
    });

    it('renders two separate caveat lines, each with its own referent, when both endpoints are unreadable', () => {
      const hops = [
        { from: '東京昭島迎賓館a', to: '築地本願寺', from_kind: 'hotel', to_kind: 'attraction', mode: 'walk', minutes: 10 },
      ];
      const { getByTestId, getAllByText } = render(Itinerary, {
        props: { plans: makePlansWithHops(hops), legs: LEGS, idempotencyKey: '' },
      });
      // two distinct caveat elements, not one run-together line
      expect(getByTestId('hop-caveat-from').textContent ?? '').toContain('Hotel name');
      expect(getByTestId('hop-caveat-to').textContent ?? '').toContain('Destination name');
      expect(getByTestId('hop-caveat-from')).not.toBe(getByTestId('hop-caveat-to'));
      expect(getAllByText(UNREADABLE_TEXT)).toHaveLength(2);
    });
  });
});

// B3 also affected the airport-transfer ribbon (same server-formatted-label
// pattern as intracity_hops), which the day-end hop fix alone wouldn't cover.
describe('Airport transfer ribbon (B3 label dedup)', () => {
  function makePlansWithTransfer(airport_transfer: Record<string, unknown>[]): DayPlan[] {
    return [{
      leg_id: 'leg-0',
      city: 'Kyoto',
      num_days: 1,
      airport_transfer,
      days: [{ day_index: 0, bad_weather: false, attractions: [], meals: {} }],
    }] as unknown as DayPlan[];
  }

  it('does not repeat minutes when t.label already contains them', () => {
    const transfer = [
      { direction: 'inbound', from: 'KIX', to: 'hotel', mode: 'taxi', minutes: 75, label: '~75 min, taxi, est. (from city centre — exact hotel location unavailable)' },
    ];
    const { container } = render(Itinerary, {
      props: { plans: makePlansWithTransfer(transfer), legs: LEGS, idempotencyKey: '' },
    });
    const text = container.querySelector('.ribbon')?.textContent ?? '';
    expect(text).toContain('~75 min, taxi, est.');
    expect(text.match(/75 min/g)?.length).toBe(1);
  });

  it('falls back to raw from/to + minutes only when label is absent', () => {
    const transfer = [{ direction: 'inbound', from: 'KIX', to: 'hotel', minutes: 75 }];
    const { container } = render(Itinerary, {
      props: { plans: makePlansWithTransfer(transfer), legs: LEGS, idempotencyKey: '' },
    });
    const text = container.querySelector('.ribbon')?.textContent ?? '';
    expect(text).toContain('KIX');
    expect(text.match(/75 min/g)?.length).toBe(1);
  });
});
