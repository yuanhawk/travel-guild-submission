import { describe, it, expect } from 'vitest';
import {
  displayName, prettyCategory, exposureOf, feeLabel,
  mealAnchoredTimeline, hazardLines, dayPins, planPins, budgetRows, budgetPct,
  attractionRef, moveOps, cuisineDisplay, namePresentation, hasNonLatinScript,
} from './itinerary';
import { uiState } from './api';
import type { NegotiateResult, DayDetail } from './api';

// ── realistic served fixture (Hokkaido, multi-city) ──────────────────────────
const day: DayDetail = {
  day_index: 1,
  bad_weather: false,
  attractions: [
    { name: 'Mt. Moiwa Trail', category: 'tourism=viewpoint', weather_exposure: 'outdoor', lat: 43.04, lon: 141.32, fee: null },
    { name: 'Hokkaido Museum', category: 'tourism=museum', weather_exposure: 'indoor', lat: 43.05, lon: 141.47, fee: 0 },
  ],
  meals: {
    breakfast: { name: 'Kissaten', category: 'amenity=cafe', cuisine: 'coffee', lat: 43.06, lon: 141.35 },
    lunch: { name: 'Ramen Counter', category: 'amenity=restaurant', cuisine: 'ramen', lat: 43.06, lon: 141.34 },
    dinner: { name: 'Susukino Izakaya', category: 'amenity=restaurant', cuisine: 'izakaya' },
  },
  intracity_hops: [{ from: 'Hotel', to: 'Mt. Moiwa', mode: 'tram', minutes: 18, label: 'Hotel → Mt. Moiwa' }],
};

const fixture: NegotiateResult = {
  outcome: 'success',
  booking_ref: 'BK-JP-7',
  package_total_with_fees_cents: 324000,
  total_budget_cents: 400000,
  // Real served shape (LineItemAssembler.add_fee): description/kind/usd_cents, never label/amount_cents.
  fee_line_items: [{ description: 'Platform & FX fees', kind: 'premium', usd_cents: 9400 }],
  insurance: { premium_cents: 17600 },
  legs: [
    { leg_id: 'leg-0', city: 'Sapporo', checkin: '2026-10-01', checkout: '2026-10-04', lat: 43.06, lng: 141.35 },
    { leg_id: 'leg-1', city: 'Hakodate', checkin: '2026-10-07', checkout: '2026-10-10', lat: 41.77, lng: 140.73 },
  ],
  day_plans: [
    { leg_id: 'leg-0', city: 'Sapporo', num_days: 3,
      airport_transfer: [{ direction: 'inbound', label: 'New Chitose → hotel', minutes: 75 }],
      days: [day],
      unscheduled_attractions: [{ name: 'Otaru Canal', category: 'tourism=attraction', lat: 43.2, lon: 141.0 }] },
    { leg_id: 'leg-1', city: 'Hakodate', num_days: 3, days: [{ day_index: 1, attractions: [], meals: {} }] },
  ],
  risk_signals: {
    consolidator: 'risk_agent',
    per_leg: [
      { leg_id: 'leg-0', alert_tier: 'MED', cyclone_likelihood_pct: 22, cyclone_basin: 'typhoon',
        flood_index_bp: 3100, decisions: { flag: true } },
      { leg_id: 'leg-1', alert_tier: 'LOW' },
    ],
  },
  active_emergencies: [{ leg_id: 'leg-0', city: 'Sapporo', status: 'clear' }],
};

describe('format helpers', () => {
  it('displayName prefers name_en, trims', () => {
    expect(displayName({ name_en: 'Tokyo Tower', name: '東京タワー' })).toBe('Tokyo Tower');
    expect(displayName({ name: '  X  ' })).toBe('X');
    expect(displayName(null)).toBe('');
  });
  it('prettyCategory strips the OSM key + title-cases', () => {
    expect(prettyCategory('tourism=museum')).toBe('Museum');
    expect(prettyCategory('art_gallery')).toBe('Art Gallery');
    expect(prettyCategory(null)).toBe('');
  });
  it('exposureOf normalizes only known values', () => {
    expect(exposureOf({ weather_exposure: 'INDOOR' })).toBe('indoor');
    expect(exposureOf({ weather_exposure: 'beach' })).toBeNull();
    expect(exposureOf({})).toBeNull();
  });
  it('feeLabel is honest: null→—, 0/free→Free, never fabricates', () => {
    expect(feeLabel(null)).toBe('—');
    expect(feeLabel('')).toBe('—');
    expect(feeLabel(0)).toBe('Free');
    expect(feeLabel('free')).toBe('Free');
    expect(feeLabel('¥500')).toBe('¥500');
    expect(feeLabel(1500)).toBe('$15.00');
  });
});

describe('mealAnchoredTimeline (no fabricated clock times)', () => {
  const tl = mealAnchoredTimeline(day);
  it('orders meals as anchors with attractions split morning/afternoon', () => {
    const slots = tl.map((t) => t.slot);
    expect(slots).toEqual(['Breakfast', 'Morning', 'Lunch', 'Afternoon', 'Dinner']);
  });
  it('carries indoor/outdoor exposure on activities, cuisine on meals', () => {
    const morning = tl.find((t) => t.slot === 'Morning');
    expect(morning?.exposure).toBe('outdoor');
    const bfast = tl.find((t) => t.slot === 'Breakfast');
    expect(bfast?.cuisine).toBe('coffee');
  });
  it('emits no time strings at all (slots only)', () => {
    expect(tl.some((t) => /\d{1,2}:\d{2}/.test(t.name))).toBe(false);
  });
});

describe('hazardLines (each hazard its own line + %)', () => {
  it('typhoon + flood are SEPARATE lines, capitalized, never bunched', () => {
    const lines = hazardLines(fixture.risk_signals!.per_leg![0]);
    expect(lines).toEqual([{ label: 'Typhoon', pct: 22 }, { label: 'Flood', pct: 31 }]);
  });
  it('a LOW leg with no hazards yields no lines', () => {
    expect(hazardLines(fixture.risk_signals!.per_leg![1])).toEqual([]);
  });
  it('a measured 0% flood/drought still renders a chip (null-vs-zero)', () => {
    const lines = hazardLines({ leg_id: 'leg-z', alert_tier: 'LOW',
      flood_index_bp: 0, drought_index_bp: 0 });
    expect(lines).toEqual([{ label: 'Flood', pct: 0 }, { label: 'Drought', pct: 0 }]);
  });
  it('absent flood/drought (bp undefined) stays excluded', () => {
    expect(hazardLines({ leg_id: 'leg-z', alert_tier: 'LOW' })).toEqual([]);
  });
});

describe('map pins (lat/lon → lng; hotel = approx centroid or geocoded)', () => {
  it('dayPins emits real attraction + meal coords only', () => {
    const pins = dayPins(day);
    expect(pins).toHaveLength(4); // 2 attractions + 2 meals with coords (dinner has none)
    expect(pins.every((p) => Number.isFinite(p.lat) && Number.isFinite(p.lng))).toBe(true);
  });
  it('planPins adds a labeled approx hotel pin per geocoded leg (legacy lat/lng path)', () => {
    const pins = planPins(fixture.day_plans!, fixture.legs!);
    expect(pins.some((p) => p.category === 'lodging' && /approx \(city centre\)/.test(p.label ?? ''))).toBe(true);
    expect(pins.some((p) => p.category === 'attraction' && p.label === 'Otaru Canal')).toBe(true);
  });
  it('planPins prefers hotel_lat/hotel_lon with "hotel location" label when coord_basis=geocoded', () => {
    // Leg with served hotel geocode (exact) — should use hotel_lat/hotel_lon
    const geoLegs = [
      { leg_id: 'leg-0', city: 'Singapore', hotel_lat: 1.3056, hotel_lon: 103.8321,
        hotel_coord_basis: 'geocoded' as const },
    ];
    const pins = planPins([], geoLegs);
    expect(pins).toHaveLength(1);
    const pin = pins[0];
    expect(pin.category).toBe('lodging');
    expect(pin.lat).toBeCloseTo(1.3056, 4);
    expect(pin.lng).toBeCloseTo(103.8321, 4);
    expect(pin.label).toMatch(/hotel location/);
    expect(pin.label).not.toMatch(/approx/);
  });
  it('planPins uses "approx (city centre)" label when coord_basis=city_centroid', () => {
    const centLegs = [
      { leg_id: 'leg-0', city: 'Tokyo', hotel_lat: 35.6762, hotel_lon: 139.6503,
        hotel_coord_basis: 'city_centroid' as const },
    ];
    const pins = planPins([], centLegs);
    expect(pins).toHaveLength(1);
    expect(pins[0].label).toMatch(/approx \(city centre\)/);
    expect(pins[0].lat).toBeCloseTo(35.6762, 4);
  });
  it('planPins falls back to legacy lat/lng when hotel_* absent', () => {
    const legacyLegs = [
      { leg_id: 'leg-0', city: 'Paris', lat: 48.8566, lng: 2.3522 },
    ];
    const pins = planPins([], legacyLegs);
    expect(pins).toHaveLength(1);
    expect(pins[0].lat).toBeCloseTo(48.8566, 4);
    expect(pins[0].label).toMatch(/approx \(city centre\)/);
  });
  it('planPins emits no lodging pin when leg has neither hotel_* nor lat/lng', () => {
    const emptyLegs = [{ leg_id: 'leg-0', city: 'Unknown' }];
    const pins = planPins([], emptyLegs);
    expect(pins.filter((p) => p.category === 'lodging')).toHaveLength(0);
  });
});

describe('budget (USD, served totals only)', () => {
  it('rows + percent from served cents', () => {
    const rows = budgetRows(fixture);
    // The fixture has no package_total_cents (lodging-only figure), so budgetRows()
    // can't itemize the honest split and falls back to the single unqualified total —
    // see the dedicated 'money-itemization honesty fix' describe block below for the
    // split-path coverage.
    expect(rows.find((r) => r.label === 'Platform & FX fees')?.value).toBe('$94.00');
    expect(rows.find((r) => r.label === 'Bookable package')?.value).toBe('$3,240.00');
    expect(budgetPct(324000, 400000)).toBe(81);
    expect(budgetPct(undefined, 400000)).toBeNull();
  });
});

// ── money-itemization honesty fix ───────────────────────────────────────────
// Held-vs-charged mismatch: fee estimates (insurance/visa/vaccine) are folded into
// package_total_with_fees_cents for DISPLAY only — they are never actually charged
// to the wallet (backend orchestrator.py _inject_fees docstring: "Lodging stays the
// merchant's authoritative line"). Only package_total_cents (lodging) is debited.
// budgetRows() must itemize the split whenever both figures + a nonzero fee are
// known, so a viewer never sees one unqualified total that silently includes money
// nobody actually charges.
describe('budgetRows — honest lodging/fee split (money-itemization fix)', () => {
  const withFeesFixture = {
    fee_line_items: [
      { description: 'Travel insurance premium', kind: 'premium', usd_cents: 17600 },
      { description: 'Visa fee', kind: 'visa', usd_cents: 3700 },
    ],
    fee_total_cents: 21300,
    package_total_cents: 160000,
    package_total_with_fees_cents: 181300,
    total_budget_cents: 300000,
  };

  it('itemizes lodging (charged) separately from fee estimates (not charged), reconciling to the held total', () => {
    const rows = budgetRows(withFeesFixture);

    const lodging = rows.find((r) => r.label === 'Lodging (charged to wallet on booking)');
    const insurance = rows.find((r) => r.label.startsWith('Travel insurance premium'));
    const visa = rows.find((r) => r.label.startsWith('Visa fee'));
    const total = rows.find((r) => r.label === 'Trip total (lodging + est. fees)');

    expect(lodging?.value).toBe('$1,600.00');
    // Fee rows are explicitly labeled as estimates paid separately — never implying
    // Travel Guild charges for them.
    expect(insurance?.label).toBe('Travel insurance premium (est., paid separately)');
    expect(insurance?.value).toBe('$176.00');
    expect(visa?.label).toBe('Visa fee (est., paid separately)');
    expect(visa?.value).toBe('$37.00');
    expect(total?.value).toBe('$1,813.00');

    // Internal consistency: lodging + fee estimate reconciles exactly to the held/
    // trip-total figure — no silent gap between what's itemized and what's held.
    const centsOf = (s: string | undefined) => Math.round(parseFloat((s ?? '$0').replace(/[$,]/g, '')) * 100);
    expect(centsOf(lodging?.value) + centsOf(insurance?.value) + centsOf(visa?.value)).toBe(centsOf(total?.value));
    // 'Bookable package' (the old unqualified label) must NOT appear once the split fires.
    expect(rows.find((r) => r.label === 'Bookable package')).toBeUndefined();
  });

  it('falls back to the single unqualified total when lodging (package_total_cents) is unknown', () => {
    const rows = budgetRows({ ...withFeesFixture, package_total_cents: undefined });
    expect(rows.find((r) => r.label === 'Lodging (charged to wallet on booking)')).toBeUndefined();
    expect(rows.find((r) => r.label === 'Bookable package')?.value).toBe('$1,813.00');
  });

  it('no split when there are no fees at all — a single "Bookable package" row, same as before', () => {
    const rows = budgetRows({ package_total_cents: 160000, package_total_with_fees_cents: 160000, total_budget_cents: 300000 });
    expect(rows.find((r) => r.label === 'Lodging (charged to wallet on booking)')).toBeUndefined();
    expect(rows.find((r) => r.label === 'Bookable package')?.value).toBe('$1,600.00');
  });
});

describe('uiState (outcome in body, HTTP always 200)', () => {
  it('classifies the served outcomes', () => {
    expect(uiState(fixture)).toBe('success');
    expect(uiState({ outcome: 'cannot_satisfy', reason: 'insufficient_funds' })).toBe('insufficient_funds');
    expect(uiState({ outcome: 'cannot_satisfy', budget_shortfall_cents: 5000 })).toBe('budget_shortfall');
    expect(uiState({ needs_clarification: true })).toBe('needs_clarification');
    expect(uiState({ outcome: 'cannot_satisfy', reason: 'do_not_recommend' })).toBe('declined');
    expect(uiState(null)).toBe('empty');
  });
});

export { fixture };

// ─── #98 edit-lane helpers ─────────────────────────────────────────────────

// T12: attractionRef — wikidata-first, fallbacks, trim
describe('attractionRef — integrity ref for /replan ops', () => {
  it('prefers wikidata over name_en and name', () => {
    expect(attractionRef({ wikidata: 'Q123', name_en: 'Senso-ji', name: '浅草寺' })).toBe('Q123');
  });
  it('falls back to name_en when wikidata absent', () => {
    expect(attractionRef({ name_en: 'Senso-ji', name: '浅草寺' })).toBe('Senso-ji');
  });
  it('falls back to name when name_en absent', () => {
    expect(attractionRef({ name: 'Meiji Shrine' })).toBe('Meiji Shrine');
  });
  it('returns empty string for null/undefined/all-empty', () => {
    expect(attractionRef(null)).toBe('');
    expect(attractionRef(undefined)).toBe('');
    expect(attractionRef({})).toBe('');
  });
  it('trims whitespace from the ref', () => {
    expect(attractionRef({ name: '  Trim me  ' })).toBe('Trim me');
  });
});

// T13-15: moveOps — drag-drop → op pair
describe('moveOps — drag-drop to /replan op pair', () => {
  // T13: same-day forward move: removing pos 0 shifts later items down → insert = dst-1
  it('same-day forward move (src 0 → dst 2): [remove(pos0), add(pos1)] (shift-down)', () => {
    const ops = moveOps({ leg_index: 0, src_day: 0, src_pos: 0, dst_day: 0, dst_pos: 2, ref: 'Q1' });
    expect(ops).toHaveLength(2);
    expect(ops[0]).toEqual({ op: 'remove_place', leg_index: 0, day_index: 0, position: 0, attraction_ref: 'Q1' });
    expect(ops[1]).toEqual({ op: 'add_place', leg_index: 0, day_index: 0, position: 1, attraction_ref: 'Q1', from: 'unscheduled' });
  });

  // T14: same-day backward move: no shift needed
  it('same-day backward move (src 2 → dst 0): [remove(pos2), add(pos0)]', () => {
    const ops = moveOps({ leg_index: 0, src_day: 0, src_pos: 2, dst_day: 0, dst_pos: 0, ref: 'Q2' });
    expect(ops).toHaveLength(2);
    expect(ops[0]).toEqual({ op: 'remove_place', leg_index: 0, day_index: 0, position: 2, attraction_ref: 'Q2' });
    expect(ops[1]).toEqual({ op: 'add_place', leg_index: 0, day_index: 0, position: 0, attraction_ref: 'Q2', from: 'unscheduled' });
  });

  // T15: cross-day move: no index shift (different day contexts)
  it('cross-day move (day0 pos1 → day1 pos0): [remove(day0,pos1), add(day1,pos0)]', () => {
    const ops = moveOps({ leg_index: 0, src_day: 0, src_pos: 1, dst_day: 1, dst_pos: 0, ref: 'Q3' });
    expect(ops).toHaveLength(2);
    expect(ops[0]).toEqual({ op: 'remove_place', leg_index: 0, day_index: 0, position: 1, attraction_ref: 'Q3' });
    expect(ops[1]).toEqual({ op: 'add_place', leg_index: 0, day_index: 1, position: 0, attraction_ref: 'Q3', from: 'unscheduled' });
  });
});

// T16: mealAnchoredTimeline attrIndex — nameless-filter/raw-index gotcha
describe('mealAnchoredTimeline: attrIndex carries RAW index despite displayName filter', () => {
  it('skips nameless attractions but attrIndex reflects raw position (not filtered pos)', () => {
    const dayWithNameless: DayDetail = {
      day_index: 0,
      bad_weather: false,
      attractions: [
        { name: '', category: 'tourism=unknown' },            // raw index 0 — nameless, filtered out
        { name: 'Senso-ji', category: 'tourism=temple' },    // raw index 1 — visible
        { name: 'Ueno Park', category: 'leisure=park' },     // raw index 2 — visible
      ],
      meals: {},
    };
    const tl = mealAnchoredTimeline(dayWithNameless);
    const activities = tl.filter((t) => t.kind === 'activity');
    expect(activities).toHaveLength(2);
    // The first visible activity is at RAW index 1 (index 0 was nameless and skipped).
    expect(activities[0].name).toBe('Senso-ji');
    expect(activities[0].attrIndex).toBe(1);   // NOT 0 — the filter/raw-index gotcha
    expect(activities[1].name).toBe('Ueno Park');
    expect(activities[1].attrIndex).toBe(2);
  });

  it('meals carry attrIndex === -1', () => {
    const tl = mealAnchoredTimeline(day);
    const meals = tl.filter((t) => t.kind === 'meal');
    expect(meals.every((m) => m.attrIndex === -1)).toBe(true);
  });

  it('existing activities carry correct raw indices (no nameless in fixture)', () => {
    const tl = mealAnchoredTimeline(day);
    const activities = tl.filter((t) => t.kind === 'activity');
    // fixture has 2 attractions at raw indices 0 and 1
    expect(activities[0].attrIndex).toBe(0);
    expect(activities[1].attrIndex).toBe(1);
  });
});

// ── cuisineDisplay (B2: cuisine chip overflow) ────────────────────────────────
describe('cuisineDisplay', () => {
  it('null/blank input → null (no chip rendered)', () => {
    expect(cuisineDisplay(null)).toBeNull();
    expect(cuisineDisplay(undefined)).toBeNull();
    expect(cuisineDisplay('')).toBeNull();
    expect(cuisineDisplay('   ')).toBeNull();
  });

  it('single token passes through, title-cased, untruncated', () => {
    expect(cuisineDisplay('ramen')).toEqual({ text: 'Ramen', full: 'Ramen', truncated: false });
  });

  it('two tokens (at the cap) show in full, untruncated', () => {
    expect(cuisineDisplay('japanese;ramen')).toEqual({
      text: 'Japanese, Ramen', full: 'Japanese, Ramen', truncated: false,
    });
  });

  // Realistic served shape: raw OSM `cuisine` tag — semicolon-joined, NO spaces —
  // is exactly what can't line-wrap in a chip (no whitespace to break on) and,
  // un-capped, is too long for any reasonable chip width. This is the B2 repro.
  it('long multi-token OSM-style string is capped at 2 visible tokens + "+N more"', () => {
    const raw = 'regional;japanese;izakaya;seafood;sushi';
    const out = cuisineDisplay(raw);
    expect(out).not.toBeNull();
    expect(out!.truncated).toBe(true);
    expect(out!.text).toBe('Regional, Japanese +3 more');
    // Nothing is silently lost — the full list survives for a title/tooltip.
    expect(out!.full).toBe('Regional, Japanese, Izakaya, Seafood, Sushi');
  });

  it('also handles comma-joined tokens and tolerates stray whitespace/underscores', () => {
    const out = cuisineDisplay(' italian , pizza,  regional_italian ,wine_bar ');
    expect(out!.text).toBe('Italian, Pizza +2 more');
    expect(out!.full).toBe('Italian, Pizza, Regional italian, Wine bar');
  });
});

describe('namePresentation: typographic Latin punctuation must NOT trigger the unreadable indicator', () => {
  it.each([
    "L’Atelier de Joël Robuchon",   // U+2019 curly apostrophe
    'Café – Bar',                    // U+2013 en dash
    'Hotel “Sakura”',                // U+201C/U+201D curly quotes
    'Rock … Roll Diner',        // U+2026 ellipsis
  ])('"%s" with no name_en is readable Latin -- unreadable=false', (name) => {
    expect(namePresentation({ name }).unreadable).toBe(false);
  });

  it('genuinely non-Latin scripts still fire (not just CJK)', () => {
    for (const name of ['Москва', 'مطعم البركة', 'ร้านอาหารไทย', '東京昇島迎賓館']) {
      expect(hasNonLatinScript(name)).toBe(true);
      expect(namePresentation({ name }).unreadable).toBe(true);
    }
  });

  // #225 CJK lane: the mechanism above has only ever been live-verified against
  // Arabic (Dubai, #222). Japanese kanji/katakana, Chinese hanzi, and Korean
  // Hangul are each DIFFERENT Unicode blocks from Arabic (and Hangul is a
  // different block from Han/Kana too) — never proven with a real string until
  // now. Every value below is copied verbatim from a real live /negotiate_text
  // response (Tokyo/Seoul/Beijing, local LLM-off orchestrator).
  it('real Japanese (kanji + katakana) restaurant names fire', () => {
    for (const name of ['MAC丼丸', 'カフェ・ド・クリエ', 'ケンタッキーフライドチキン']) {
      expect(hasNonLatinScript(name)).toBe(true);
      expect(namePresentation({ name }).unreadable).toBe(true);
    }
  });

  it('real Chinese (simplified hanzi) restaurant/POI names fire', () => {
    for (const name of ['星巴克', '大董烤鸭团结湖分店', '海淀第一深情之家', '中国铁道博物馆（正阳门展馆）']) {
      expect(hasNonLatinScript(name)).toBe(true);
      expect(namePresentation({ name }).unreadable).toBe(true);
    }
  });

  it('real Korean (Hangul) restaurant/POI names fire — a distinct Unicode block from Han/Kana', () => {
    for (const name of ['그로밋 커피하우스', '결 by Bean Brothers', '국군중앙성당']) {
      expect(hasNonLatinScript(name)).toBe(true);
      expect(namePresentation({ name }).unreadable).toBe(true);
    }
  });

  it('CJK name_en pairs (real live data) still prefer the English name — no unreadable indicator', () => {
    for (const [name, name_en] of [
      ['カフェ・ド・クリエ', 'CAFE de CRIE'],   // Japanese
      ['7번가 피자', '7th Street pizza'],        // Korean
      ['星巴克', 'Starbucks'],                    // Chinese
    ]) {
      const np = namePresentation({ name, name_en });
      expect(np.primary).toBe(name_en);
      expect(np.unreadable).toBe(false);
    }
  });

  // #225 Thai + Greek lane: the mechanism has only ever been live-verified
  // against Arabic (Dubai, #222) and, in this file, CJK. Thai (U+0E00-U+0E7F)
  // has NO inter-word spacing and stacks combining tone/vowel marks on the
  // base consonant — a different failure mode from every space-delimited
  // script tested so far. Greek (U+0370-U+03FF) sits directly adjacent to the
  // Latin Extended-A/B ranges this same allow-list carves out (which end at
  // U+024F) — a plausible over-broad-range edge case — and separately has a
  // lowercase final-sigma codepoint (ς, U+03C2) distinct from medial sigma
  // (σ, U+03C3) that a naive range check could handle inconsistently. Every
  // value below is copied VERBATIM from a real live build_booking_links()/
  // day-planner run (society/poi_catalog.json TH:bangkok / GR:athens, and
  // ucp-merchant/catalog.json for the lodging hotel_title case) — not a
  // synthetic placeholder.
  it('real Thai (no word-spacing, stacked marks) attraction/restaurant names fire', () => {
    for (const name of [
      'พระพุทธนรสีห์ตรีโลกเชฏฐ์',   // real Bangkok attraction, no name_en at all
      'เข้ม-ข้น',                    // stacked tone/vowel marks, no spaces
      'กรุงเทพมหานคร',               // "Bangkok" in Thai, no spaces
    ]) {
      expect(hasNonLatinScript(name)).toBe(true);
      expect(namePresentation({ name }).unreadable).toBe(true);
    }
  });

  it('a MIXED Latin+Thai restaurant name still fires on the whole string', () => {
    // Real live restaurant name (poi_catalog.json TH:bangkok), no name_en —
    // the Latin "Kem-Kon" prefix must not make the regex miss the Thai tail.
    const name = 'Kem-Kon (เข้ม-ข้น)';
    expect(hasNonLatinScript(name)).toBe(true);
    expect(namePresentation({ name }).unreadable).toBe(true);
  });

  it('real Greek (incl. lowercase final sigma ς) attraction/restaurant names fire', () => {
    for (const name of [
      'Αγία Ειρήνη',              // real Athens attraction, no name_en at all
      'Αγγέλων Βήμα',             // real Athens attraction, no name_en at all
      '...γεια...καλαμάκι...',    // real Athens restaurant, no name_en at all
      'λόγος',                     // medial sigma (σ)
      'λόγοςς',                    // forces a final-sigma (ς) codepoint
    ]) {
      expect(hasNonLatinScript(name)).toBe(true);
      expect(namePresentation({ name }).unreadable).toBe(true);
    }
  });

  it('real Greek lodging hotel_title (schema has no name_en at all) fires', () => {
    // Real live string (ucp-merchant/catalog.json, "piraeus" — Athens metro).
    const name = 'Σκορπιός';
    expect(hasNonLatinScript(name)).toBe(true);
    expect(namePresentation({ name }).unreadable).toBe(true);
  });

  it('Thai/Greek name_en pairs (real live data) still prefer the English name — no unreadable indicator', () => {
    for (const [name, name_en] of [
      ['พระบรมมหาราชวัง', 'Grand Palace'],   // Thai — real Bangkok pair
      ['Kem-Kon (เข้ม-ข้น)', 'Kem-Kon'],       // Thai — mixed-script pair
      ['Αγία Σοφία', 'St. Sophia'],           // Greek — near-homograph pair
    ]) {
      const np = namePresentation({ name, name_en });
      expect(np.primary).toBe(name_en);
      expect(np.unreadable).toBe(false);
      // The local-script companion must still surface (#168's "always show
      // local script alongside verified English" — never silently dropped
      // just because an English name_en exists).
      expect(np.local).toBe(name);
    }
  });
});

// ---------------------------------------------------------------------------
// #233 root-cause regression tests: NFD-decomposed Latin, Azerbaijani/Uzbek
// Latin-script gap-range letters, and (frontend defense-in-depth) that
// genuinely non-Latin scripts are still caught after the NFC-normalize +
// range-widen fix. Real names drawn from the live catalog per the audit.
// ---------------------------------------------------------------------------
describe('hasNonLatinScript / namePresentation: #233 false-positive regressions', () => {
  it('NFD-decomposed Vietnamese (base letter + combining marks) is readable Latin, not flagged', () => {
    const nfc = "Quán bánh xèo Thanh Nga";
    const nfd = "Quán bánh xèo Thanh Nga";
    expect(nfd).not.toBe(nfc); // sanity: genuinely decomposed, not a no-op
    expect(hasNonLatinScript(nfc)).toBe(false);
    expect(hasNonLatinScript(nfd)).toBe(false);
    expect(namePresentation({ name: nfd }).unreadable).toBe(false);
  });

  it('real Azerbaijani schwa (U+0259) attraction name is readable Latin, not flagged', () => {
    // Real live Baku attraction, no name_en: "Azerbaijan State Puppet Theatre".
    const name = "Azərbaycan Dövlət Kukla Teatrı";
    expect(hasNonLatinScript(name)).toBe(false);
    expect(namePresentation({ name }).unreadable).toBe(false);
  });

  it('real Uzbek Latin okina/turned-comma (U+02BB/U+02BC) mosque name is readable Latin, not flagged', () => {
    // Real live Andijon mosque, no name_en.
    const name = "Dovudxon toʻra jomeʼ masjidi";
    expect(hasNonLatinScript(name)).toBe(false);
    expect(namePresentation({ name }).unreadable).toBe(false);
  });

  it('genuinely non-Latin scripts (Cyrillic, Hebrew) still fire after the widen + NFC fix', () => {
    // Real Belgrade kafana (Serbian Cyrillic) and a Jerusalem hotel_title (Hebrew) —
    // negative control: widening the allow-list must not swallow real non-Latin script.
    for (const name of ["Кафана Знак Питања", "מלון 21"]) {
      expect(hasNonLatinScript(name)).toBe(true);
      expect(namePresentation({ name }).unreadable).toBe(true);
    }
  });
});

// ---------------------------------------------------------------------------
// #234: the #233 NFC-normalize fix only helps sequences that HAVE a
// precomposed codepoint to compose into -- it does nothing for a combining
// mark that has none. Turkish dotted-I (U+0130) case-folds to "i" + COMBINING
// DOT ABOVE (U+0307), for which no precomposed "i-with-dot-above" letter
// exists, so NFC leaves the bare mark in place; the old allow-list (which
// stopped at U+02FF / U+1EFF) then flagged the whole plainly-readable Latin
// name as non-Latin. Real venue names from the live catalog per the audit.
// ---------------------------------------------------------------------------
describe('hasNonLatinScript / namePresentation: #234 combining-mark-with-no-precomposed-form regressions', () => {
  it('Turkish i-with-dot-above (U+0307, no precomposed letter) is readable Latin, not flagged', () => {
    // Real Kars museum + Istanbul-area restaurant names, no name_en.
    for (const name of ['Kars Müzesi̇', 'Köfteci̇ Yusuf', 'Dönerci̇ Ahmet Usta']) {
      expect(hasNonLatinScript(name)).toBe(false);
      expect(namePresentation({ name }).unreadable).toBe(false);
    }
  });

  it('Lao romanization bare macron (U+0304, no precomposed letter) is readable Latin, not flagged', () => {
    const name = 'Vat Sīmư̄ang';
    expect(hasNonLatinScript(name)).toBe(false);
    expect(namePresentation({ name }).unreadable).toBe(false);
  });

  it('negative control: a combining mark riding on a non-Latin base does not launder the whole string', () => {
    expect(hasNonLatinScript('Кафана')).toBe(true);
  });
});
