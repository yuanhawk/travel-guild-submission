// Pure, presentation-only helpers for the itinerary view. NO network, NO recompute
// of price/risk/rank — they only RESHAPE server-supplied values for display, so they
// are trivially unit-testable and keep the var-0 contract (frontend never invents data).

import { bpToPct, centsToUsd } from './api';
import type {
  Attraction, Restaurant, Meals, DayDetail, DayPlan, Leg, RiskSignal, MapPin, ReplanOp, BookingLink,
} from './api';

// ── small utils ────────────────────────────────────────────────────────────
const fin = (n: unknown): n is number => typeof n === 'number' && Number.isFinite(n);
const round1 = (n: number): number => Math.round(n * 10) / 10;
const cap = (s?: string | null): string => (s ? s.charAt(0).toUpperCase() + s.slice(1) : '');

export function displayName(x: { name_en?: string; name?: string } | null | undefined): string {
  return ((x?.name_en || x?.name) ?? '').trim();
}

// ── honest name-tier presentation (B1 spillover, #168) ───────────────────────
// Shared here (not just Itinerary.svelte-local) because more than one surface renders
// a raw place/lodging name: the itinerary timeline AND RightRail's Suggested-tab cards
// both need the SAME "never silently show unexplained non-Latin text" honesty treatment.
// "Always show local script alongside verified English (no similarity heuristic)."
// The verified-vs-romanized SOURCE badge needs a backend name_source field that does
// not exist for every surface yet — so the badge is rendered ONLY when an object
// explicitly carries that (forward-compatible, optional) field; it is never fabricated.
export function hasNonLatinScript(s: string): boolean {
  // Allowed ranges: Basic Latin + Latin-1 Supplement + Latin Extended-A/B +
  // IPA Extensions + Spacing Modifier Letters (\u0020-\u02FF — widening the
  // old \u024F cutoff to \u02FF covers real Latin-script orthographies that
  // live just past Extended-B: Azerbaijani schwa (U+0259) and the Uzbek
  // Latin okina/turned comma (U+02BB) — genuinely readable precomposed
  // Latin letters, not decomposition artifacts, so NFC normalization alone
  // can't fix them; the allow-list range itself has to widen (#233 root
  // cause B)), Latin Extended Additional incl. Vietnamese (\u1E00-\u1EFF),
  // whitespace, common ASCII punctuation, and the common TYPOGRAPHIC
  // punctuation block real place names use — curly single/double quotes
  // (\u2018-\u201F), en/em dash (\u2013-\u2014), and ellipsis (\u2026). Those
  // are General Punctuation, not a non-Latin script, and must not trigger
  // the "unreadable / non-Latin" classification (see
  // fix/nonlatin-punctuation-fp).
  //
  // The input is NFC-normalized FIRST: real catalog data is not guaranteed
  // to arrive precomposed (society/orchestration/server.py NFC-normalizes
  // `title` for its own Vietnamese matching, and the seed pipeline strips
  // combining marks via NFKD elsewhere), so an NFD Vietnamese name — base
  // vowel + horn (U+031B) + tone mark (U+0300 etc, Combining Diacritical
  // Marks) — must not be misclassified as non-Latin just because it
  // wasn't composed yet (#233 root cause A).
  //
  // Combining Diacritical Marks (\u0300-\u036F) are ALSO allow-listed
  // outright (#234): NFC only composes a base+mark sequence into a
  // precomposed codepoint WHERE ONE EXISTS -- it does not exist for every
  // mark, e.g. Turkish dotted-I (U+0130) case-folds to "i" + COMBINING DOT
  // ABOVE (U+0307) with no precomposed "i-with-dot-above" letter, and some
  // Lao/Vietnamese romanizations carry a bare macron/dot-below (U+0304/
  // U+0323) the same way -- so NFC-normalizing first is not sufficient on
  // its own; the residual combining mark must also be allow-listed, same
  // fix shape as the #233 root-cause-B range widen. A combining mark can
  // only ever ride on a preceding base letter, so allow-listing this block
  // alone never masks a genuinely non-Latin string: any non-Latin base
  // character it's attached to still fails the allow-list on its own. Kept
  // in sync with _LATIN_ALLOWED_RE in booking_links.py -- mirror any change
  // there here too.
  // eslint-disable-next-line no-control-regex
  return /[^\u0020-\u02FF\u0300-\u036F\u1E00-\u1EFF\u2013-\u2014\u2018-\u201F\u2026\s\-'.,()&/0-9]/u
    .test(s.normalize('NFC'));
}
export interface NamePresentation {
  primary: string; local: string | null; badge: string | null;
  // true when `primary` itself is the only name we have AND it's non-Latin script —
  // i.e. there is no name_en fallback, so an English-reading user would otherwise see
  // unreadable text with no explanation. Purely data-driven (name/name_en presence).
  unreadable: boolean;
}
export function namePresentation(
  x: { name?: string; name_en?: string; name_source?: unknown } | null | undefined,
): NamePresentation {
  const primary = displayName(x);
  const raw = (x?.name ?? '').trim();
  const nameEn = (x?.name_en ?? '').trim();
  const local = (nameEn && raw && raw !== primary && hasNonLatinScript(raw)) ? raw : null;
  const src = typeof x?.name_source === 'string' ? x.name_source : null;
  const badge = src === 'wikidata' ? 'verified · wikidata'
    : src === 'osm_name_en' ? 'verified'
    : src === 'romanized' ? 'romanized'
    : null;
  const unreadable = !nameEn && hasNonLatinScript(primary);
  return { primary, local, badge, unreadable };
}

/** "tourism=museum" → "Museum"; "art_gallery" → "Art Gallery". */
export function prettyCategory(raw?: string | null): string {
  if (!raw) return '';
  const v = raw.includes('=') ? raw.split('=').pop()! : raw;
  return v.replace(/[_-]+/g, ' ').replace(/\b\w/g, (c) => c.toUpperCase()).trim();
}

export type Exposure = 'indoor' | 'outdoor' | 'mixed';
export function exposureOf(a: { weather_exposure?: unknown } | null | undefined): Exposure | null {
  const v = String(a?.weather_exposure ?? '').toLowerCase();
  return v === 'indoor' || v === 'outdoor' || v === 'mixed' ? v : null;
}

/** (B2) Server-supplied `cuisine` is a raw OSM tag — often several tokens joined with
 *  ";" or "," and NO surrounding spaces (e.g. "regional;japanese;izakaya;seafood;sushi"),
 *  which can't line-wrap in a chip (no whitespace to break on) and, un-capped, is too
 *  long for any reasonable chip width. Cap the VISIBLE tokens; keep the full list in
 *  `full` for a title/tooltip so nothing is silently dropped, just de-prioritized. */
export interface CuisineDisplay { text: string; full: string; truncated: boolean }
const CUISINE_TOKEN_CAP = 2;
export function cuisineDisplay(raw?: string | null): CuisineDisplay | null {
  const s = (raw ?? '').trim();
  if (!s) return null;
  const tokens = s.split(/[;,]/).map((t) => cap(t.trim().replace(/_/g, ' '))).filter(Boolean);
  if (!tokens.length) return null;
  const full = tokens.join(', ');
  if (tokens.length <= CUISINE_TOKEN_CAP) return { text: full, full, truncated: false };
  const shown = tokens.slice(0, CUISINE_TOKEN_CAP).join(', ');
  const extra = tokens.length - CUISINE_TOKEN_CAP;
  return { text: `${shown} +${extra} more`, full, truncated: true };
}

/** HONEST fee label: null/blank → "—"; 0/"free" → "Free"; number → USD; else the raw string. */
export function feeLabel(fee: number | string | null | undefined): string {
  if (fee == null || fee === '') return '—';
  if (fee === 0) return 'Free';
  if (typeof fee === 'number') return centsToUsd(fee);
  const s = String(fee).trim();
  if (s === '0' || s.toLowerCase() === 'free') return 'Free';
  return s;
}

// ── meal-anchored timeline (NO fabricated clock times) ───────────────────────
export type Slot = 'Breakfast' | 'Morning' | 'Lunch' | 'Afternoon' | 'Tea' | 'Dinner' | 'Supper';
export interface TimelineItem {
  slot: Slot;
  kind: 'meal' | 'activity';
  name: string;
  category: string;
  exposure: Exposure | null;
  cuisine: string | null;
  fee: number | string | null;
  attrIndex: number;   // RAW index into day.attractions for activities; -1 for meals
  // #37/#212 — CONSTRUCTED reference/handoff link (attraction_link()/restaurant_link(),
  // society/utils/booking_links.py), mutated in place onto the source attraction/meal
  // entity. null/absent when the backend had nothing to construct a link from — never
  // fabricated. Render via .booking_url through safeHref(), same guard as #211.
  link: BookingLink | null;
}

const MEAL_SLOTS: Array<[Slot, keyof Meals]> = [
  ['Breakfast', 'breakfast'], ['Lunch', 'lunch'], ['Tea', 'tea'],
  ['Dinner', 'dinner'], ['Supper', 'supper'],
];

/** Build the day timeline: meals are the time-of-day anchors; attractions are split
 *  morning/afternoon by position (the backend serves no clock times — so we never
 *  invent any). Order: breakfast → morning → lunch → afternoon → tea → dinner → supper.
 *  attrIndex carries the RAW index into day.attractions (pre-filter) so /replan position
 *  is correct even when nameless attractions are skipped by displayName. */
export function mealAnchoredTimeline(day: DayDetail): TimelineItem[] {
  const raw = day.attractions ?? [];
  const att = raw.map((a, i) => ({ a, i })).filter(({ a }) => displayName(a));
  const mid = Math.ceil(att.length / 2);
  const meals = day.meals ?? {};
  const out: TimelineItem[] = [];

  const pushMeal = (slot: Slot, r: Restaurant | null | undefined) => {
    if (!r || !displayName(r)) return;
    out.push({ slot, kind: 'meal', name: displayName(r), category: prettyCategory(r.category),
      exposure: null, cuisine: (r.cuisine ?? null) as string | null, fee: null, attrIndex: -1,
      link: r.link ?? null });
  };
  const pushAtt = (slot: Slot, a: Attraction, rawIndex: number) => {
    out.push({ slot, kind: 'activity', name: displayName(a), category: prettyCategory(a.category),
      exposure: exposureOf(a), cuisine: null, fee: a.fee ?? null, attrIndex: rawIndex,
      link: a.link ?? null });
  };

  pushMeal('Breakfast', meals.breakfast);
  att.slice(0, mid).forEach(({ a, i }) => pushAtt('Morning', a, i));
  pushMeal('Lunch', meals.lunch);
  att.slice(mid).forEach(({ a, i }) => pushAtt('Afternoon', a, i));
  pushMeal('Tea', meals.tea);
  pushMeal('Dinner', meals.dinner);
  pushMeal('Supper', meals.supper);
  return out;
}

// ── /replan op helpers (pure; no network) ────────────────────────────────────

/** Integrity ref for /replan ops. Matches backend _attraction_ref_matches:
 *  wikidata OR name_en/name (lowered, server-side). wikidata-first for stability. */
export function attractionRef(
  a: { wikidata?: string | null; name_en?: string; name?: string } | null | undefined,
): string {
  return ((a?.wikidata || a?.name_en || a?.name) ?? '').trim();
}

/** Map a drag-drop MOVE to the /replan op pair (WITHIN ONE LEG only).
 *  src_pos/dst_pos are RAW indices into the respective day.attractions arrays.
 *  A move = remove_place (→ leg's unscheduled pool, with position+ref integrity check)
 *  then add_place from that pool at the target. The pair is atomic-in-practice: if
 *  remove_place is rejected (stale board), the ref isn't in the pool so add_place is
 *  also rejected → server returns noop, plan unchanged (no partial corruption). */
export function moveOps(args: {
  leg_index: number; src_day: number; src_pos: number; dst_day: number; dst_pos: number; ref: string;
}): ReplanOp[] {
  const { leg_index, src_day, src_pos, dst_day, dst_pos, ref } = args;
  // Same-day forward move: removing src shifts later raw indices down by one.
  const insert = src_day === dst_day && dst_pos > src_pos ? dst_pos - 1 : dst_pos;
  return [
    { op: 'remove_place', leg_index, day_index: src_day, position: src_pos, attraction_ref: ref },
    { op: 'add_place', leg_index, day_index: dst_day, position: insert, attraction_ref: ref, from: 'unscheduled' },
  ];
}

// ── per-hazard risk lines (each hazard its OWN line + % — never bunched) ──────
export interface HazardLine { label: string; pct: number; }
export function hazardLines(r: RiskSignal): HazardLine[] {
  const out: HazardLine[] = [];
  const cyc = r.cyclone_likelihood_pct;
  if (fin(cyc) && cyc > 0) out.push({ label: cap(r.cyclone_basin) || 'Cyclone', pct: round1(cyc) });
  const fl = bpToPct(r.flood_index_bp); if (fl != null) out.push({ label: 'Flood', pct: fl });
  const wf = r.wildfire_likelihood_pct; if (fin(wf) && wf > 0) out.push({ label: 'Wildfire', pct: round1(wf) });
  const dr = bpToPct(r.drought_index_bp); if (dr != null) out.push({ label: 'Drought', pct: dr });
  return out;
}

// ── map pins (coords are lat/lon → MapPin uses lng) ──────────────────────────
function pinOf(x: Attraction | Restaurant, category: MapPin['category'], city?: string): MapPin | null {
  if (!fin(x.lat) || !fin(x.lon)) return null;
  const name = displayName(x);
  return { lat: x.lat, lng: x.lon, label: name, name, city, category };
}
export function dayPins(day: DayDetail, city?: string): MapPin[] {
  const pins: MapPin[] = [];
  for (const a of day.attractions ?? []) { const p = pinOf(a, 'attraction', city); if (p) pins.push(p); }
  for (const m of Object.values(day.meals ?? {})) { if (m) { const p = pinOf(m, 'restaurant', city); if (p) pins.push(p); } }
  return pins;
}

/** All pins for the trip: scheduled attractions + meals + unscheduled candidates +
 *  a hotel pin per leg (labeled "approx (city centre)" since lodging carries no exact coords). */
export function planPins(plans: DayPlan[], legs: Leg[]): MapPin[] {
  const pins: MapPin[] = [];
  for (const dp of plans) {
    for (const d of dp.days ?? []) pins.push(...dayPins(d, dp.city));
    for (const a of dp.unscheduled_attractions ?? []) { const p = pinOf(a, 'attraction', dp.city); if (p) pins.push(p); }
  }
  for (const l of legs) {
    // #101 — prefer served hotel geocode coord; fall back to legacy leg lat/lng (city centroid)
    const hasHotelGeo = fin(l.hotel_lat) && fin(l.hotel_lon);
    const lat = hasHotelGeo ? l.hotel_lat! : (fin(l.lat) ? l.lat! : undefined);
    const lon = hasHotelGeo ? l.hotel_lon! : (fin(l.lng) ? l.lng! : undefined);
    if (fin(lat) && fin(lon)) {
      const exact = l.hotel_coord_basis === 'geocoded';
      pins.push({
        lat: lat!,
        lng: lon!,
        label: `${l.city ?? 'Hotel'} — ${exact ? 'hotel location' : 'approx (city centre)'}`,
        name: l.city ?? 'Hotel',
        city: l.city,
        category: 'lodging',
      });
    }
  }
  return pins;
}

// ── budget rows (USD, served totals only — never recomputed) ─────────────────
// Money-itemization honesty fix: fee estimates (insurance/visa/vaccine — see
// society/orchestration/orchestrator.py _inject_fees, this repo's root) are folded
// into package_total_with_fees_cents for DISPLAY, but are never actually charged
// to the wallet — only the lodging line (package_total_cents) is. Whenever both
// figures are known and fees are non-zero, itemize the split explicitly instead
// of showing one unqualified "total" that silently includes money nobody charges.
export interface BudgetRow { label: string; value: string; }
export function budgetRows(r: { fee_line_items?: Array<{ description?: string; kind?: string; usd_cents?: number }>;
  fee_total_cents?: number; package_total_cents?: number;
  package_total_with_fees_cents?: number; total_budget_cents?: number; }): BudgetRow[] {
  const rows: BudgetRow[] = [];
  // Real served shape (LineItemAssembler.add_fee, society/utils/line_item_assembler.py, this repo's root):
  // {description, kind, usd_cents, ...} -- there is no `label`/`amount_cents` field on the wire.
  // Fall back to `kind` (e.g. "visa"/"vaccine"/"premium") before the generic "Fee" so an entry with
  // an empty description is still identifiable, not just a bare number.
  const feeLineSum = (r.fee_line_items ?? []).reduce((s, f) => s + (f.usd_cents ?? 0), 0);
  const fees = r.fee_total_cents ?? feeLineSum;
  const total = r.package_total_with_fees_cents ?? r.package_total_cents;

  if (fees > 0 && r.package_total_cents != null) {
    // Honest split: lodging is the amount actually held/charged to the wallet;
    // the fee lines are estimates paid directly to third parties, never by us.
    rows.push({ label: 'Lodging (charged to wallet on booking)', value: centsToUsd(r.package_total_cents) });
    for (const f of r.fee_line_items ?? [])
      rows.push({ label: `${f.description || f.kind || 'Fee'} (est., paid separately)`, value: centsToUsd(f.usd_cents) });
    if (total != null) rows.push({ label: 'Trip total (lodging + est. fees)', value: centsToUsd(total) });
  } else {
    for (const f of r.fee_line_items ?? [])
      rows.push({ label: f.description || f.kind || 'Fee', value: centsToUsd(f.usd_cents) });
    if (total != null) rows.push({ label: 'Bookable package', value: centsToUsd(total) });
  }
  if (r.total_budget_cents != null) rows.push({ label: 'Your budget', value: centsToUsd(r.total_budget_cents) });
  return rows;
}

/** % of budget used (for the bar). Returns null when either side is missing. */
export function budgetPct(used?: number, budget?: number): number | null {
  if (!fin(used) || !fin(budget) || budget <= 0) return null;
  return Math.min(100, Math.round((used / budget) * 100));
}
