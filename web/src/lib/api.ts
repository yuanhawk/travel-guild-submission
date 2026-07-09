// Typed client for the Travel Guild backend (the `society` orchestrator).
//
// PURE PRESENTATION CONTRACT: this frontend RENDERS server-supplied values and
// NEVER recomputes prices / risk / scores client-side. The deterministic (var-0)
// core in the Python/Go backend stays the single source of truth. Field names
// below are the canonical served names (verified against /negotiate_text +
// /emergencies). Money is in CENTS everywhere.

// IDOR fix (see session.ts): authFields() supplies the ownership signals threaded
// onto trip-creation + every trip-scoped mutation below. This is a one-directional
// runtime import — session.ts only imports API's *types* (erased at compile time),
// so there's no real module cycle.
import { authFields } from './session';

export const API_BASE: string = (
  import.meta.env.VITE_API_BASE ?? 'http://localhost:8080'
).replace(/\/+$/, '');

// Guard: prevent plaintext API traffic in production builds.
// Only fires when VITE_API_BASE is explicitly set to a non-https URL.
// CI (no VITE_API_BASE set) and local dev (PROD=false) are unaffected.
const _rawBase = import.meta.env.VITE_API_BASE;
if (import.meta.env.PROD && _rawBase && !_rawBase.startsWith('https://')) {
  throw new Error('VITE_API_BASE must use https:// in production — got: ' + _rawBase);
}

// ─── unit helpers ──────────────────────────────────────────────────────────
export const centsToUsd = (c: number | null | undefined): string =>
  c == null ? '—' : `$${(c / 100).toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;

/** XSS-safe external-href guard for monetized booking deeplinks.
 *  Allows ONLY same-origin relative paths and https:// URLs (scheme match is
 *  case-insensitive per RFC 3986 §3.1, so Https://, HTTPS://, etc. are also
 *  allowed); blocks javascript:, data:, http:, and protocol-relative //host
 *  in ANY casing. Mirrors Aftercare.safePath() so the live-price deeplink is
 *  routed through the SAME guard. Returns '' when unsafe. */
export const safeHref = (raw: string | null | undefined): string => {
  if (!raw) return '';
  const s = raw.trim();
  if (s.startsWith('//')) return '';                  // protocol-relative → external, reject
  if (s.startsWith('/') || s.toLowerCase().startsWith('https://')) return s;
  return '';                                          // javascript:, data:, http:, … → reject
};

/** basis-points → percent (flood_index_bp / drought_index_bp are bp; ÷100). */
export const bpToPct = (bp: number | null | undefined): number | null =>
  bp == null ? null : Math.round((bp / 100) * 10) / 10;

// ─── map pin (shared by Map.svelte) ────────────────────────────────────────
export interface MapPin {
  lat: number;
  lng: number;
  label?: string;
  name?: string;   // clean place name for /place_card {place} (no decoration)
  city?: string;   // city for /place_card {city}
  category?: 'lodging' | 'restaurant' | 'attraction' | 'transit' | string;
}

// ─── indicative display-currency review (#77/#101) ────────────────────────
// Present only when: home_currency != USD, total > 0, currency seeded.
// Server-computed from the seeded FX table — FE only formats the served integer,
// never recomputes client-side. Honest: charges are always in USD; this is display
// guidance only, backed by a seeded snapshot with an explicit disclaimer.
export interface CurrencyReview {
  display_currency: string;
  usd_cents: number;
  indicative_minor_units: number;  // minor units of display_currency (e.g. cents for SGD)
  decimals: number;                // 2 for SGD/EUR, 0 for JPY, etc.
  as_of: string;                   // snapshot month e.g. "2026-06"
  indicative: boolean;             // always true — never a live rate
  basis: string;                   // "seeded_snapshot"
  disclaimer: string;
  exchange_timing?: { tier: string; guidance: string; caveat: string };
}

/** Format the server-supplied indicative figure for display. Pure formatting —
 *  NO FX math on the client; only formats the already-converted integer.
 *  Returns null if cr is absent or indicative===false. */
export const fmtIndicative = (cr: CurrencyReview | null | undefined): string | null => {
  if (!cr || !cr.indicative) return null;
  const major = cr.indicative_minor_units / Math.pow(10, cr.decimals);
  const s = major.toLocaleString('en-US', {
    minimumFractionDigits: cr.decimals,
    maximumFractionDigits: cr.decimals,
  });
  return `≈ ${cr.display_currency} ${s}`;
};

// ─── per-leg risk signal (risk_agent) ──────────────────────────────────────
export interface RiskAdvisory { type: string; severity: string; detail: string; }
export interface RiskDecisions { flag?: boolean; avoid_window?: boolean; buffer_connection_min?: number; }
export interface RiskOccurrence { max_likelihood_pct?: number | null; categorical_advisory_present?: boolean; band?: string; }
export interface RiskSignal {
  leg_id?: string;                      // per_leg entries carry the leg id (for leg-matching)
  region?: string | null;
  alert_tier?: string;                  // HIGH | MED | LOW | UNKNOWN | NONE
  cyclone_likelihood_pct?: number | null;
  cyclone_basin?: string | null;        // typhoon | hurricane | cyclone
  flood_index_bp?: number | null;       // ÷100 → %
  wildfire_likelihood_pct?: number | null;
  drought_index_bp?: number | null;     // ÷100 → %
  occurrence?: RiskOccurrence;
  reason_codes?: string[];
  city?: string;
  mode?: string;
  decisions?: RiskDecisions;            // NOTE: object — NOT `decision`
  advisory?: RiskAdvisory[];            // NOTE: array  — NOT `advisory_level`
  planning_note?: string | null;        // top-level sibling of advisory[]; L3/L4 guide-recommendation text
}

/** Consolidated risk block served as result.risk_signals — meta + per-leg array.
 *  (Verified: it's an object with `per_leg[]`, NOT a leg-keyed dict.) */
export interface RiskSignalsBlock {
  consolidator?: string;
  any_avoid_window?: boolean;
  buffer_connection_min?: number;
  flagged_legs?: string[];
  reason_codes?: string[];
  per_leg?: RiskSignal[];
}

// ─── live best-price overlay (DISPLAY-ONLY; PROD/live edition) ──────────────
// The price provider may attach a best-price overlay to a lodging/leg via an
// as_display_dict() shape. PURE DISPLAY: the FE only renders the served integer +
// the monetized booking deeplink; it never fetches prices or recomputes anything.
//
// UAT (seeded): there is NO overlay — the field is ABSENT, or present with
// status:'unavailable'. PROD (live): status:'ok' carries a best-price + deeplink + source.
// Either way the seeded display is the floor; the overlay is purely additive.
export interface BestPriceResult {
  status: 'ok';
  hotel?: string;
  lowest_price_cents: number;
  currency: string;          // ISO-4217 e.g. 'USD'
  deeplink: string;          // monetized OTA booking link (https://… — guarded before use)
  source: string;            // provider/source label e.g. 'ExamplePartner'
  fetched_at?: string;       // ISO timestamp the price was fetched
}
export interface UnavailableResult {
  status: 'unavailable';
  reason?: string;
}
export type PriceOverlay = BestPriceResult | UnavailableResult;

/** Return the live best-price overlay IFF it is present AND status:'ok' AND has the
 *  fields we render. In UAT (no overlay) or status:'unavailable' this returns null, so
 *  callers fall through to the EXISTING seeded display UNCHANGED (zero-regression).
 *  Reads either `price_overlay` or `live_price` (whichever the backend attaches). */
export function livePriceOverlay(
  src: { price_overlay?: unknown; live_price?: unknown } | null | undefined,
): BestPriceResult | null {
  if (!src) return null;
  const o = (src.price_overlay ?? src.live_price) as PriceOverlay | undefined;
  if (!o || typeof o !== 'object' || o.status !== 'ok') return null;
  if (typeof o.lowest_price_cents !== 'number' || !o.deeplink || !o.currency) return null;
  return o;
}

// ─── legs / day plans ──────────────────────────────────────────────────────
// ─── inter-city transport (multi-leg routing) ──────────────────────────────
// Served at the TOP LEVEL of the negotiate/replan envelope:
//   transport_edges  — one per consecutive-city hop; pure-presentation, never recomputed
//   transport_pricing — honest caveat about data vintage and booking responsibility
// Each leg after the first ALSO carries inbound_transport (ready display string fallback).
export interface TransportEdge {
  from_leg?: string;            // leg_id of the departure city
  to_leg?: string;              // leg_id of the arrival city
  from_city?: string;
  to_city?: string;
  from_area?: string;
  to_area?: string;
  feasible: boolean;            // false → no direct land route; show honest reason
  mode: string;                 // 'rail' | 'road' | 'flight' | 'ferry' | …
  transfer_minutes?: number;    // door-to-door estimate (integer)
  reason?: string;              // honest explanation when feasible===false
  rail_tier?: string;           // 'hsr' → show HSR chip; 'regional' | 'inter_city' | …
}
export interface TransportPricing {
  basis?: string;               // 'handoff'
  basis_label?: string;         // e.g. "Durations estimated (OpenFlights ~2014 vintage); fares not priced — book externally"
}

export interface Leg {
  leg_id?: string;
  city?: string;
  iso2?: string;
  country?: string;
  checkin?: string;
  checkout?: string;
  cost_cents?: number;
  // #42 PART A (core/cost_basis.py) — per-leg lodging total + honesty basis
  // disclosure. total_cents is the real per-leg cost (distinct from cost_cents,
  // which is unused/legacy — the server emits total_cents; see golden fixtures).
  total_cents?: number;
  cost_basis?: 'ucp_prepaid' | 'deterministic_estimate' | 'handoff' | 'unknown' | string;
  cost_basis_label?: string;            // e.g. "Prepaid via secure checkout"
  lat?: number; lng?: number;           // present when the leg is geocoded
  hotel?: Record<string, unknown>;
  hotel_title?: string;                 // display-name of the selected hotel (seeded catalog)
  unverified_lodging?: boolean;         // honesty flag — surface, never silently book
  note?: string;
  inbound_transport?: string;           // ready display string for the trip ARRIVING at this leg
  // DISPLAY-ONLY live best-price overlay (PROD/live edition). ABSENT in UAT (seeded).
  // Backend may attach under either key — read via livePriceOverlay().
  price_overlay?: PriceOverlay;
  live_price?: PriceOverlay;
  // #101 — additive hotel geocode fields (attached server-side, off var-0)
  hotel_lat?: number;                   // geocoded or centroid lat
  hotel_lon?: number;                   // geocoded or centroid lon
  hotel_coord_basis?: 'geocoded' | 'city_centroid';  // provenance label
  [k: string]: unknown;
}
// ─── #37/#211/#212 — booking_links: CONSTRUCTED reference/handoff deep-links ─
// (society/utils/booking_links.py). Every link is deterministically BUILT from
// known parameters (POI name, hotel name, IATA codes, wikidata id, website, …),
// never fetched or live-verified, and NEVER implies a confirmed booking — the
// honesty contract this module documents at length. `kind` is a closed
// discriminator; UAT/PROD both emit it, `affiliate_handoff` is PROD-only
// (insurance). `booking_url` may be null (e.g. flight meta_search / insurance
// compare_note, which carry only `providers`). Single shared definition —
// used by both #211's trip/leg-level block (RightRail.svelte) and #212's
// per-attraction/per-meal duplicates (Attraction.link/Restaurant.link below),
// per society/utils/booking_links.py's single `_link()` assembler.
export type BookingLinkKind =
  | 'official_site' | 'search_handoff' | 'meta_search' | 'maps'
  | 'reference' | 'gov_portal' | 'cdc_who_portal' | 'compare_note'
  | 'affiliate_handoff' | string;

export interface BookingLinkProvider { name: string; url: string; }

export interface BookingLink {
  booking_url?: string | null;
  kind?: BookingLinkKind;
  label?: string;
  providers?: BookingLinkProvider[] | null;
  provenance?: Record<string, unknown>;
  [k: string]: unknown;
}

// nested day-plan shapes (served under day_plans[].days[]). Coords are lat/lon.
export interface Attraction {
  name?: string; name_en?: string; category?: string;          // category = raw OSM tag e.g. "tourism=museum"
  lat?: number; lon?: number;
  fee?: number | string | null;                                 // ~always null in the catalog
  opening_hours?: string | null; website?: string | null;
  image?: string | null;
  // #37/#212 — CONSTRUCTED reference/handoff link (attraction_link(), society/utils/
  // booking_links.py), mutated in place onto this entity. NOT a plain URL string —
  // reuse the shared BookingLink shape; render via .booking_url through safeHref().
  link?: BookingLink | null;
  weather_exposure?: 'indoor' | 'outdoor' | 'mixed' | string | null;
  wikidata?: string | null; heritage?: string | null;
  [k: string]: unknown;
}
export interface Restaurant {
  name?: string; name_en?: string; category?: string;
  cuisine?: string | null; diet?: string | null;
  lat?: number; lon?: number;
  opening_hours?: string | null; outdoor_seating?: string | null;
  // #37/#212 — CONSTRUCTED reference/handoff link (restaurant_link()); same shape/
  // caveat as Attraction.link above.
  link?: BookingLink | null;
  fee?: number | string | null;
  [k: string]: unknown;
}
export interface Meals {
  breakfast?: Restaurant | null; lunch?: Restaurant | null; tea?: Restaurant | null;
  dinner?: Restaurant | null; supper?: Restaurant | null;
}
export interface IntracityHop {
  from?: string; to?: string; from_kind?: string; to_kind?: string;
  distance_km?: number; mode?: string; minutes?: number; label?: string;
  coord_basis?: string; estimate?: boolean;
}
export interface AirportTransfer {
  from?: string; to?: string; iata?: string; distance_km?: number;
  mode?: string; minutes?: number; label?: string;
  direction?: 'inbound' | 'outbound' | string;
}
/** Inline intra-city transport gap between two consecutive timeline items (UI #3).
 *  Server-computed from POI coords; pure presentation — never recomputed client-side.
 *  estimate is always true (haversine+speed model, no real-time data). */
export interface IntraCityGap { mode: string; minutes: number; estimate: boolean; }
export interface MealOption {
  name?: string; cuisine?: string; lat?: number; lon?: number;
  [k: string]: unknown;
}
export interface DayDetail {
  day_index?: number; bad_weather?: boolean;
  attractions?: Attraction[]; meals?: Meals; intracity_hops?: IntracityHop[];
  /** Inline transport gaps between consecutive timeline items (mealAnchoredTimeline order).
   *  N−1 entries for N timeline items; null = either endpoint lacks coords (honest suppression). */
  item_transport_gaps?: (IntraCityGap | null)[];
  /** Server-emitted alternatives per meal slot (up to 2). Drives the inline swap panel. */
  meal_pool?: Record<string, MealOption[]>;
  [k: string]: unknown;
}
export interface DayPlan {
  leg_id?: string; city?: string; iso2?: string; country?: string; region?: string;
  checkin?: string; checkout?: string; num_days?: number; catalog_hit?: boolean;
  days?: DayDetail[];
  unscheduled_attractions?: Attraction[];
  airport_transfer?: AirportTransfer[];
  notes?: string[];
  [k: string]: unknown;
}

// ─── emergencies (live overlay) ────────────────────────────────────────────
export type EmergencySeverity = 'high' | 'medium' | 'monitoring';
export interface EmergencyCountry {
  iso2: string; hazard: string; severity: EmergencySeverity;
  headline?: string; source?: string; as_of?: string;
}
export interface EmergenciesResponse {
  status: 'ok' | 'unavailable';
  source?: string; as_of?: string;
  countries: EmergencyCountry[];
}
/** per-trip escalation entries — result.active_emergencies. */
export interface ActiveEmergency {
  leg_id?: string; city?: string;
  status: 'active' | 'monitoring' | 'clear' | 'unavailable';
  hazard?: string; severity?: string; headline?: string; advice?: string;
  source?: string; as_of?: string; notice?: string; note?: string;
}

// ─── money / wallet / coverage sub-shapes ──────────────────────────────────
// Real served shape (LineItemAssembler.add_fee, society/utils/line_item_assembler.py, this repo's root):
// description/kind/usd_cents. There is no label/amount_cents field on the wire -- a prior version
// of this interface claimed there was, which is why the frontend read the wrong fields for months.
export interface FeeLineItem { description?: string; kind?: string; usd_cents?: number; [k: string]: unknown; }
export interface Wallet {
  balance_cents?: number; held_cents?: number; debit_cents?: number;
  debited?: boolean; note?: string; [k: string]: unknown;
}
export interface Insurance {
  premium_money?: Record<string, unknown>; premium_cents?: number;
  coverage?: string; [k: string]: unknown;
}

// Trip/leg-level (LOW cardinality — #211) + attractions[]/restaurants[]
// (HIGH-cardinality per-entity portion, mirrored via Attraction.link/
// Restaurant.link — #212). Uses the shared BookingLink shape defined above.
export interface BookingLinks {
  lodging?: BookingLink[];
  transport?: BookingLink[];
  attractions?: BookingLink[];
  restaurants?: BookingLink[];
  visa?: BookingLink | null;
  health?: BookingLink | null;
  insurance?: BookingLink | null;
  [k: string]: unknown;
}

// ─── itinerary narrative (LLM-on — structured object, NOT a flat string) ──
// The chat bubble reads .overview; per-leg summaries and per-day narratives
// are for the itinerary panel. Absent on LLM-off (deterministic / demo mode).
export interface NarrativeDay {
  day_number?: number;
  title?: string;
  narrative?: string;
  highlights?: { id: number; name: string; blurb: string }[];
  dining?: { id: number; name: string; blurb: string }[];
}
export interface NarrativeLeg {
  leg_id?: string;
  city?: string;
  summary?: string;
  days?: NarrativeDay[];
}
export interface ItineraryNarrative {
  overview?: string;
  legs?: NarrativeLeg[];
  _validation?: Record<string, unknown>;
  // Set by the backend after a structural /replan edit (server.py) — the narrative
  // text above was generated BEFORE that edit and may describe stops that were since
  // removed/added/reordered. Surface stale_reason wherever overview is rendered.
  stale?: boolean;
  stale_reason?: string;
}

// ─── negotiate result envelope ─────────────────────────────────────────────
export interface NegotiateResult {
  outcome?: string;                     // success | cannot_satisfy | insufficient_funds | …
  reason?: string | null;
  needs_clarification?: boolean;
  user_id?: string;
  legs?: Leg[];
  day_plans?: DayPlan[];
  risk_signals?: RiskSignalsBlock | null;
  active_emergencies?: ActiveEmergency[];
  fee_line_items?: FeeLineItem[];
  fee_total_cents?: number;
  total_booked_cents?: number;
  total_budget_cents?: number;
  package_total_cents?: number;
  package_total_with_fees_cents?: number;
  budget_shortfall_cents?: number;
  min_feasible_total_cents?: number;
  wallet_balance_cents?: number;
  wallet?: Wallet;
  insurance?: Insurance | null;
  health_verdict?: Record<string, unknown> | null;
  booking_ref?: string;
  idempotency_key?: string;
  payment_status?: string;              // 'held' (plan_ready) | 'charged' (booked)
  needs_reconciliation?: boolean;       // /confirm ambiguous → retry the SAME key
  total_cents?: number;                 // insufficient_funds terminal carries THIS (not package_total_cents)
  closest_package_total_cents?: number; // nearest feasible package on insufficient_funds
  itinerary_narrative?: ItineraryNarrative; // LLM-on only (honestly absent on LLM-off); object NOT string
  currency_review?: CurrencyReview;     // #101 — indicative display-currency (non-USD users; additive, var-0-safe)
  route?: string[];                     // multi-leg only — ordered ready strings e.g. ["Tokyo → Kyoto · rail · ~1h55m"]
  transport_edges?: TransportEdge[];    // one per consecutive-city hop (pure-presentation; never recomputed)
  transport_pricing?: TransportPricing | null; // honest caveat about data vintage / booking responsibility
  stream_id?: string;                   // present when {stream:true}
  // ── honest-assumption disclosures (backend attach_assumption_notes,
  //    society/utils/intent_parser.py) — set only when the parser guessed ──
  assumed_start_date?: string | null;        // ISO date the parser invented
  date_assumption_note?: string | null;
  assumed_date_year?: boolean | null;        // year-less date rolled forward
  date_year_assumption_note?: string | null;
  assumed_adults?: number | null;            // party size defaulted to 1
  adults_assumption_note?: string | null;
  assumed_currency?: string | null;          // bare-number budget → USD
  currency_assumption_note?: string | null;
  ignored_children?: number | null;          // children stated but not priced
  children_note?: string | null;
  // #205 — server-lifted honesty advisories (society/orchestration/orchestrator.py
  // ~L2118-2164): flat list[str], pooled from compliance flagged-leg advisories,
  // health flagged-destination advisories, risk per-leg advisory detail text, AND
  // day-planner honesty notes (dietary-fallback/no-restaurant/etc). No kind/category
  // field is emitted — plain prose strings only. Absent/empty when nothing to flag.
  advisories?: string[];
  // #37/#211 — CONSTRUCTED reference/handoff links (never a confirmed booking).
  // NOTE: legs[i].booking_link duplicates booking_links.lodging[i] in place
  // (society/utils/booking_links.py build_booking_links side effect) — render
  // from THIS top-level block only; do not also read legs[].booking_link.
  booking_links?: BookingLinks | null;
  [k: string]: unknown;
}

/** Honest-assumption notes the parser attached to this plan (server-authored
 *  wording — NEVER invent client-side). Empty array = nothing was guessed. */
export function assumptionNotes(r: NegotiateResult | null): string[] {
  if (!r) return [];
  return [
    r.date_assumption_note,
    r.date_year_assumption_note,
    r.adults_assumption_note,
    r.currency_assumption_note,
    r.children_note,
  ].filter((n): n is string => typeof n === 'string' && n.length > 0);
}

// ─── concrete trip date range (companion to the assumption-notes banner) ────
export interface TripDateRange {
  startISO: string;
  endISO: string;
  nights: number;
  label: string; // e.g. "Oct 1 – Oct 8, 2026 (7 nights)"
}

/** Parse a served 'YYYY-MM-DD…' date string as a UTC midnight Date — matches
 *  Preview.svelte's dateOf() so this never drifts a day from the itinerary's own
 *  date rendering under a non-UTC browser timezone. Returns null on unparseable input. */
function parseIsoDateUTC(iso: string): Date | null {
  const m = /^(\d{4})-(\d{2})-(\d{2})/.exec(iso);
  if (!m) return null;
  return new Date(Date.UTC(+m[1], +m[2] - 1, +m[3]));
}

/** The overall trip's concrete date range — earliest checkin to latest checkout across
 *  EVERY leg/day_plan (not just the first) — plus the resulting night count. This is the
 *  companion to assumptionNotes(): the notes narrate WHY a date was assumed ("no travel
 *  dates were given, so I assumed a start date of…"); this renders WHAT that assumption
 *  concretely produced, so the reader isn't left to parse a sentence to find the date.
 *  Pure presentation over server-supplied checkin/checkout — never fabricates a range:
 *  returns null when no leg/day_plan carries a checkin+checkout anywhere (mirrors the
 *  tripHasDates guard in App.svelte) or when the computed range is non-positive. */
export function tripDateRange(r: NegotiateResult | null): TripDateRange | null {
  if (!r) return null;

  const checkinDates: Date[] = [];
  const checkoutDates: Date[] = [];
  for (const l of r.legs ?? []) {
    if (l.checkin) { const d = parseIsoDateUTC(l.checkin); if (d) checkinDates.push(d); }
    if (l.checkout) { const d = parseIsoDateUTC(l.checkout); if (d) checkoutDates.push(d); }
  }
  for (const p of r.day_plans ?? []) {
    if (p.checkin) { const d = parseIsoDateUTC(p.checkin); if (d) checkinDates.push(d); }
    if (p.checkout) { const d = parseIsoDateUTC(p.checkout); if (d) checkoutDates.push(d); }
  }
  if (!checkinDates.length || !checkoutDates.length) return null;

  const start = new Date(Math.min(...checkinDates.map((d) => d.getTime())));
  const end = new Date(Math.max(...checkoutDates.map((d) => d.getTime())));
  const nights = Math.round((end.getTime() - start.getTime()) / 86400000);
  if (nights <= 0) return null; // defensive — never show a nonsensical/inverted range

  const fmt = (d: Date, withYear: boolean) =>
    d.toLocaleDateString('en-US', {
      month: 'short', day: 'numeric', timeZone: 'UTC',
      ...(withYear ? { year: 'numeric' as const } : {}),
    });
  const sameYear = start.getUTCFullYear() === end.getUTCFullYear();
  const label = `${fmt(start, !sameYear)} – ${fmt(end, true)} (${nights} night${nights === 1 ? '' : 's'})`;

  return { startISO: start.toISOString().slice(0, 10), endISO: end.toISOString().slice(0, 10), nights, label };
}

// ── outcome → UI state (both routes return HTTP 200; outcome is in the BODY) ──
export type UiState =
  | 'success' | 'plan_ready' | 'insufficient_funds' | 'budget_shortfall'
  | 'needs_clarification' | 'reconcile' | 'plan_expired'
  | 'declined' | 'invalid' | 'server_error' | 'empty';

export function uiState(r: NegotiateResult | null): UiState {
  if (!r) return 'empty';
  if (r.needs_clarification) return 'needs_clarification';
  const o = (r.outcome ?? '').toLowerCase();
  const reason = (r.reason ?? '').toLowerCase();
  if (o === 'success') return 'success';
  if (o === 'plan_ready') return 'plan_ready';          // HELD — awaiting the one /confirm consent
  if (reason.includes('insufficient_funds')) return 'insufficient_funds';
  // /confirm ambiguity → retry the SAME idempotency_key (NEVER re-book)
  if (r.needs_reconciliation || reason.includes('commit_errored') || reason.includes('reconcil'))
    return 'reconcile';
  // a held plan that no longer exists (expired / cancelled) → re-plan
  if (reason.includes('unknown_plan') || reason.includes('plan_cancelled')) return 'plan_expired';
  if (r.budget_shortfall_cents != null || r.min_feasible_total_cents != null) return 'budget_shortfall';
  if (reason.includes('invalid')) return 'invalid';
  if (reason.includes('server_error')) return 'server_error';
  if (o === 'cannot_satisfy') return 'declined';      // incl. do-not-recommend / armed-conflict
  return 'empty';
}

export interface NegotiateBody {
  text: string;
  plan?: boolean;                       // plan-only (held, no charge) → outcome:'plan_ready'
  user_id?: string;                     // demo-user identity (replayed across requests)
  nationality?: string;                 // passport for visa/compliance
  wallet_balance_cents?: number;        // SIMULATED prepaid wallet (cents)
  live_emergency?: { check: boolean };  // opt-in active-emergency overlay
  stream?: boolean;                     // returns {stream_id} for /stream/{id}
  narrate?: boolean;                    // request the LLM-generated itinerary_narrative
                                         // (qwen-turbo; adds ~14-30s) — INITIAL plan only,
                                         // never on refine/"make it cheaper" follow-ups.
  owner_token?: string;                 // IDOR fix: anonymous-trip ownership secret, bound
                                         // write-once at creation. NOT part of the request
                                         // digest (var-0-safe) — see session.ts authFields().
}

export interface ConfirmBody { idempotency_key: string; user_id?: string; session_token?: string; owner_token?: string; }

async function postJson<T>(path: string, body: unknown, signal?: AbortSignal): Promise<T> {
  const r = await fetch(`${API_BASE}${path}`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
    signal,
  });
  // 402 (insufficient_funds) / 403 (budget veto) carry a structured body we still parse.
  if (!r.ok && r.status !== 402 && r.status !== 403) {
    const body = await r.text();
    console.debug(`${path} error body:`, body.slice(0, 500));
    throw new Error(`${path} → HTTP ${r.status}`);
  }
  return (await r.json()) as T;
}

// IDOR fix — shared honest-refusal message for the 403 {outcome:'forbidden',
// reason:'not_trip_owner'} shape returned by /confirm, /cancel, /refine, /replan.
export const NOT_TRIP_OWNER_MESSAGE = 'This trip belongs to another session.';
// #203/#207 — backend now distinguishes WHY the 403 fired (society/orchestration/server.py
// _forbidden()): a logged-in user's session token missing/expired/wiped by a backend
// restart (reason:'session_invalid' — actionable, re-login fixes it) vs a genuine
// cross-user ownership violation (reason:'not_trip_owner' — unchanged). Both still carry
// {outcome:'forbidden', idempotency_key} at the top level; only `reason` distinguishes them.
export const SESSION_INVALID_MESSAGE = 'Your session expired — log in again and retry.';
export const isForbidden = (r: { outcome?: string } | null | undefined): boolean =>
  r?.outcome === 'forbidden';
/** Picks the right honest-refusal copy for a 403 {outcome:'forbidden'} body. Defaults to
 *  NOT_TRIP_OWNER_MESSAGE when `reason` is absent/unrecognized — backward-compat with a
 *  backend that hasn't deployed #203 yet (never crashes/shows undefined). */
export const forbiddenMessage = (r: { reason?: string | null } | null | undefined): string =>
  r?.reason === 'session_invalid' ? SESSION_INVALID_MESSAGE : NOT_TRIP_OWNER_MESSAGE;

export function negotiateText(body: NegotiateBody, signal?: AbortSignal): Promise<NegotiateResult> {
  // IDOR fix: bind this browser session's anonymous-trip secret at CREATION time so an
  // anonymous trip can later be confirmed/cancelled/refined/replanned by its creator.
  // Additive only — owner_token is NOT part of the server's request_digest, so it never
  // changes which plan is produced (var-0-safe).
  //
  // M1 follow-up — ALSO send session_token here (previously only owner_token
  // was sent). The travel_memory personalization write/read at plan-creation time now
  // requires it to trust a logged-in user_id claim (backend: server.py
  // _memory_verified_user_id); omitting it is harmless (personalization is simply
  // skipped, never an error), but a real session-holder should still get their
  // personalization. Still additive/var-0-safe: session_token, like owner_token, is not
  // part of the server's request_digest.
  return postJson<NegotiateResult>('/negotiate_text', { ...body, ...authFields() }, signal);
}

/** The ONE human consent: commit a held plan → booked (idempotent; retrying the same
 *  key NEVER double-books or double-charges). IDOR fix: session_token/owner_token prove
 *  ownership server-side (see session.ts authFields()) — a non-owner gets a 403
 *  {outcome:'forbidden', reason:'not_trip_owner'}. */
export function confirmPlan(idempotency_key: string, user_id?: string): Promise<NegotiateResult> {
  return postJson<NegotiateResult>('/confirm', { idempotency_key, user_id, ...authFields() } satisfies ConfirmBody);
}

/** GET /trips/{key} — the sanitized stored plan envelope, for restoring the itinerary
 *  after a page reload. IDOR fix (#155): thread this session's ownership proof as
 *  REQUEST HEADERS (X-Session-Token for logged-in trips, X-Owner-Token for anonymous)
 *  so the server's two-tier gate can authorize the read — same authFields() sent on
 *  POST /confirm etc., now via headers rather than the query string (secrets-in-URL
 *  fix — see _authHeaders' docstring) so this session's bearer tokens never land in
 *  an access log, browser history, or Referer header. A non-owner (or expired token)
 *  gets a 404, which we map to null exactly like a genuinely-gone trip → caller
 *  clears the stale key. Returns null on any non-2xx. */
export async function getTrip(idempotency_key: string): Promise<NegotiateResult | null> {
  const { session_token, owner_token } = authFields();
  const r = await fetchAuthed(`/trips/${encodeURIComponent(idempotency_key)}`, session_token, owner_token);
  if (!r.ok) return null;
  return (await r.json()) as NegotiateResult;
}

export interface TripSummary {
  idempotency_key: string; user_id: string; status?: string; outcome?: string;
  booking_ref?: string | null; package_total_cents?: number | null;
  created_at?: string; confirmed_at?: string | null;
}

/** GET /trips?user_id= — this user's past trips (held + booked), newest first. */
export async function listMyTrips(user_id: string): Promise<TripSummary[]> {
  const r = await fetch(`${API_BASE}/trips?user_id=${encodeURIComponent(user_id)}`);
  if (!r.ok) return [];
  const d = await r.json();
  return (d.trips ?? []) as TripSummary[];
}

export interface CancelResult {
  outcome: 'cancelled' | 'already_cancelled' | 'invalid_request' | 'forbidden' | string;
  refunded_cents?: number;
  wallet_credit_cents?: number;
  wallet_balance_cents?: number;
  reason?: string;
  idempotency_key?: string;   // present on the 403 {outcome:'forbidden'} body
}

/** POST /cancel — void a booked trip and refund the SIMULATED wallet. Idempotent:
 *  a second cancel returns already_cancelled with no double refund. IDOR fix:
 *  session_token/owner_token prove ownership server-side — a non-owner gets a 403
 *  {outcome:'forbidden', reason:'not_trip_owner'} (see session.ts authFields()). */
export function cancelTrip(idempotency_key: string, user_id?: string): Promise<CancelResult> {
  return postJson<CancelResult>('/cancel', { idempotency_key, user_id, ...authFields() });
}

export async function getEmergencies(): Promise<EmergenciesResponse> {
  return getJson<EmergenciesResponse>('/emergencies');
}

// ─── demo-user session + preferences (slice 4) ─────────────────────────────
// Pre-seeded demo travellers — NO registration (judges pick one). Selecting a
// user replays its `user_id` on every plan/confirm so the backend auto-applies
// that profile's currency / nationality / persona-preset (var-0-safe, merged
// server-side BEFORE the deterministic core).
export interface DemoUser {
  user_id: string; display_name: string; persona: string;
  nationality: string; home_currency: string; wallet_balance_cents: number;
}
export interface Preferences extends DemoUser { prefs?: Record<string, unknown>; }

async function getJson<T>(path: string): Promise<T> {
  const r = await fetch(`${API_BASE}${path}`);
  if (!r.ok) throw new Error(`${path} → HTTP ${r.status}`);
  return (await r.json()) as T;
}

// SECURITY: session_token/owner_token are bearer-equivalent ownership secrets
// (8h TTL) — they must never ride in a URL query string, where they'd land in
// server/proxy access logs, browser history, and any Referer header sent to a
// cross-origin resource loaded by the page. Sent as request headers instead;
// server.py reads the same headers (case-insensitive; lowercased here because
// the browser's Headers object normalizes to lowercase anyway).
function _authHeaders(session_token?: string | null, owner_token?: string | null): HeadersInit {
  const headers: Record<string, string> = {};
  if (session_token) headers['x-session-token'] = session_token;
  if (owner_token) headers['x-owner-token'] = owner_token;
  return headers;
}

async function fetchAuthed(
  path: string, session_token?: string | null, owner_token?: string | null,
): Promise<Response> {
  return fetch(`${API_BASE}${path}`, { headers: _authHeaders(session_token, owner_token) });
}

async function getJsonAuthed<T>(
  path: string, session_token?: string | null, owner_token?: string | null,
): Promise<T> {
  const r = await fetchAuthed(path, session_token, owner_token);
  if (!r.ok) throw new Error(`${path} → HTTP ${r.status}`);
  return (await r.json()) as T;
}
async function sendJson<T>(method: string, path: string, body: unknown): Promise<T> {
  const r = await fetch(`${API_BASE}${path}`, {
    method, headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
  });
  if (!r.ok) throw new Error(`${path} → HTTP ${r.status}`);
  return (await r.json()) as T;
}

/** List the pre-seeded demo travellers. */
export function listDemoUsers(): Promise<{ demo_users: DemoUser[] }> {
  return postJson<{ demo_users: DemoUser[] }>('/session', {});
}
/** Select a demo traveller by id → their profile (404 → throws). */
export function selectDemoUser(user_id: string): Promise<{ user: DemoUser }> {
  return postJson<{ user: DemoUser }>('/session', { user_id });
}
/** session_token is OPTIONAL for now (backend not yet gated — see
 *  "SECURITY FOLLOW-UP" in society/orchestration/server.py) but threaded
 *  through so the backend can be gated in a follow-up without another FE
 *  change. Unlike getTelegramLinkToken (whose session_token is REQUIRED
 *  server-side, so it's always sent, even empty), this one is only appended
 *  when present. SECURITY (secrets-in-URL fix): sent as the X-Session-Token
 *  request header, not a query param — bearer-equivalent secrets must never
 *  land in an access log, browser history, or a Referer header. */
export function getPreferences(user_id: string, session_token?: string): Promise<Preferences> {
  return getJsonAuthed<Preferences>(`/preferences?${new URLSearchParams({ user_id })}`, session_token);
}

// ─── session login (SECURITY: closes the user_id-trust gap) ───────────────
// PUT /preferences and GET /telegram/link both act directly AS the claimed
// user_id, so a bare user_id is not authorization — POST /session/login
// issues a reusable, server-verified session_token for a KNOWN demo user
// (no register/signup) that must be threaded into both calls below. Called
// once, right after a demo user is picked (see App.svelte's pickUser()).
export interface LoginSessionResponse { session_token: string }
export function loginSession(user_id: string): Promise<LoginSessionResponse> {
  return postJson<LoginSessionResponse>('/session/login', { user_id });
}

/** Update an existing profile (update-only; 404 on unknown — NO register).
 *  session_token is REQUIRED server-side (401 without a valid one matching
 *  user_id) — see loginSession(). */
export function putPreferences(
  p: Partial<Preferences> & { user_id: string }, session_token?: string,
): Promise<Preferences> {
  return sendJson<Preferences>('PUT', '/preferences', { ...p, session_token });
}

// ─── Telegram account-linking deep link (Preferences → Connect Telegram) ──────
// GET /telegram/link issues a short-lived, single-use deep-link token. Honest
// degradation: `available:false` (+ `reason`) when the deployment has no bot
// configured — never a broken/fake link. `deep_link` opens t.me/<bot>?start=<token>;
// tapping Start in Telegram resolves the token server-side (bot /start handler)
// and persists chat_id into prefs.telegram_chat_id — no chat_id ever touches the FE.
export interface TelegramLinkResponse {
  available: boolean;
  deep_link?: string;
  expires_in_seconds?: number;
  reason?: string;
}
/** session_token is REQUIRED server-side (401 without a valid one matching
 *  user_id) — see loginSession(). SECURITY (secrets-in-URL fix): sent as the
 *  X-Session-Token request header, not a query param. */
export function getTelegramLinkToken(user_id: string, session_token?: string): Promise<TelegramLinkResponse> {
  return getJsonAuthed<TelegramLinkResponse>(`/telegram/link?${new URLSearchParams({ user_id })}`, session_token);
}

/** The per-leg risk signals live in `risk_signals.per_leg`. */
export function riskSignalList(rs: NegotiateResult['risk_signals']): RiskSignal[] {
  return rs?.per_leg ?? [];
}

// ─── /refine — conversational session-context follow-up ────────────────────
export interface RefineBody {
  idempotency_key: string;
  message: string;
  session_id?: string;
  user_id?: string;
  wallet_balance_cents?: number;
  session_token?: string;   // IDOR fix — see session.ts authFields()
  owner_token?: string;     // IDOR fix — see session.ts authFields()
}

export interface RefineResult {
  outcome: string;                // plan_ready | refine_partial | refine_unsupported | unknown_plan | plan_locked | …
  session_id?: string;            // stable thread id — store and replay on subsequent refines
  idempotency_key?: string;       // NEW key after a successful re-plan (frontend must swap)
  assistant_reply?: string;       // truthful diff summary built from actual plan diff (never from LLM)
  changed?: string[];             // list of change descriptors e.g. ["budget.adjust:-15%","legs.add:osaka"]
  delta_applied?: Record<string, unknown>; // the parsed delta ops that were applied
  plan?: NegotiateResult;         // full new plan_ready envelope (same shape as /negotiate_text)
  kept_previous?: boolean;        // true when re-plan failed and the old plan is still held
  reason?: string;                // #203 — present on the 403 {outcome:'forbidden'} body: 'session_invalid' | 'not_trip_owner'
}

/** Conversational follow-up on a held plan. Returns a new plan_ready (with new idempotency_key)
 *  on success, or an honest partial/unsupported reply when the change can't be applied.
 *  IDOR fix: session_token/owner_token prove ownership server-side — a non-owner gets a 403
 *  {outcome:'forbidden', reason:'not_trip_owner'} (see session.ts authFields()). authFields()
 *  is spread AFTER body so it always wins over any stale value the caller might pass. */
export function refineTrip(body: RefineBody): Promise<RefineResult> {
  return postJson<RefineResult>('/refine', { ...body, ...authFields() });
}

// ─── place card (#99 — server-proxied Google Places detail) ────────────────
// FE NEVER holds the Google key. We only ever call our own /place_card and
// /place_photo (both server-proxied). Photo refs are OPAQUE — they resolve
// server-side only; the FE must never build a Google URL or embed a key.
export interface PlaceReview {
  author_name?: string;
  rating?: number;             // 1..5
  text?: string;
  relative_time_description?: string;
}
export interface PlaceCard {
  status: 'ok' | 'unavailable'; // HTTP is always 200; truth lives in `status`
  name?: string;
  rating?: number;              // 0..5, one decimal
  // Real served field name (society/utils/places_card.py: "user_rating_count": p.get("userRatingCount")).
  // A prior version of this interface declared `user_ratings_total` (Google's legacy v0 API name),
  // which never matched the wire shape -- PinDetailContent.svelte silently dropped the review count.
  user_rating_count?: number;
  reviews?: PlaceReview[];
  open_now?: boolean;
  opening_hours?: string[];     // weekday_text lines
  photo_refs?: string[];        // OPAQUE refs → feed to placePhotoUrl()
}
export interface PlaceCardBody { mode: 'detail'; place: string; city: string; }

// Wire envelope actually served by society/utils/places_card.py: the useful
// fields live NESTED under `place`, using different names than PlaceCard's flat
// contract, and `photos` are already-built "/place_photo?ref=<opaque>" paths
// rather than bare refs. This drifted silently (map pins rendered empty in
// prod — every field below was undefined against the wire response) because
// nothing exercised the real nested shape; see PlaceCardWire tests below.
interface PlaceCardWireReview { author?: string; rating?: number; text?: string; }
interface PlaceCardWirePlace {
  display_name?: string;
  rating?: number;
  user_rating_count?: number;
  open_now?: boolean;
  photos?: string[];             // "/place_photo?ref=<opaque>" paths, not bare refs
  reviews?: PlaceCardWireReview[];
}
interface PlaceCardWire {
  status?: unknown;
  source?: string;
  as_of?: string;
  place?: PlaceCardWirePlace;
}

/** Pull the opaque `ref` query param out of a served "/place_photo?ref=<opaque>"
 *  path. Falls back to the raw string if it isn't in that shape (defensive —
 *  never throws), since photo_refs feed straight into placePhotoUrl(). */
function _refFromPhotoPath(p: string): string {
  const m = /[?&]ref=([^&]+)/.exec(p);
  return m ? decodeURIComponent(m[1]) : p;
}

/** Normalize the server's nested {place:{...}} envelope into the flat PlaceCard
 *  shape PinDetailContent.svelte renders. Unavailable/malformed → {status:'unavailable'}. */
function _normalizePlaceCard(wire: PlaceCardWire | null | undefined): PlaceCard {
  if (!wire || typeof wire.status !== 'string' || wire.status !== 'ok' || !wire.place) {
    return { status: 'unavailable' };
  }
  const p = wire.place;
  return {
    status: 'ok',
    name: p.display_name,
    rating: p.rating,
    user_rating_count: p.user_rating_count,
    open_now: p.open_now,
    photo_refs: p.photos?.map(_refFromPhotoPath),
    reviews: p.reviews?.map((r) => ({ author_name: r.author, rating: r.rating, text: r.text })),
  };
}

// In-memory cache: resolves once per (place|city). A 200 {status:'unavailable'}
// is a DEFINITIVE answer (key off / no match) → cached. A thrown network error
// is NOT cached (entry deleted) so a re-click can retry honestly.
const _placeCardCache = new Map<string, Promise<PlaceCard>>();
const _pcKey = (place: string, city: string): string =>
  `${place.trim().toLowerCase()}|${city.trim().toLowerCase()}`;

/** Server-proxied Google Places detail for one pin. Cached per (place,city).
 *  Always returns a PlaceCard; honesty lives in `.status`. */
export function placeCard(place: string, city: string): Promise<PlaceCard> {
  const key = _pcKey(place, city);
  const hit = _placeCardCache.get(key);
  if (hit) return hit;
  const p = postJson<PlaceCardWire>('/place_card', { mode: 'detail', place, city } satisfies PlaceCardBody)
    .then(_normalizePlaceCard)
    .catch((e) => { _placeCardCache.delete(key); throw e; }); // don't cache errors
  _placeCardCache.set(key, p);
  return p;
}

/** Build the <img src> for an OPAQUE photo ref. Server holds the key and
 *  resolves the ref → image bytes. Never construct a googleapis URL here. */
export function placePhotoUrl(ref: string): string {
  return `${API_BASE}/place_photo?ref=${encodeURIComponent(ref)}`;
}

/** Test-only: reset the place-card cache. */
export function __clearPlaceCardCache(): void { _placeCardCache.clear(); }

// ─── /replan — item-level deterministic edit lane (#97/#98) ─────────────────
// Pure server-side envelope transform of a HELD or BOOKED plan's day_plans.
// Money/booking fields are preserved verbatim (fail-closed money guard server-side);
// /replan NEVER books or debits. The FE NEVER fabricates the result — it always
// renders the `plan` envelope the server returns (or leaves the plan unchanged on noop).
//
// Items are addressed by a COMPOSITE HANDLE (leg_index, day_index, position) plus an
// attraction_ref integrity check (wikidata|name_en|name). position indexes the RAW
// day.attractions list (see itinerary.ts attractionRef/moveOps). A stale FE board →
// honest entry in rejected[], never a silent wrong-item edit.
export type ReplanOp =
  | { op: 'remove_place'; leg_index: number; day_index: number; position: number; attraction_ref: string }
  | { op: 'add_place'; leg_index: number; day_index: number; position: number; attraction_ref: string; from?: string }
  | { op: 'swap_place'; leg_index: number; day_index: number; position: number; remove_ref: string; add_ref: string; from?: string }
  | { op: 'switch_variant'; leg_index: number; day_index: number; variant: 'fair' | 'wet' }
  | { op: 'reflow_from'; leg_index: number; from_day_index: number }
  | { op: 'swap_meal'; leg_index: number; day_index: number; slot: string; restaurant_name: string }
  // itinerary-scale-draft4.html backend-note: day-tab drag-to-reorder swaps two days'
  // FULL content (day_a <-> day_b), never changes day count/reindexes. NOT YET IMPLEMENTED
  // server-side as of this writing (grepped this repo's society/: no swap_days handler) —
  // the frontend sends this op and surfaces whatever honest rejection the server returns
  // via the existing runOps() generic-failure path, same as any other unsupported op.
  | { op: 'swap_days'; leg_index: number; day_a: number; day_b: number };

export interface ReplanRejection { op: string; reason: string }

// Success is ALWAYS 'plan_ready' (even for in-trip edits on a BOOKED plan — booked-ness
// lives in plan.payment_status/booking_ref, NOT here). 'noop' = every op rejected, plan
// returned UNCHANGED. The rest are honest terminal/error states.
export type ReplanOutcome =
  | 'plan_ready' | 'noop' | 'unknown_plan' | 'plan_locked' | 'cannot_satisfy' | 'invalid_request'
  | 'forbidden';   // IDOR fix: 403 not_trip_owner (see session.ts authFields())

export interface ReplanResponse {
  outcome: ReplanOutcome | string;
  idempotency_key?: string;
  applied: string[];              // diff tokens e.g. "remove:leg0.day1.pos2:senso-ji"
  rejected: ReplanRejection[];    // {op, reason} — honest stale-board / not-in-pool failures
  notes: string[];                // honest advisories e.g. pace-cap exceeded after add
  plan: NegotiateResult;          // FULL updated envelope (or the UNCHANGED one on noop/cannot_satisfy)
  reason?: string;                // money_invariant_violation, cancelled status, etc.
  detail?: string;
}

export interface ReplanBody {
  idempotency_key: string; ops: ReplanOp[]; user_id?: string;
  session_token?: string;   // IDOR fix — see session.ts authFields()
  owner_token?: string;     // IDOR fix — see session.ts authFields()
}

/** Apply item-level structured edits to a held OR booked plan. Returns the updated
 *  envelope. The key is STABLE across edits (unlike /refine). 402/403 tolerated by postJson.
 *  IDOR fix: session_token/owner_token prove ownership server-side — a non-owner gets a 403
 *  {outcome:'forbidden', reason:'not_trip_owner'} (see session.ts authFields()). */
export function replanTrip(
  idempotency_key: string, ops: ReplanOp[], user_id?: string,
): Promise<ReplanResponse> {
  return postJson<ReplanResponse>('/replan', { idempotency_key, ops, user_id, ...authFields() } satisfies ReplanBody);
}

/** True iff the edit actually changed the plan (success AND at least one op applied).
 *  On 'noop'/'cannot_satisfy' the returned plan is the UNCHANGED original — caller must
 *  NOT swap it in. */
export function replanApplied(r: ReplanResponse): boolean {
  return r.outcome === 'plan_ready' && (r.applied?.length ?? 0) > 0;
}

/** Booked-ness lives in the envelope (outcome is always 'plan_ready' on /replan success). */
export function isBookedEnvelope(r: NegotiateResult | null | undefined): boolean {
  return !!r && (r.payment_status === 'charged' || !!r.booking_ref);
}
