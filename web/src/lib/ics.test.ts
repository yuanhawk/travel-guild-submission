import { describe, it, expect } from 'vitest';
import { buildIcs, escIcs, dateOnly, assistantSummary } from './ics';
import type { NegotiateResult } from './api';

// A realistic BOOKED result: 2 legs, real check-in/out, days with meals + attractions,
// one place name with a comma + newline to exercise escaping.
const BOOKED: NegotiateResult = {
  outcome: 'success',
  booking_ref: 'BK-JP-1',
  itinerary_narrative: { overview: 'Day one is gentle.\n\nThen the mountains.' },
  legs: [
    { leg_id: 'leg-0', city: 'Tokyo', country: 'Japan', checkin: '2026-10-01', checkout: '2026-10-04' },
    { leg_id: 'leg-1', city: 'Hakodate', country: 'Japan', checkin: '2026-10-04', checkout: '2026-10-06' },
  ],
  day_plans: [
    {
      leg_id: 'leg-0', city: 'Tokyo', country: 'Japan', checkin: '2026-10-01', checkout: '2026-10-04', num_days: 3,
      days: [
        { day_index: 0,
          meals: { breakfast: { name: 'Kissaten, café' }, lunch: { name: 'Ramen' } },
          attractions: [{ name: 'Mt. Moiwa\nTrail', category: 'tourism=viewpoint', weather_exposure: 'outdoor' }] },
        { day_index: 1, meals: {}, attractions: [{ name: 'Museum', category: 'tourism=museum' }] },
      ],
    },
    {
      leg_id: 'leg-1', city: 'Hakodate', country: 'Japan', checkin: '2026-10-04', checkout: '2026-10-06', num_days: 2,
      days: [{ day_index: 0, meals: {}, attractions: [] }],
    },
  ],
  package_total_with_fees_cents: 184000,
};

describe('buildIcs', () => {
  const ics = buildIcs(BOOKED);

  it('is a valid VCALENDAR with CRLF line endings', () => {
    expect(ics.startsWith('BEGIN:VCALENDAR\r\n')).toBe(true);
    expect(ics.includes('VERSION:2.0\r\n')).toBe(true);
    expect(ics.includes('PRODID:')).toBe(true);
    expect(ics.trimEnd().endsWith('END:VCALENDAR')).toBe(true);
    expect(ics.includes('\n') && !ics.includes('\r\n\r')).toBe(true);
    // every newline is part of a CRLF (no bare \n)
    expect(/[^\r]\n/.test(ics)).toBe(false);
  });

  it('emits a lodging event per leg + one event per day', () => {
    const veventCount = (ics.match(/BEGIN:VEVENT\r\n/g) || []).length;
    // 2 lodging (Tokyo, Hakodate) + 3 days (Tokyo d0, d1; Hakodate d0) = 5
    expect(veventCount).toBe(5);
    expect((ics.match(/END:VEVENT\r\n/g) || []).length).toBe(veventCount);
  });

  it('uses all-day VALUE=DATE events and invents NO clock times', () => {
    expect(ics.includes('DTSTART;VALUE=DATE:20261001')).toBe(true);   // Tokyo day 0 = check-in
    expect(ics.includes('DTSTART;VALUE=DATE:20261002')).toBe(true);   // Tokyo day 1 = +1
    expect(ics.includes('DTSTART;VALUE=DATE:20261004')).toBe(true);   // Hakodate
    expect(ics.includes('DTEND;VALUE=DATE:20261004')).toBe(true);     // Tokyo stay end
    // no fabricated time component on any DTSTART/DTEND (would look like ...DATE:YYYYMMDDTHHMM)
    expect(/DT(START|END)[^\r]*\d{8}T\d/.test(ics)).toBe(false);
  });

  it('gives every event a stable, deterministic UID (no now()/random)', () => {
    expect(ics.includes('UID:lodging-BK-JP-1-0@travelguild')).toBe(true);
    expect(ics.includes('UID:day-BK-JP-1-0-0@travelguild')).toBe(true);
    // deterministic: a second build is byte-identical
    expect(buildIcs(BOOKED)).toBe(ics);
    // DTSTAMP is derived from the trip (earliest check-in), not the wall clock
    expect(ics.includes('DTSTAMP:20261001T000000Z')).toBe(true);
  });

  it('escapes commas, semicolons and newlines in text fields', () => {
    expect(ics.includes('Kissaten\\, café')).toBe(true);     // comma escaped (in DESCRIPTION)
    expect(ics.includes('Mt. Moiwa\\nTrail')).toBe(true);    // newline → literal \n
    expect(ics.includes('SUMMARY:Stay: Tokyo')).toBe(true);
    expect(ics.includes('LOCATION:Tokyo')).toBe(true);
  });

  it('surfaces an unverified-lodging note honestly when flagged', () => {
    const flagged = { ...BOOKED, legs: [{ ...BOOKED.legs![0], unverified_lodging: true }, BOOKED.legs![1]] };
    expect(buildIcs(flagged)).toMatch(/Lodging not yet verified/);
  });

  // RFC 5545 folds any content line over 75 octets across CRLF + a leading-space
  // continuation — unfold before substring/regex assertions so tests don't depend
  // on exactly where a long CALDESC happens to wrap.
  function unfold(raw: string): string { return raw.replace(/\r\n /g, ''); }

  it('embeds the narrative overview as X-WR-CALDESC when present', () => {
    expect(unfold(ics)).toMatch(/X-WR-CALDESC:Day one is gentle\.\\n\\nThen the mountains\./);
  });

  it('does NOT prepend a stale caveat when itinerary_narrative.stale is absent/false (no regression)', () => {
    const notStale = { ...BOOKED, itinerary_narrative: { ...BOOKED.itinerary_narrative, stale: false } };
    const out = unfold(buildIcs(notStale));
    expect(out).toMatch(/X-WR-CALDESC:Day one is gentle\.\\n\\nThen the mountains\./);
    expect(out).not.toMatch(/may be outdated|generated before your latest edit/);
  });

  it('prepends the honest stale_reason to X-WR-CALDESC when itinerary_narrative.stale is true', () => {
    const stale = {
      ...BOOKED,
      itinerary_narrative: {
        ...BOOKED.itinerary_narrative,
        stale: true,
        stale_reason: 'This AI-written summary was generated before your latest edit and may still describe stops that were since removed, added, or reordered.',
      },
    };
    const out = unfold(buildIcs(stale));
    expect(out).toMatch(/X-WR-CALDESC:This AI-written summary was generated before your latest edit/);
    // the original overview text is still present after the caveat (not replaced)
    expect(out).toMatch(/Day one is gentle\.\\n\\nThen the mountains\./);
  });

  it('falls back to a generic honest caveat when stale is true but stale_reason is absent', () => {
    const stale = { ...BOOKED, itinerary_narrative: { ...BOOKED.itinerary_narrative, stale: true } };
    const out = unfold(buildIcs(stale));
    expect(out).toMatch(/X-WR-CALDESC:This summary may be outdated after your latest edit\./);
  });

  it('emits no X-WR-CALDESC line at all when there is no narrative overview (no fabrication)', () => {
    const noNarrative: NegotiateResult = { ...BOOKED, itinerary_narrative: undefined };
    const out = buildIcs(noNarrative);
    expect(out).not.toContain('X-WR-CALDESC');
  });

  it('skips a leg with no real check-in date (never fabricates one)', () => {
    const noDate: NegotiateResult = {
      outcome: 'success', booking_ref: 'BK-X',
      legs: [{ leg_id: 'leg-0', city: 'Nowhere' }],
      day_plans: [{ leg_id: 'leg-0', city: 'Nowhere', days: [{ day_index: 0, attractions: [] }] }],
    };
    const out = buildIcs(noDate);
    expect((out.match(/BEGIN:VEVENT/g) || []).length).toBe(0);   // nothing dated → no events
    expect(out.includes('BEGIN:VCALENDAR')).toBe(true);          // still a valid empty calendar
  });
});

describe('dateOnly / escIcs / assistantSummary', () => {
  it('dateOnly formats + adds days in UTC deterministically', () => {
    expect(dateOnly('2026-10-01')).toBe('20261001');
    expect(dateOnly('2026-10-01', 3)).toBe('20261004');
    expect(dateOnly('2026-10-31', 1)).toBe('20261101');   // month rollover
    expect(dateOnly('not-a-date')).toBeNull();
  });
  it('escIcs escapes per RFC 5545', () => {
    expect(escIcs('a, b; c\\d\ne')).toBe('a\\, b\\; c\\\\d\\ne');
  });
  it('assistantSummary returns overview when present, null when absent (no fabrication)', () => {
    expect(assistantSummary({ itinerary_narrative: { overview: 'hi' } })).toBe('hi');
    expect(assistantSummary({ itinerary_narrative: { overview: '   ' } })).toBeNull();
    expect(assistantSummary({ itinerary_narrative: {} })).toBeNull();     // no overview field
    expect(assistantSummary({})).toBeNull();
    expect(assistantSummary(null)).toBeNull();
  });
});
