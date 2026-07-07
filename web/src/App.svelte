<script lang="ts">
  import { onMount } from 'svelte';
  import { negotiateText, confirmPlan, refineTrip, centsToUsd, uiState,
           listDemoUsers, getPreferences, isBookedEnvelope, getTrip, cancelTrip, listMyTrips,
           loginSession, isForbidden, forbiddenMessage, assumptionNotes, tripDateRange } from './lib/api';
  import type { NegotiateResult, UiState, DemoUser, Preferences as Prefs, EmergenciesResponse } from './lib/api';
  import { downloadIcs } from './lib/ics';
import { placeSheetOpen } from './lib/mapStore';
  import { loadSession, saveSession, clearSession, setActivePlan, clearActivePlan } from './lib/session';
  import type { ActivePlan, Session } from './lib/session';
  import ChatPane from './lib/components/ChatPane.svelte';
  import Itinerary from './lib/components/Itinerary.svelte';
  import RightRail from './lib/components/RightRail.svelte';
  import Preview from './lib/components/Preview.svelte';
  import SessionPicker from './lib/components/SessionPicker.svelte';
  import Preferences from './lib/components/Preferences.svelte';
  import { planStreaming } from './lib/planStream';
  import { initProgress, reduceProgress } from './lib/progress';
  import type { ProgressState } from './lib/progress';
  import LiveProgress from './lib/components/LiveProgress.svelte';
  import SafetyWatch from './lib/components/SafetyWatch.svelte';
  import { safeGetEmergencies } from './lib/safety';
  import DateRangePicker from './lib/components/DateRangePicker.svelte';
  import { itineraryLightboxOpen } from './lib/mapStore';

  // Destination hero photo thumbnails (112px, licensed via Wikimedia Commons —
  // see DATA-ATTRIBUTIONS.md). Kept as named imports so Vite fingerprints/
  // bundles them; the 1280px originals live alongside for future use (e.g. a
  // map-card slider) but are not shipped into these small guide cards.
  import japanThumb from './lib/assets/destinations/thumbs/japan.jpg';
  import baliThumb from './lib/assets/destinations/thumbs/bali.jpg';
  import thailandThumb from './lib/assets/destinations/thumbs/thailand.jpg';
  import vietnamThumb from './lib/assets/destinations/thumbs/vietnam.jpg';
  import singaporeThumb from './lib/assets/destinations/thumbs/singapore.jpg';
  import southKoreaThumb from './lib/assets/destinations/thumbs/south-korea.jpg';
  import franceThumb from './lib/assets/destinations/thumbs/france.jpg';
  import spainThumb from './lib/assets/destinations/thumbs/spain.jpg';
  import italyThumb from './lib/assets/destinations/thumbs/italy.jpg';
  import greeceThumb from './lib/assets/destinations/thumbs/greece.jpg';
  import portugalThumb from './lib/assets/destinations/thumbs/portugal.jpg';
  import netherlandsThumb from './lib/assets/destinations/thumbs/netherlands.jpg';
  import usaThumb from './lib/assets/destinations/thumbs/usa.jpg';
  import mexicoThumb from './lib/assets/destinations/thumbs/mexico.jpg';
  import colombiaThumb from './lib/assets/destinations/thumbs/colombia.jpg';
  import peruThumb from './lib/assets/destinations/thumbs/peru.jpg';
  import canadaThumb from './lib/assets/destinations/thumbs/canada.jpg';
  import costaRicaThumb from './lib/assets/destinations/thumbs/costa-rica.jpg';
  import dubaiThumb from './lib/assets/destinations/thumbs/dubai.jpg';
  import moroccoThumb from './lib/assets/destinations/thumbs/morocco.jpg';
  import egyptThumb from './lib/assets/destinations/thumbs/egypt.jpg';
  import kenyaThumb from './lib/assets/destinations/thumbs/kenya.jpg';
  import jordanThumb from './lib/assets/destinations/thumbs/jordan.jpg';
  import southAfricaThumb from './lib/assets/destinations/thumbs/south-africa.jpg';

  let walletUsd = 5000;
  let loading = false;
  let confirming = false;
  let result: NegotiateResult | null = null;
  let chatCollapsed = false;
  // Bound to ChatPane's mobile sheet — lets the guide-panel's assistant header
  // (visually the more prominent affordance on mobile) open the same sheet the
  // floating bubble does, instead of sitting there inert (missed-assistant bug).
  let preplanMobOpen = false;
  // Cancel a booked trip → /cancel (refunds the SIMULATED wallet; idempotent).
  let cancelling = false;
  async function cancelBooking(): Promise<void> {
    const key = result?.idempotency_key ?? activePlan?.idempotency_key;
    if (!key || cancelling) return;
    cancelling = true;
    const mySeq = reqSeq;                  // race guard (capture only — `cancelling` above already blocks
                                            // self-reentry; this catches a concurrent logout/new-plan instead)
    try {
      const c = await cancelTrip(key, activeUser?.user_id);
      if (mySeq !== reqSeq) return;        // a logout/new-plan superseded this cancel — drop the stale result
      if (isForbidden(c)) {
        // IDOR fix: this session's session_token/owner_token don't match the trip's
        // actual owner — honest refusal, plan state untouched (NOT cleared).
        // #203: distinguishes an expired/wiped session (actionable) from a genuine
        // cross-user ownership violation.
        messages = [...messages, { role: 'note', text: forbiddenMessage(c) }];
      } else if (c.outcome === 'cancelled') {
        const back = c.refunded_cents ?? c.wallet_credit_cents;
        messages = [...messages, { role: 'note',
          text: `Booking cancelled — ${back != null ? centsToUsd(back) + ' refunded to' : 'refunded to'} your SIMULATED wallet.` }];
        if (c.wallet_balance_cents != null) walletUsd = c.wallet_balance_cents / 100;
        result = null; activePlan = null;
        const sess = loadSession(); if (sess) saveSession(setActivePlan(sess, null));
      } else if (c.outcome === 'already_cancelled') {
        messages = [...messages, { role: 'note', text: `This booking was already cancelled.` }];
        result = null; activePlan = null;
      } else {
        messages = [...messages, { role: 'note', text: `Couldn't cancel: ${c.reason ?? c.outcome}` }];
      }
    } catch (e) {
      if (mySeq === reqSeq) {
        messages = [...messages, { role: 'note', text: `Cancel failed: ${String(e)}` }];
      }
    } finally { cancelling = false; }
  }
  let showPreview = false;                // booked → keepable trip summary / save-to-phone
  let progress: ProgressState | null = null;
  let streamRun: { close: () => void } | null = null;
  let reqSeq = 0;
  let sessionSeq = 0;  // separate generation counter for session/nav async ops (pickUser, openPrefs) —
                        // reqSeq is scoped to trip-planning requests and is NOT bumped by login/logout races
  let safety: EmergenciesResponse | null = null;
  let safetyOpen = false;
  let messages: Array<{ role: 'user' | 'bot' | 'note'; text: string }> = [
    { role: 'bot', text: `Describe your trip and I'll plan it — multi-city, budget-aware, safety-checked.` },
  ];

  // ── session (slice 4): pick a demo traveller (or guest) → replay user_id ──
  let view: 'loading' | 'picker' | 'app' | 'prefs' = 'loading';
  let activeUser: DemoUser | null = null;     // null = guest (system defaults)
  let demoUsers: DemoUser[] = [];
  let sessionError: string | null = null;
  let prefsProfile: Prefs | null = null;

  // ── conversational session context (B6): active plan handle ─────────────
  let activePlan: ActivePlan | null = null;   // set after first plan_ready; cleared on new trip
  let priorSessionOnLogout: Session | null = null;  // captured by switchUser(), consumed once by pickUser() to detect a same-account relogin

  onMount(async () => {
    safeGetEmergencies().then((r) => (safety = r));
    const s = loadSession();
    if (s && (s.user || s.guest)) {            // returning visitor: restore choice
      activeUser = s.user;
      if (s.user) walletUsd = s.user.wallet_balance_cents / 100;
      // Restore active plan handle (so follow-ups survive page reload).
      activePlan = s.activePlan ?? null;
      // Re-fetch the plan envelope so the ITINERARY survives a reload (not just the key).
      if (activePlan?.idempotency_key) {
        const env = await getTrip(activePlan.idempotency_key);
        if (env) { result = env; }
        else { activePlan = null; saveSession(setActivePlan(s, null)); }  // stale/gone → clear
      }
      view = 'app';
      return;
    }
    try {
      demoUsers = (await listDemoUsers()).demo_users ?? [];
    } catch (e) {
      sessionError = String(e);                // picker still offers "guest"
    }
    view = 'picker';
  });

  async function pickUser(u: DemoUser): Promise<void> {
    const mySeq = ++sessionSeq;                  // race guard: invalidated by a second pickUser/continueGuest/switchUser
    activeUser = u; walletUsd = u.wallet_balance_cents / 100;
    // Same account logging back in within this browser session → restore its held plan
    // (mirrors the onMount page-reload restore above). A genuine switch to a DIFFERENT
    // demo persona (or first-ever login) stays cleared.
    const priorPlan = priorSessionOnLogout?.user?.user_id === u.user_id
      ? (priorSessionOnLogout?.activePlan ?? null) : null;
    priorSessionOnLogout = null;
    let restored = false;
    if (priorPlan?.idempotency_key) {
      const env = await getTrip(priorPlan.idempotency_key);
      if (mySeq !== sessionSeq) return;           // a newer login/guest/logout won — drop this stale restore
      if (env) { result = env; activePlan = priorPlan; restored = true; }
    }
    // SECURITY (user_id-trust gap fix): mint a server-verified session token so
    // PUT /preferences and GET /telegram/link (both gated behind {#if activeUser})
    // can prove possession rather than just claiming a user_id. Best-effort: a
    // login failure degrades honestly (those two features show a clear error
    // when used) rather than blocking the rest of the app.
    let sessionToken: string | undefined;
    try {
      const loginResp = await loginSession(u.user_id);
      if (mySeq !== sessionSeq) return;           // a newer login/guest/logout won — drop this stale login
      sessionToken = loginResp.session_token;
    } catch {
      if (mySeq !== sessionSeq) return;
      sessionToken = undefined;
    }
    saveSession({ user: u, guest: true, activePlan: restored ? priorPlan : null, session_token: sessionToken });
    // A2: visible confirmation that prefs + currency applied (no extra step needed)
    const currencyNote = u.home_currency !== 'USD'
      ? ` · prices shown in USD with an indicative ${u.home_currency} estimate`
      : '';
    messages = [...messages, {
      role: 'note',
      text: `Logged in as ${u.display_name}. Prefs applied${currencyNote}.`
        + (restored ? ' Welcome back — restored your held trip.' : ''),
    }];
    view = 'app';
  }
  function continueGuest(): void {
    ++sessionSeq;                                 // invalidate any in-flight pickUser()/openPrefs()
    priorSessionOnLogout = null;
    activeUser = null; saveSession({ user: null, guest: true }); view = 'app';
  }
  function switchUser(): void {
    priorSessionOnLogout = loadSession();
    clearSession(); activeUser = null; result = null; sessionError = null;
    activePlan = null;                          // cleared here; pickUser() restores it if the SAME account logs back in
    streamRun?.close(); streamRun = null; progress = null; ++reqSeq; ++sessionSeq;  // invalidate in-flight trip AND session/prefs ops
    loading = false; confirming = false; cancelling = false;  // #117: clear in-flight flags (plan()'s finally is seq-guarded, won't self-heal after reqSeq bump)
    chatCollapsed = false; showPreview = false;
    walletUsd = 5000;                           // #117: reset wallet to guest default
    messages = [{ role: 'bot', text: `Describe your trip and I'll plan it — multi-city, budget-aware, safety-checked.` }];
    listDemoUsers().then((r) => (demoUsers = r.demo_users ?? [])).catch((e) => (sessionError = String(e)));
    view = 'picker';
  }
  async function openPrefs(): Promise<void> {
    if (!activeUser) return;
    const mySeq = ++sessionSeq;                    // race guard: invalidated by a logout/relogin while this fetch is in flight
    let fetched: Prefs;
    try { fetched = await getPreferences(activeUser.user_id, loadSession()?.session_token); }
    catch { fetched = { ...activeUser }; }
    if (mySeq !== sessionSeq) return;               // a newer session op won — drop this stale prefs open
    prefsProfile = fetched;
    view = 'prefs';
  }
  function onPrefsSaved(p: Prefs): void {       // saved prefs auto-apply to the next plan
    activeUser = { ...(activeUser as DemoUser), home_currency: p.home_currency,
                   nationality: p.nationality, persona: p.persona };
    // SECURITY (user_id-trust gap fix): preserve the session_token here — this
    // saveSession() previously dropped it, which would silently log the user
    // out of PUT /preferences / GET /telegram/link (403→"session expired")
    // after the very first successful prefs save, without any real expiry.
    const sess = loadSession();
    saveSession({ user: activeUser, guest: true, activePlan: sess?.activePlan ?? null,
                  session_token: sess?.session_token }); view = 'app';
  }

  // ── Destination browser: region tabs + persona recommendations ──────────
  // GuideDest.photo is optional (not `as const`) because only some entries
  // have a sourced photo — PERSONA_RECS in particular mixes destinations that
  // duplicate a REGION_TABS entry (which does have a photo) with a handful
  // that don't (Nepal, New Zealand, Iceland, Australia, Maldives, Seychelles,
  // French Riviera, Chiang Mai, Medellín), which stay on the emoji fallback.
  type GuideDest = { icon: string; dest: string; sub: string; prompt: string; photo?: string };

  const REGION_TABS: { id: string; label: string; dests: GuideDest[] }[] = [
    { id: 'asia', label: '🌏 Asia', dests: [
      { icon: '🗼', dest: 'Japan',       sub: 'Tokyo · Kyoto · Osaka',       prompt: '10 days Japan, $2500', photo: japanThumb },
      { icon: '🏝️', dest: 'Bali',        sub: 'Indonesia · beaches · culture', prompt: '7 days Bali, $1200, solo', photo: baliThumb },
      { icon: '🛺', dest: 'Thailand',    sub: 'Bangkok · Chiang Mai · islands', prompt: '10 days Thailand, $1500', photo: thailandThumb },
      { icon: '🌿', dest: 'Vietnam',     sub: 'Hanoi → Hoi An → Ho Chi Minh', prompt: '12 days Vietnam north to south, $1500', photo: vietnamThumb },
      { icon: '🇸🇬', dest: 'Singapore',  sub: 'City-state · food · gardens',  prompt: '5 days Singapore, $2000', photo: singaporeThumb },
      { icon: '🎋', dest: 'South Korea', sub: 'Seoul · Busan · Jeju Island',  prompt: '10 days South Korea, $2000', photo: southKoreaThumb },
    ]},
    { id: 'europe', label: '🌍 Europe', dests: [
      { icon: '🥐', dest: 'France',      sub: 'Paris · Loire Valley · Riviera', prompt: '10 days France, $2500', photo: franceThumb },
      { icon: '🥘', dest: 'Spain',       sub: 'Barcelona · Madrid · Seville', prompt: '10 days Spain, $2000', photo: spainThumb },
      { icon: '🍕', dest: 'Italy',       sub: 'Rome · Florence · Amalfi Coast', prompt: '10 days Italy, $2200', photo: italyThumb },
      { icon: '🏛️', dest: 'Greece',      sub: 'Athens · Santorini · Mykonos', prompt: '10 days Greece, $2000', photo: greeceThumb },
      { icon: '🍷', dest: 'Portugal',    sub: 'Lisbon · Porto · Algarve',     prompt: '7 days Portugal, $1500', photo: portugalThumb },
      { icon: '🌷', dest: 'Netherlands', sub: 'Amsterdam · tulip country',    prompt: '7 days Netherlands, $1800', photo: netherlandsThumb },
    ]},
    { id: 'americas', label: '🌎 Americas', dests: [
      { icon: '🗽', dest: 'USA',         sub: 'NYC · California · Nat. Parks', prompt: '14 days USA, $3500', photo: usaThumb },
      { icon: '🌮', dest: 'Mexico',      sub: 'Cancún · Mexico City · Oaxaca', prompt: '10 days Mexico, $1800', photo: mexicoThumb },
      { icon: '🦋', dest: 'Colombia',    sub: 'Cartagena · Medellín · Bogotá', prompt: '10 days Colombia, $1800', photo: colombiaThumb },
      { icon: '🦙', dest: 'Peru',        sub: 'Lima · Cusco · Machu Picchu',  prompt: '10 days Peru, $2000', photo: peruThumb },
      { icon: '🍁', dest: 'Canada',      sub: 'Vancouver · Banff · Toronto',  prompt: '14 days Canada, $3000', photo: canadaThumb },
      { icon: '🌿', dest: 'Costa Rica',  sub: 'Rainforest · beaches',         prompt: '10 days Costa Rica, $2500, adventure', photo: costaRicaThumb },
    ]},
    { id: 'africa-me', label: '🌍 Africa & Middle East', dests: [
      { icon: '🏙️', dest: 'Dubai',        sub: 'UAE · skyline · desert',      prompt: '5 days Dubai, $2500', photo: dubaiThumb },
      { icon: '🌍', dest: 'Morocco',      sub: 'Marrakech · Sahara · Fes',    prompt: '7 days Morocco, $1400', photo: moroccoThumb },
      { icon: '🏺', dest: 'Egypt',        sub: 'Cairo · Luxor · Red Sea',     prompt: '10 days Egypt, $2000', photo: egyptThumb },
      { icon: '🦁', dest: 'Kenya',        sub: 'Masai Mara · Nairobi safari', prompt: '10 days Kenya safari, $4000', photo: kenyaThumb },
      { icon: '🌅', dest: 'Jordan',       sub: 'Petra · Wadi Rum · Dead Sea', prompt: '7 days Jordan, $2000', photo: jordanThumb },
      { icon: '🌊', dest: 'South Africa', sub: 'Cape Town · Garden Route',    prompt: '12 days South Africa, $3000', photo: southAfricaThumb },
    ]},
  ];

  const PERSONA_RECS: Record<string, GuideDest[]> = {
    foodie:        [
      { icon: '🗼', dest: 'Japan',    sub: 'Ramen · sushi · izakayas',      prompt: '10 days Japan food tour, $2500, foodie', photo: japanThumb },
      { icon: '🥐', dest: 'France',   sub: 'Paris bistros · Lyon bouchons', prompt: '7 days France culinary tour, $2500', photo: franceThumb },
      { icon: '🍜', dest: 'Thailand', sub: 'Bangkok street food · markets', prompt: '10 days Thailand street food, $1500, foodie', photo: thailandThumb },
      { icon: '🍕', dest: 'Italy',    sub: 'Naples pizza · Tuscan wine',    prompt: '10 days Italy food and wine, $2200, foodie', photo: italyThumb },
    ],
    hiker:         [
      { icon: '🏔️', dest: 'Nepal',       sub: 'Everest Base Camp trek',      prompt: '14 days Nepal trekking, $2000, adventure' },
      { icon: '🦙', dest: 'Peru',         sub: 'Inca Trail · Machu Picchu',   prompt: '10 days Peru Inca Trail, $2500, adventure', photo: peruThumb },
      { icon: '🌋', dest: 'New Zealand',  sub: 'South Island · Fiordland',    prompt: '14 days New Zealand hiking, $3000, adventure' },
      { icon: '🌊', dest: 'Iceland',      sub: 'Ring Road · glaciers · lava', prompt: '10 days Iceland adventure, $3000' },
    ],
    family:        [
      { icon: '🗼', dest: 'Japan',     sub: 'Disneyland · teamLab · Nara deer', prompt: '10 days Japan family, $4000, family', photo: japanThumb },
      { icon: '🏝️', dest: 'Bali',      sub: 'Ubud · Seminyak · temples',      prompt: '7 days Bali family, $2000, family', photo: baliThumb },
      { icon: '🦘', dest: 'Australia', sub: 'Sydney · Uluru · reef',           prompt: '14 days Australia family, $5000, family' },
      { icon: '🥘', dest: 'Spain',     sub: 'Barcelona beaches · Sagrada',     prompt: '10 days Spain family, $3000, family', photo: spainThumb },
    ],
    luxury:        [
      { icon: '🏖️', dest: 'Maldives',       sub: 'Overwater villas · reefs',   prompt: '7 days Maldives luxury, $5000' },
      { icon: '🌴', dest: 'Seychelles',      sub: 'Private islands · pristine', prompt: '10 days Seychelles luxury, $6000' },
      { icon: '🥂', dest: 'French Riviera',  sub: 'Cannes · Monaco · Nice',     prompt: '10 days French Riviera luxury, $5000' },
      { icon: '🏙️', dest: 'Dubai',           sub: 'Burj Khalifa · desert camp', prompt: '7 days Dubai luxury, $4000', photo: dubaiThumb },
    ],
    budget:        [
      { icon: '🌿', dest: 'Vietnam',  sub: 'Street food · $25/day hostels',  prompt: '14 days Vietnam budget, $1200', photo: vietnamThumb },
      { icon: '🛺', dest: 'Thailand', sub: 'Islands · full-moon party',      prompt: '14 days Thailand backpacker, $1200, budget', photo: thailandThumb },
      { icon: '🏝️', dest: 'Bali',     sub: 'Cheap cafes · surf · yoga',     prompt: '10 days Bali budget, $900', photo: baliThumb },
      { icon: '🌍', dest: 'Morocco',  sub: 'Medinas · Sahara · riads',       prompt: '7 days Morocco budget, $900', photo: moroccoThumb },
    ],
    digital_nomad: [
      { icon: '🏝️', dest: 'Bali',       sub: 'Canggu coworking · fast wifi', prompt: '30 days Bali digital nomad, $2500, remote work', photo: baliThumb },
      { icon: '🛺', dest: 'Chiang Mai', sub: 'Cheap coliving · temples',     prompt: '30 days Chiang Mai digital nomad, $2000, remote work' },
      { icon: '🍷', dest: 'Portugal',   sub: 'Lisbon nomad visa · beaches',  prompt: '30 days Portugal digital nomad, $2500, remote work', photo: portugalThumb },
      { icon: '🦋', dest: 'Medellín',   sub: 'Colombia · spring city · buzz', prompt: '30 days Medellin Colombia nomad, $2000, remote work' },
    ],
  };

  function defaultRegion(nat: string | undefined): string {
    if (!nat) return 'asia';
    const seaEa   = ['SG','MY','ID','TH','PH','VN','KH','MM','JP','KR','CN','TW','HK'];
    const eu      = ['GB','DE','FR','IT','ES','NL','BE','PL','SE','NO','DK','FI','CH','AT','PT','GR','RU'];
    const am      = ['US','CA','BR','MX','AR','CO','CL','PE'];
    const africaMe = ['EG','ZA','NG','KE','MA','DZ','TZ','GH','ET','AO','CI','CM','MZ','ZM','ZW',
                      'AE','SA','QA','KW','BH','OM','JO','IQ','IR','IL','LB','SY','YE'];
    if (seaEa.includes(nat))    return 'asia';
    if (eu.includes(nat))       return 'europe';
    if (am.includes(nat))       return 'americas';
    if (africaMe.includes(nat)) return 'africa-me';
    return 'asia';
  }

  let activeRegion = 'asia';
  $: activeRegion = defaultRegion(activeUser?.nationality);

  $: state = uiState(result) as UiState;
  $: plans = result?.day_plans ?? [];
  $: tripHasDates = (result?.legs ?? []).some((l) => !!l.checkin) || (result?.day_plans ?? []).some((d) => !!d.checkin);
  /** True while a plan is active — ChatPane switches to floating bot-emoji mode. */
  $: hasPlan = result !== null && (state === 'plan_ready' || state === 'success');
  // Honest-assumption disclosures (#188) — server-authored notes for anything the
  // parser silently guessed (date/year/party-size/currency/children-not-priced).
  // Self-corrects: `result` is swapped wholesale on /refine + /replan, and the
  // backend only re-attaches notes for fields still unresolved.
  $: planAssumptions = assumptionNotes(result);
  // Companion to planAssumptions: the CONCRETE date range those assumptions produced
  // (e.g. "Oct 1 – Oct 8, 2026 (7 nights)"), rendered next to the assumption-notes banner
  // so the reader doesn't have to parse a sentence to learn the assumed date. null when
  // no leg/day_plan carries dates — mirrors the tripHasDates guard, no fabrication.
  $: planDateRange = tripDateRange(result);

  const MY_TRIPS_RE = /\b(my|previous|past|last)\s+(trips?|itinerar(?:y|ies)|bookings?)\b|\bwhat did i book\b|\bretrieve my (?:last |previous )?(?:trip|itinerary|booking)\b/i;

  async function send(text: string): Promise<void> {
    if (activeUser && MY_TRIPS_RE.test(text)) {
      await showMyTrips();
      return;
    }
    if (activePlan?.idempotency_key && result && uiState(result) === 'plan_ready') {
      await refineCurrentPlan(text);
    } else {
      await plan(text);
    }
  }

  /** "show my trips" / "what did I book" — list past trips for the logged-in demo user
   *  (GET /trips?user_id=) and show the most recent one's full itinerary. View-only: this
   *  does NOT establish a /refine-able activePlan, since list_trips rows carry no
   *  session_id — it's an honest "look back," not a resumed conversation. */
  async function showMyTrips(): Promise<void> {
    if (!activeUser) return;
    const mySeq = ++reqSeq;                        // race guard: shares reqSeq with plan()/refineCurrentPlan() — a
                                                     // logout, new plan, or new refine while this is in flight wins
    messages = [...messages, { role: 'user', text: 'Show my trips' }];
    const trips = await listMyTrips(activeUser.user_id);
    if (mySeq !== reqSeq) return;                   // a newer trip op (or logout) won — drop this stale lookup
    if (!trips.length) {
      messages = [...messages, { role: 'note', text: `No previous trips found for ${activeUser.display_name}.` }];
      return;
    }
    const lines = trips.slice(0, 5).map((t) => {
      const when = (t.confirmed_at ?? t.created_at ?? '').slice(0, 10);
      const label = t.booking_ref ? `Booked ✓ ${t.booking_ref}` : (t.status ?? t.outcome ?? 'held');
      const total = t.package_total_cents != null ? centsToUsd(t.package_total_cents) : '';
      return `• ${label}${total ? ' — ' + total : ''}${when ? ' (' + when + ')' : ''}`;
    });
    const latest = trips[0];
    let viewedNote = '';
    if (latest?.idempotency_key) {
      const env = await getTrip(latest.idempotency_key);
      if (mySeq !== reqSeq) return;                 // a newer trip op (or logout) won — drop this stale restore
      // View-only: clear any live activePlan so a stray follow-up message doesn't
      // silently refine a plan the user is no longer looking at (this trip has no
      // session_id, so it was never made refineable in the first place).
      if (env) { result = env; activePlan = null; viewedNote = ' Showing your most recent trip above.'; }
    }
    messages = [...messages, {
      role: 'note',
      text: `Found ${trips.length} previous trip${trips.length === 1 ? '' : 's'}:\n${lines.join('\n')}${viewedNote}`,
    }];
  }

  async function plan(text: string): Promise<void> {
    const mySeq = ++reqSeq;                       // #1 generation counter
    loading = true;
    progress = initProgress();                    // hero board appears immediately

    // When following up after a failed result (budget shortfall, cannot_satisfy, etc.),
    // the user's message often omits the destination ("raise to 3k" after "10 days Japan").
    // Prepend the prior user message as context so the backend can still resolve it.
    // Only prepend for failure/clarification states — never for success or plan_ready,
    // where the floating chat accepts new independent requests.
    const PREPEND_STATES = new Set(['budget_shortfall','declined','needs_clarification','invalid','insufficient_funds','reconcile']);
    let effectiveText = text;
    if (PREPEND_STATES.has(uiState(result))) {
      const priorUserMsg = messages.filter((m) => m.role === 'user').at(-1);
      if (priorUserMsg && priorUserMsg.text !== text) {
        effectiveText = `${priorUserMsg.text} — ${text}`;
      }
    }

    messages = [...messages, { role: 'user', text }];
    const body = {
      text: effectiveText, plan: true,
      wallet_balance_cents: Math.round(walletUsd * 100),
      live_emergency: { check: true },
      narrate: true,  // initial plan only — Preview's "✨ assistant's summary" needs this;
                       // refineCurrentPlan/"make it cheaper" deliberately does NOT set it
                       // (narration adds ~14-30s on top of an already ~11s call).
      ...(activeUser ? { user_id: activeUser.user_id, nationality: activeUser.nationality } : {}),
    };
    streamRun?.close();
    const run = planStreaming(body, (e) => {
      if (mySeq === reqSeq) progress = reduceProgress(progress ?? initProgress(), e);
    });
    streamRun = run;
    try {
      const r = await run.done;                   // resolves on negotiate_finished OR degrade
      if (mySeq !== reqSeq) return;               // #1 a newer submit won — drop this result
      result = r;
      const s = uiState(r);
      if (s === 'plan_ready' || s === 'success') {
        const narrativeText = r.itinerary_narrative?.overview ?? null;
        const planFees = feeAmount(r);
        messages = [...messages, { role: 'bot', text: narrativeText
          ?? (s === 'plan_ready'
            ? `Planned ${plansLabel(r)} — ${centsToUsd(heldAmount(r))} held${planFees > 0 ? ` (${centsToUsd(chargeAmount(r))} lodging + ${centsToUsd(planFees)} est. fees, not charged)` : ''}. Review below, then Confirm & Book.`
            : `Planned ${plansLabel(r)} — ${centsToUsd(chargeAmount(r))} charged${planFees > 0 ? ` (+ ${centsToUsd(planFees)} est. third-party fees, not charged by us)` : ''}${r.booking_ref ? ` · booked ${r.booking_ref}` : ''}.`) }];
        // Honesty caveat: a structural /replan edit can leave this narrative describing
        // stops that were since removed/added/reordered — surface the server's own
        // explanation as a 'note' bubble (same pattern as stateNote/kept_previous below),
        // never inventing our own wording.
        if (narrativeText && r.itinerary_narrative?.stale) {
          messages = [...messages, { role: 'note',
            text: r.itinerary_narrative.stale_reason ?? 'This summary may be outdated after your latest edit.' }];
        }
        // Honest-assumption disclosures (#188) — mirror the banner into the chat
        // thread so the guessed date/party/currency/children caveat is visible
        // even if the user never scrolls to the Confirm & Book banner.
        const assum = assumptionNotes(r);
        if (assum.length) {
          messages = [...messages, { role: 'note', text: assum.join(' ') }];
        }
        chatCollapsed = true;
        if (r.idempotency_key && s === 'plan_ready') {
          activePlan = { session_id: '', idempotency_key: r.idempotency_key };
          const sess = loadSession(); if (sess) saveSession(setActivePlan(sess, activePlan));
        }
      } else {
        messages = [...messages, { role: 'note', text: stateNote(r) }];
      }
    } catch (e) {
      if (mySeq !== reqSeq) return;
      result = null;
      messages = [...messages, { role: 'note', text: `Couldn't reach the planner: ${String(e)}` }];
    } finally {
      if (mySeq === reqSeq) { loading = false; progress = null; streamRun = null; }
    }
  }

  /** B6: conversational refinement of the active held plan. */
  async function refineCurrentPlan(text: string): Promise<void> {
    if (!activePlan?.idempotency_key) return;
    const mySeq = ++reqSeq;                        // race guard: shares reqSeq with plan() — a logout, "New trip",
                                                     // or a fresh plan() submit while this is in flight wins
    loading = true;
    messages = [...messages, { role: 'user', text }];
    try {
      const r = await refineTrip({
        idempotency_key: activePlan.idempotency_key,
        message: text,
        session_id: activePlan.session_id || undefined,
        wallet_balance_cents: Math.round(walletUsd * 100),
        ...(activeUser ? { user_id: activeUser.user_id } : {}),
      });
      if (mySeq !== reqSeq) return;                 // a newer submit/logout/new-trip won — drop this stale reply
      if (isForbidden(r)) {
        // IDOR fix: session_token/owner_token don't match the trip's actual owner —
        // honest refusal, held plan left untouched. #203: expired-session vs
        // cross-user-ownership get distinct copy.
        messages = [...messages, { role: 'note', text: forbiddenMessage(r) }];
        return;
      }
      const reply = r.assistant_reply ?? 'Your plan has been updated.';
      if (r.outcome === 'plan_ready' && r.plan) {
        // Success: new plan replaces old; new idempotency_key must be used for /confirm.
        result = r.plan;
        activePlan = {
          session_id: r.session_id ?? activePlan.session_id ?? '',
          idempotency_key: r.idempotency_key ?? activePlan.idempotency_key,
        };
        const sess = loadSession();
        if (sess) saveSession(setActivePlan(sess, activePlan));
        messages = [...messages, { role: 'bot', text: reply }];
      } else if (r.kept_previous) {
        // Re-plan failed but old plan is still held — show the honest note.
        messages = [...messages, { role: 'note', text: reply }];
      } else {
        // Partial / unsupported / unknown — honest reply, plan unchanged.
        messages = [...messages, { role: 'bot', text: reply }];
      }
    } catch (e) {
      if (mySeq !== reqSeq) return;
      messages = [...messages, { role: 'note', text: `Couldn't refine the plan: ${String(e)}` }];
    } finally {
      if (mySeq === reqSeq) loading = false;
    }
  }

  /** UI increment #1: date-range picker handler — reuses the /refine flow (no client math). */
  async function onDatesChosen({ start, end }: { start: string; end: string }): Promise<void> {
    await refineCurrentPlan(`Set the trip dates to ${start} through ${end}.`);
  }

  /** "New trip" — clear active plan and reset chat. */
  function newTrip(): void {
    activePlan = null;
    result = null;
    streamRun?.close(); streamRun = null; progress = null; ++reqSeq;
    messages = [{ role: 'bot', text: `Describe your trip and I'll plan it — multi-city, budget-aware, safety-checked.` }];
    const sess = loadSession();
    if (sess) saveSession(clearActivePlan(sess));
  }

  // The held amount surfaced on a plan_ready trip (wallet HOLD, not a charge).
  // This is the TRIP TOTAL — lodging (actually charged on booking) PLUS any
  // estimated third-party fees (insurance/visa/etc.) that are display-only and
  // never charged by us (backend orchestrator.py _inject_fees: "Lodging stays
  // the merchant's authoritative line"). Never show this number unqualified —
  // pair it with chargeAmount()/feeAmount() below wherever it's rendered.
  function heldAmount(r: NegotiateResult | null): number | undefined {
    return r?.wallet?.held_cents ?? r?.package_total_with_fees_cents ?? r?.package_total_cents;
  }

  // The amount ACTUALLY charged to the wallet — lodging only. Post-booking this
  // is the real merchant debit (wallet.debit_cents); pre-booking it's the same
  // figure the /confirm call will debit (package_total_cents). Falls back to
  // heldAmount() only for old envelopes that predate the lodging/fee split.
  function chargeAmount(r: NegotiateResult | null): number | undefined {
    return r?.wallet?.debit_cents ?? r?.package_total_cents ?? heldAmount(r);
  }

  // Estimated third-party fees (insurance/visa/vaccine) folded into heldAmount()
  // for display but never charged to the wallet by Travel Guild.
  function feeAmount(r: NegotiateResult | null): number {
    return r?.fee_total_cents ?? 0;
  }

  // THE ONE HUMAN CONSENT: commit the held plan → booked. Idempotent (retry safe).
  async function confirmBook(): Promise<void> {
    if (!result?.idempotency_key || confirming) return;
    confirming = true;
    const key = result.idempotency_key;
    const mySeq = reqSeq;                  // race guard (capture only — `confirming` above already blocks
                                            // self-reentry; this catches a concurrent logout/new-plan instead)
    try {
      const b = await confirmPlan(key, activeUser?.user_id);
      if (mySeq !== reqSeq) return;        // a logout/new-plan superseded this confirm — drop the stale result
      if (isForbidden(b)) {
        // IDOR fix: session_token/owner_token don't match the trip's actual owner —
        // honest refusal, held plan left untouched (NOT overwritten/discarded).
        // #203: expired-session vs cross-user-ownership get distinct copy.
        messages = [...messages, { role: 'note', text: forbiddenMessage(b) }];
        return;
      }
      const s = uiState(b);
      if (s === 'success') {
        result = b;                        // booked: real booking_ref + wallet debited
        if (b.wallet?.balance_cents != null) walletUsd = b.wallet.balance_cents / 100;
        const bookedFees = feeAmount(b);
        messages = [...messages, { role: 'bot',
          text: `Booked ✓ ${b.booking_ref} — ${centsToUsd(chargeAmount(b))} charged (SIMULATED prepaid).`
            + (bookedFees > 0 ? ` ${centsToUsd(bookedFees)} in estimated third-party fees (insurance/visa) are not charged by Travel Guild — see the Budget tab.` : '') }];
      } else if (s === 'reconcile' || s === 'server_error') {
        // commit ambiguous / errored: keep the held plan visible — NOT charged; the SAME key
        // is safe to retry (never double-books). Don't discard the plan_ready + idempotency_key.
        messages = [...messages, { role: 'note',
          text: `We couldn't confirm that — you weren't charged. Tap Confirm again (it's safe; it won't double-book).` }];
        chatCollapsed = false;   // surface the retry note — user MUST see it
      } else {
        result = b;                        // insufficient_funds / plan_expired — terminal
        messages = [...messages, { role: 'note', text: stateNote(b) }];
      }
    } catch (e) {
      if (mySeq === reqSeq) {
        messages = [...messages, { role: 'note', text: `Couldn't confirm — you weren't charged; tap Confirm again. (${String(e)})` }];
      }
    } finally {
      confirming = false;
    }
  }

  function plansLabel(r: NegotiateResult | null): string {
    const cities = (r?.day_plans ?? []).map((d) => d.city).filter(Boolean);
    return cities.length ? cities.join(' → ') : 'your trip';
  }

  /** Typed wrapper for Svelte's on:replanned event (avoids inline type annotation in template). */
  function handleReplanned(e: CustomEvent<NegotiateResult>): void { onReplanned(e.detail); }

  /** Handle a successful /replan: swap the envelope (server is truth), rebuild ICS if booked. */
  function onReplanned(env: NegotiateResult): void {
    // The server envelope is the truth. Key is STABLE across /replan (unlike /refine).
    result = { ...env, idempotency_key: env.idempotency_key ?? result?.idempotency_key };
    // Persist handle (key unchanged) so edits survive reload.
    const sess = loadSession();
    if (sess && activePlan) saveSession(setActivePlan(sess, activePlan));
    // In-trip edit on a BOOKED trip → refresh the calendar file.
    if (isBookedEnvelope(result)) downloadIcs(result);
  }

  // ── visual severity for the non-success/error state-card (design-consistency fix) ──
  // Mirrors the established SafetyWatch/Aftercare "here's a problem" language:
  //   danger    = a hard content/money decision (declined, over budget, can't afford)
  //   attention = a retry-able technical hiccup (parse/server/expiry) — same amber
  //               tokens as SafetyWatch's "unavailable" tone / Aftercare's beta-banner
  //   info      = benign, no alarm warranted (we just need more detail from you)
  type StateTier = 'danger' | 'attention' | 'info';
  const DANGER_STATES = new Set<UiState>(['declined', 'insufficient_funds', 'budget_shortfall']);
  const ATTENTION_STATES = new Set<UiState>(['invalid', 'server_error', 'plan_expired', 'reconcile']);
  function stateTier(s: UiState): StateTier {
    if (DANGER_STATES.has(s)) return 'danger';
    if (ATTENTION_STATES.has(s)) return 'attention';
    return 'info';                                  // needs_clarification (and any other fallback)
  }
  function stateIcon(s: UiState): string {
    switch (s) {
      case 'declined': return '🚫';
      case 'insufficient_funds': return '💰';
      case 'budget_shortfall': return '📉';
      case 'invalid': return '❓';
      case 'server_error': return '⚠️';
      case 'plan_expired': return '⏱️';
      case 'reconcile': return '⚠️';
      default: return '💬';                          // needs_clarification
    }
  }
  function stateChipLabel(s: UiState): string {
    switch (s) {
      case 'declined': return 'DECLINED';
      case 'insufficient_funds': return 'INSUFFICIENT FUNDS';
      case 'budget_shortfall': return 'BUDGET SHORTFALL';
      case 'invalid': return "COULDN'T PARSE";
      case 'server_error': return 'SERVER ERROR';
      case 'plan_expired': return 'PLAN EXPIRED';
      case 'reconcile': return 'RETRY NEEDED';
      default: return 'MORE INFO NEEDED';             // needs_clarification
    }
  }

  // honest, body-driven messages for the non-success states (both routes return HTTP 200)
  function stateNote(r: NegotiateResult | null): string {
    switch (uiState(r)) {
      case 'insufficient_funds':
        return `Trip exceeds your funded wallet (${centsToUsd(r?.total_cents ?? r?.closest_package_total_cents)} vs ${centsToUsd(r?.wallet_balance_cents)}). Top up or trim the trip.`;
      case 'reconcile':
        return `Couldn't confirm the booking just now — tap Confirm again. You were NOT charged twice (the request is idempotent).`;
      case 'plan_expired':
        return 'This held plan has expired — plan the trip again to continue.';
      case 'budget_shortfall':
        return `About ${centsToUsd(r?.budget_shortfall_cents)} short — raise the budget or shorten the trip (min feasible ${centsToUsd(r?.min_feasible_total_cents)}).`;
      case 'needs_clarification':
        return (r?.reason ?? '').replace(/^cannot_satisfy:\s*/i, '')
          || 'I need a bit more detail to plan this — where and roughly when?';
      case 'declined':
        return (r?.reason ?? '').replace(/^cannot_satisfy:\s*/i, '')
          || `I won't book this one — it falls in a do-not-recommend / advisory zone.`;
      case 'invalid':
        return `That request didn't parse into a bookable trip — try naming a destination, dates, and budget.`;
      case 'server_error':
        return 'The planner hit an internal error. Please try again.';
      default:
        return r?.reason || 'No itinerary could be produced.';
    }
  }
</script>

<div class="top">
  <div class="brand">Travel <span>Guild</span></div>
  {#if state === 'success' || state === 'plan_ready'}
    <div class="trip">{plansLabel(result)}</div>
  {/if}
  <SafetyWatch data={safety} open={safetyOpen} on:toggle={() => (safetyOpen = !safetyOpen)} />
  <div class="spacer"></div>
  {#if view === 'app' || view === 'prefs'}
    <!-- landing-page-draft2: grouped account-cluster pill (was 3 separate loose header
         elements — wallet input, user/persona/currency pill, log in/out button). Pure
         visual regrouping: all bindings/handlers/testids preserved unchanged. -->
    <div class="account-cluster">
      <label class="wallet">$<input type="number" bind:value={walletUsd} min="0" step="100" /></label>
      <span class="divider"></span>
      {#if activeUser}
        <button class="pill-btn" data-testid="prefs-link" on:click={openPrefs}>👤 {activeUser.display_name} · {activeUser.user_id} · {activeUser.persona} · {activeUser.home_currency}</button>
      {:else}
        <span class="pill-btn guest">👤 Guest</span>
      {/if}
      <span class="divider"></span>
      <button class="pill-btn primary" data-testid="switch-user" on:click={switchUser}>{activeUser ? 'Log out' : 'Log in'}</button>
    </div>
  {/if}
</div>

{#if view === 'loading'}
  <div class="loading-screen">Loading travellers…</div>
{:else if view === 'picker'}
  <SessionPicker users={demoUsers} error={sessionError} on:select={(e) => pickUser(e.detail)} on:guest={continueGuest} />
{:else if view === 'prefs' && prefsProfile}
  <Preferences user={prefsProfile} on:saved={(e) => onPrefsSaved(e.detail)} on:close={() => (view = 'app')} />
{:else}
{#if (state === 'success' || state === 'plan_ready') && result}
  <div class="banner" data-testid="status-banner" class:held={state === 'plan_ready'}>
    {#if state === 'plan_ready'}
      <b>Plan ready — held, nothing booked yet.</b>
      {#if feeAmount(result) > 0}
        <span class="sub" data-testid="held-breakdown">{centsToUsd(heldAmount(result))} trip total — {centsToUsd(chargeAmount(result))} lodging held against your wallet (SIMULATED prepaid) + {centsToUsd(feeAmount(result))} estimated third-party fees (insurance/visa — paid directly to providers, never charged by us). Refine it by chatting, or confirm to book.</span>
      {:else}
        <span class="sub" data-testid="held-breakdown">{centsToUsd(heldAmount(result))} held against your wallet (SIMULATED prepaid). Refine it by chatting, or confirm to book.</span>
      {/if}
      {#if planAssumptions.length}
        <div class="assumption-notes" data-testid="assumption-notes">
          <b>⚠ I had to assume {planAssumptions.length === 1 ? 'one thing' : `${planAssumptions.length} things`}:</b>
          <ul>
            {#each planAssumptions as n}<li>{n}</li>{/each}
          </ul>
          {#if planDateRange}
            <div class="assumption-date-range" data-testid="assumption-date-range">Currently planned: {planDateRange.label}</div>
          {/if}
        </div>
      {/if}
      <button class="confirm" data-testid="confirm-book" on:click={confirmBook} disabled={confirming || !tripHasDates}>
        {confirming ? 'Booking…' : `✓ Confirm & Book ${centsToUsd(chargeAmount(result))} to wallet`}
      </button>
      {#if !tripHasDates}
        <div class="date-required-note" data-testid="date-required-note">📅 Set your trip dates above before booking.</div>
      {/if}
      <button class="newtrip" data-testid="new-trip" on:click={newTrip}>Start new trip</button>
    {:else if result.booking_ref}
      <b>Booked ✓</b> <span class="ref">{result.booking_ref}</span>
      <span class="sub" data-testid="charged-breakdown">· {centsToUsd(chargeAmount(result))} charged to your wallet (SIMULATED prepaid).{#if feeAmount(result) > 0}{' '}{centsToUsd(feeAmount(result))} in estimated third-party fees (insurance/visa) are not charged by Travel Guild — see the Budget tab.{/if}</span>
      <button class="summary" data-testid="view-summary" on:click={() => (showPreview = true)}>View trip summary &amp; save to phone →</button>
      <button class="newtrip" data-testid="cancel-booking" on:click={cancelBooking} disabled={cancelling}>{cancelling ? 'Cancelling…' : 'Cancel booking'}</button>
    {/if}
  </div>
{/if}

{#if state === 'empty' || (loading && !result)}
<!-- #118: Pre-plan 2-col split — stays visible during initial load so ChatPane never jumps to full-size sidebar -->
<div class="empty-layout">
  <div class="empty-chat-col">
    <ChatPane {messages} {loading} hasPlan={false} collapsed={false} showGrammarChips={true}
      bind:mobOpen={preplanMobOpen} on:plan={(e) => send(e.detail)} />
  </div>
  <div class="empty-guide-col">
    <div class="guide-panel" data-testid="guide-panel">
      {#if loading && progress}
        <!-- Replace cards with the live progress board while planning -->
        <div class="guide-heading" style="font-size:18px">Planning your trip…</div>
        <div class="guide-subheading">This usually takes 10–30 seconds</div>
        <LiveProgress {progress} />
      {:else}
      <button type="button" class="guide-assistant-header" on:click={() => (preplanMobOpen = true)}>
        <span class="guide-bot-bubble">🤖</span>
        <span class="guide-bot-label">Guild Assistant</span>
      </button>
      <div class="guide-heading">Where would you like to go?</div>
      <div class="guide-subheading">Multi-city · budget-aware · safety-checked</div>

      {#if activeUser && PERSONA_RECS[activeUser.persona]}
      <div class="guide-section-label">Recommended for {activeUser.persona.replace('_', ' ')}</div>
      <div class="guide-dest-list">
        {#each PERSONA_RECS[activeUser.persona] as d}
        <button class="guide-card" on:click={() => send(d.prompt)}>
          {#if d.photo}<img class="guide-photo" src={d.photo} alt="" loading="lazy" decoding="async" />{:else}<span class="guide-icon">{d.icon}</span>{/if}
          <div class="guide-dest-info">
            <div class="guide-title">{d.dest}</div>
            <div class="guide-sub">{d.sub}</div>
          </div>
          <span class="guide-arrow">→</span>
        </button>
        {/each}
      </div>
      <div class="guide-divider"></div>
      {/if}

      <div class="region-tabs" role="tablist">
        {#each REGION_TABS as tab}
        <button class="rtab" role="tab" aria-selected={activeRegion === tab.id}
          class:active={activeRegion === tab.id}
          on:click={() => activeRegion = tab.id}>{tab.label}</button>
        {/each}
      </div>
      <div class="guide-dest-list">
        {#each REGION_TABS.find(t => t.id === activeRegion)?.dests ?? [] as d}
        <button class="guide-card" on:click={() => send(d.prompt)}>
          {#if d.photo}<img class="guide-photo" src={d.photo} alt="" loading="lazy" decoding="async" />{:else}<span class="guide-icon">{d.icon}</span>{/if}
          <div class="guide-dest-info">
            <div class="guide-title">{d.dest}</div>
            <div class="guide-sub">{d.sub}</div>
          </div>
          <span class="guide-arrow">→</span>
        </button>
        {/each}
      </div>
      {/if}
    </div>
  </div>
</div>
{:else if hasPlan}
<!-- ── Plan active: ChatPane floats as 🤖 emoji, grid shows itinerary ──
     hideMobileBubble covers three fixed-position overlays that otherwise sit
     on top of / intercept clicks on the assistant trigger (bottom-left,
     z-index 300) on mobile: Preview's .budget-bar (showPreview), Map.svelte's
     mobile place-detail sheet (.pin-sheet, z-index 315, via placeSheetOpen —
     B5), and Itinerary.svelte's own item-thumb/leg-thumb photo lightbox
     (.lb-overlay, z-index 9999, via itineraryLightboxOpen — #168 B5 spillover
     item 4, verified live to intercept clicks on the trigger underneath). -->
<ChatPane {messages} {loading} {hasPlan} collapsed={chatCollapsed} hideMobileBubble={showPreview || $placeSheetOpen || $itineraryLightboxOpen}
  on:plan={(e) => send(e.detail)} on:toggle={(e) => (chatCollapsed = e.detail)} />

{#if showPreview && state === 'success' && result}
  <Preview {result} on:close={() => (showPreview = false)} />
{:else}
<div class="grid">
  <main class="center" data-testid="center">
    {#if loading && progress}
      <LiveProgress {progress} />
    {:else}
      {#if !tripHasDates}
        <DateRangePicker start="" end="" on:confirm={(e) => onDatesChosen(e.detail)} />
      {/if}
      <Itinerary {plans} legs={result?.legs ?? []}
        idempotencyKey={result?.idempotency_key ?? ''}
        editable={state === 'plan_ready' || state === 'success'}
        transport_edges={result?.transport_edges ?? []}
        transport_pricing={result?.transport_pricing ?? null}
        on:replanned={handleReplanned}
        on:suggest={(e) => send(e.detail)} />
    {/if}
  </main>
  {#if result}<RightRail {result} user_id={activeUser?.user_id} on:replanned={handleReplanned} />{:else}<div></div>{/if}
</div>
{/if}

{:else}
<!-- ── No plan (needs_clarification / error / declined): keep 2-col layout ──
     Without this, ChatPane renders as a full-width 78vh aside and pushes the
     state message below it. Mirror the empty-layout structure so chat stays
     in a constrained left column and the state message lives on the right. -->
<div class="empty-layout">
  <div class="empty-chat-col">
    <ChatPane {messages} {loading} hasPlan={false} collapsed={false}
      on:plan={(e) => send(e.detail)} />
  </div>
  <div class="empty-guide-col">
    <div class="guide-panel">
      {#if loading && progress}
        <div class="guide-heading" style="font-size:18px">Planning your trip…</div>
        <div class="guide-subheading">This usually takes 10–30 seconds</div>
        <LiveProgress {progress} />
      {:else}
        <div class="placeholder state-{state} tier-{stateTier(state)}" data-testid="state-card">
          <div class="state-head">
            <span class="state-icon">{stateIcon(state)}</span>
            <span class="sev-chip tier-{stateTier(state)}">{stateChipLabel(state)}</span>
          </div>
          <div class="state-message">{stateNote(result)}</div>
        </div>
      {/if}
    </div>
  </div>
</div>
{/if}
{/if}

<style>
  :global(html, body) { margin: 0; }
  :global(body) { font-family: system-ui, -apple-system, sans-serif; background: #faf6f0; color: #2d2a26; }
  .top { display: flex; align-items: center; gap: 14px; max-width: 1340px; margin: 0 auto; padding: 16px 18px 8px; }
  .brand { font-weight: 700; font-size: 20px; }
  .brand span { color: #d9774a; }
  .trip { color: #7c7468; font-size: 14px; }
  .spacer { flex: 1; }
  /* landing-page-draft2 .account-cluster: one rounded-999px pill holding wallet input,
     user pill, and log in/out — replaces the 3 previously-separate .wallet/.demo elements. */
  .account-cluster { display: flex; align-items: center; border: 1px solid #ece2d5; background: #fff;
    border-radius: 999px; padding: 3px; }
  .account-cluster .wallet { font-size: 12px; color: #7c7468; display: flex; align-items: center;
    gap: 2px; padding: 4px 10px; }
  .account-cluster .wallet input { width: 56px; border: none; background: transparent; font-size: 12px;
    font-weight: 700; color: #2d2a26; padding: 0; }
  .account-cluster .wallet input:focus { outline: none; }
  .account-cluster .divider { width: 1px; height: 16px; background: #ece2d5; flex-shrink: 0; }
  .account-cluster .pill-btn { border: none; background: transparent; font-size: 12px; color: #7c7468;
    padding: 5px 12px; border-radius: 999px; cursor: pointer; font-family: inherit; white-space: nowrap; }
  .account-cluster .pill-btn.guest { cursor: default; }
  .account-cluster button.pill-btn:hover { color: #d9774a; }
  .account-cluster .pill-btn.primary { background: #d9774a; color: #fff; font-weight: 700; }
  .account-cluster .pill-btn.primary:hover { color: #fff; opacity: .92; }
  /* Header scaling fix: .top previously had zero responsive rules while the content below it
     (.empty-layout) already adapted at 900/768px — on narrow phones the brand/badge/wallet/login
     row would overflow un-wrapped. Priority as space shrinks: trip label drops first, then the
     spacer forces the account-cluster onto a wrapped second row (brand+badge stay on row 1). */
  @media (max-width: 760px) {
    .top { flex-wrap: wrap; row-gap: 8px; padding: 12px 14px 6px; column-gap: 10px; }
    .trip { display: none; }
    .account-cluster .wallet input { width: 64px; }
  }
  @media (max-width: 460px) {
    .spacer { flex-basis: 100%; height: 0; }
    /* Open note (landing-page-draft2 audit): confirm the account-pill doesn't overflow
       at narrow widths — the user pill's text ("👤 name · id · persona · currency") can
       run long once grouped into one non-wrapping pill, so it truncates with ellipsis
       instead of breaking the pill shape or overflowing the viewport. */
    .account-cluster { max-width: 100%; }
    .account-cluster .pill-btn { font-size: 11px; padding: 4px 8px; max-width: 130px;
      overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
    .account-cluster .wallet { font-size: 11.5px; padding: 4px 6px; }
    .account-cluster .wallet input { width: 46px; }
  }
  .loading-screen { max-width: 1340px; margin: 14vh auto; text-align: center; color: #998a78; font-size: 14px; }
  .banner { max-width: 1340px; margin: 6px auto 0; padding: 11px 16px; background: linear-gradient(90deg, #fff, #fffaf4);
    border: 1px solid #caa75b; border-left: 4px solid #d9774a; border-radius: 12px; font-size: 14px; }
  .banner .ref { color: #d9774a; font-weight: 700; }
  .banner .sub { color: #7c7468; display: block; margin-top: 2px; }
  .banner.held { border-left-color: #4f8a63; }
  .banner .confirm { margin-top: 9px; border: 0; border-radius: 9px; background: #d9774a; color: #fff;
    font-weight: 700; font-size: 14px; padding: 9px 16px; cursor: pointer; }
  .banner .confirm:disabled { opacity: .6; cursor: default; }
  .banner .summary { margin-top: 9px; margin-left: 10px; border: 1px solid #ece2d5; border-radius: 9px;
    background: #fff; color: #2d2a26; font-weight: 600; font-size: 13px; padding: 8px 14px; cursor: pointer; }
  .banner .newtrip { margin-top: 9px; margin-left: 8px; border: 1px solid #ece2d5; border-radius: 9px;
    background: #f4ede3; color: #7c7468; font-size: 12px; padding: 7px 12px; cursor: pointer; font-family: inherit; }
  .date-required-note { margin-top: 6px; font-size: 12px; color: #c98a2b; font-weight: 600; }
  .assumption-notes { margin-top: 8px; font-size: 12px; color: #c98a2b; background: #fdf6e9;
    border: 1px solid #e8d6a0; border-radius: 8px; padding: 8px 12px; }
  .assumption-notes ul { margin: 4px 0 0 16px; padding: 0; }
  .assumption-notes li { margin-top: 2px; }
  .assumption-date-range { margin-top: 6px; font-weight: 600; }
  .grid { display: grid; grid-template-columns: 1fr 360px; gap: 16px;
    max-width: 1340px; margin: 14px auto; padding: 0 18px 28px; align-items: start; }
  .center { background: #fff; border: 1px solid #ece2d5; border-radius: 14px; padding: 12px 14px;
    min-height: 70vh; min-width: 0; overflow-x: hidden; }
  /* State-card: mirrors SafetyWatch/Aftercare's icon + severity-chip + colored-card
     "here's a problem" language rather than a flat line of text. Three tiers:
     danger (red) = a hard content/money decision, attention (amber) = a retry-able
     hiccup, info (neutral) = benign — we just need more detail. Same red/amber tokens
     as Aftercare's alert-card/sev-chip and the shared amber "unavailable"/beta tone. */
  .placeholder { padding: 16px 18px; font-size: 14px; border-radius: 12px; border: 1px solid #ece2d5; background: #faf6f0; }
  .placeholder.tier-danger { background: #fef2f0; border-color: #f0c8c0; }
  .placeholder.tier-attention { background: #fdf6e9; border-color: #e8d6a0; }
  .state-head { display: flex; align-items: center; gap: 8px; margin-bottom: 8px; }
  .state-icon { font-size: 17px; line-height: 1; }
  .sev-chip { font-size: 11px; font-weight: 700; border-radius: 6px; padding: 2px 8px; letter-spacing: .3px; }
  .sev-chip.tier-info { background: #f4ede3; color: #7c7468; }
  .sev-chip.tier-danger { background: #f8e4de; color: #c0563f; }
  .sev-chip.tier-attention { background: #e8d6a0; color: #7a6a30; }
  .state-message { color: #2d2a26; line-height: 1.5; }
  .placeholder.tier-info .state-message { color: #7c7468; }
  @media (max-width: 1100px) { .grid { grid-template-columns: 1fr; } }
  /* #118/scaling-fix: Pre-plan 2-col split — 2:3 fluid ratio (guide holds tabs + up to 6 cards,
     legitimately wider), matches the 1340px page-width family via a reading-optimized 1040px cap
     (this is a landing/decision screen, not the itinerary data-grid — full 1340px would make chat
     bubbles/cards absurdly wide). */
  .empty-layout { display: flex; gap: 20px; max-width: 1040px; margin: 14px auto; padding: 0 18px 28px; align-items: start; justify-content: center; }
  .empty-chat-col { flex: 2 1 0; min-width: 300px; max-width: 420px; }
  .empty-guide-col { flex: 3 1 0; min-width: 360px; max-width: 560px; }
  .guide-panel { background: #fff; border: 1px solid #ece2d5; border-radius: 14px; padding: 22px 20px 20px;
    max-height: calc(100vh - 90px); overflow-y: auto; }
  .guide-heading { font-size: 22px; font-weight: 700; color: #2d2a26; margin: 0 0 4px; letter-spacing: -.3px; line-height: 1.2; }
  .guide-subheading { font-size: 13px; color: #998a78; margin: 0 0 14px; }
  .guide-section-label { font-size: 11px; font-weight: 700; text-transform: uppercase; letter-spacing: .6px;
    color: #d9774a; margin: 0 0 8px; }
  .guide-dest-list { display: flex; flex-direction: column; gap: 6px; }
  .guide-card { display: flex; align-items: center; gap: 10px; padding: 9px 12px;
    background: #faf6f0; border: 1px solid #ece2d5; border-radius: 10px;
    cursor: pointer; text-align: left; font-family: inherit; transition: border-color .15s, background .15s;
    width: 100%; }
  .guide-card:hover { border-color: #d9774a; background: #fff7f1; }
  .guide-icon { font-size: 22px; flex-shrink: 0; width: 44px; height: 44px;
    display: flex; align-items: center; justify-content: center; }
  .guide-photo { width: 44px; height: 44px; border-radius: 8px; object-fit: cover; flex-shrink: 0; }
  .guide-dest-info { flex: 1; min-width: 0; }
  .guide-title { font-size: 13px; font-weight: 600; color: #2d2a26; }
  .guide-sub { font-size: 11.5px; color: #998a78; margin-top: 1px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .guide-arrow { font-size: 14px; color: #ccc; flex-shrink: 0; }
  .guide-divider { height: 1px; background: #ece2d5; margin: 12px 0; }
  .region-tabs { display: flex; gap: 4px; margin-bottom: 10px; flex-wrap: wrap; }
  .rtab { background: #f7f0e8; border: 1px solid #ece2d5; border-radius: 18px; padding: 4px 10px;
    font-size: 11.5px; font-weight: 600; color: #5a4a3a; cursor: pointer; font-family: inherit; transition: background .12s, border-color .12s; }
  .rtab.active, .rtab:hover { background: #d9774a; color: #fff; border-color: #d9774a; }
  /* Assistant header — hidden on desktop (chat sidebar visible), shown on mobile.
     Now a <button> (was a plain div): on mobile this is the visually prominent
     "assistant" affordance, but it wasn't wired to anything — a first-time user
     tapping it got no response and never found the actual entry point (the small
     floating mob-bubble in the corner). Tapping it now opens the same sheet. */
  .guide-assistant-header { display: none; align-items: center; gap: 10px; margin-bottom: 14px;
    border: none; background: none; padding: 0; margin-left: 0; margin-right: 0; text-align: left;
    cursor: pointer; font-family: inherit; -webkit-tap-highlight-color: transparent; }
  .guide-bot-bubble { width: 38px; height: 38px; border-radius: 50%; background: #d9774a;
    display: flex; align-items: center; justify-content: center; font-size: 20px;
    box-shadow: 0 2px 8px rgba(217,119,74,.35); flex-shrink: 0; }
  .guide-bot-label { font-size: 14px; font-weight: 600; color: #d9774a; letter-spacing: -.2px; }
  @media (max-width: 900px) {
    .empty-layout { flex-direction: column-reverse; max-width: 100%; }
    /* flex-direction flips the main axis to vertical here, so the 2:3 flex-grow ratio from the
       desktop rule would otherwise distribute HEIGHT instead of width — neutralize it. */
    .empty-chat-col, .empty-guide-col { flex: none; width: 100%; min-width: 0; max-width: none; }
    .guide-heading { font-size: 20px; }
    .empty-chat-col :global(.chat) { height: 42vh; }
  }
  /* Mobile: guide fills viewport; chat becomes a floating mob-bubble (no bottom bar to clear).
     IMPORTANT: cannot use display:none on .empty-chat-col — it would suppress the position:fixed
     mob-wrap/mob-bubble inside ChatPane. Use zero-width + overflow:visible instead. */
  @media (max-width: 768px) {
    .empty-layout { flex-direction: column; padding: 0; margin: 0; }
    .empty-chat-col { flex: 0 0 0; width: 0; min-width: 0; overflow: visible; }
    .empty-guide-col { width: 100%; padding-bottom: 84px; overflow-x: hidden; }
    .guide-panel { border-radius: 0; border: none; padding: 16px 14px 14px; max-height: none; }
    .guide-assistant-header { display: flex; }
    .guide-card { min-width: 0; }
    .guide-title, .guide-sub { word-break: break-word; overflow-wrap: break-word; }
  }
</style>
