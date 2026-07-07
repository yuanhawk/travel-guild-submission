<script lang="ts">
  import { createEventDispatcher, onDestroy } from 'svelte';
  import type { DayPlan, Leg, Attraction, NegotiateResult, TransportEdge, TransportPricing, IntraCityGap, DayDetail, MealOption } from '../api';
  import type { ReplanOp } from '../api';
  import { replanTrip, livePriceOverlay, safeHref, centsToUsd, API_BASE, isForbidden, forbiddenMessage } from '../api';
  import { mealAnchoredTimeline, feeLabel, attractionRef, moveOps, displayName, cuisineDisplay, namePresentation } from '../itinerary';
  import type { TimelineItem } from '../itinerary';
  import { mapFocus, itineraryLightboxOpen } from '../mapStore';

  export let plans: DayPlan[] = [];
  export let legs: Leg[] = [];
  export let idempotencyKey: string = '';
  export let editable: boolean = false;
  // Inter-city transport (multi-leg). Pure-presentation: values come from the server
  // envelope — no client recompute. Optional: absent when plan is single-city.
  export let transport_edges: TransportEdge[] = [];
  export let transport_pricing: TransportPricing | null = null;

  // leg_id → leg, to surface per-leg honesty flags (unverified_lodging) on the header.
  $: legById = new Map(legs.filter((l) => l.leg_id).map((l) => [l.leg_id as string, l]));
  // from_leg_id → edge, for O(1) connector lookup at render time.
  $: edgeByFromLeg = new Map(
    (transport_edges ?? []).filter((e) => e.from_leg).map((e) => [e.from_leg as string, e]),
  );

  const slotIcon: Record<string, string> = {
    Breakfast: '☕', Lunch: '🍜', Tea: '🍵', Dinner: '🍽️', Supper: '🌙',
    Morning: '📍', Afternoon: '📍',
  };

  // ── inter-city transport helpers ──────────────────────────────────────────
  const modeIcon: Record<string, string> = {
    rail: '🚆', road: '🚌', bus: '🚌', flight: '✈️', ferry: '⛴️',
  };
  function iconForMode(mode: string): string {
    return modeIcon[mode?.toLowerCase?.()] ?? '🚌';
  }

  // ── intra-city inline gap chips (UI increment #3) ─────────────────────────
  // Pure presentation: render server values only; NO client distance/time math.
  const intraModeIcon: Record<string, string> = {
    walk: '🚶', metro: '🚇', bus: '🚌', taxi: '🚕',
  };
  function intraIconFor(mode: string): string {
    return intraModeIcon[mode?.toLowerCase?.()] ?? '🚶';
  }
  /** Format transfer_minutes as "~Xh Ym" / "~Xh" / "~Ym". */
  function fmtMinutes(m: number | undefined): string {
    if (m == null || m <= 0) return '';
    const h = Math.floor(m / 60);
    const min = m % 60;
    if (h === 0) return `~${min}m`;
    if (min === 0) return `~${h}h`;
    return `~${h}h ${min}m`;
  }

  // ── transient UI state (never mutates the rendered data) ──────────────────
  let busy: string | null = null;
  let flash: { kind: 'ok' | 'reject' | 'note'; msg: string } | null = null;

  const dispatch = createEventDispatcher<{
    replanned: NegotiateResult;
    locate: { label: string; lat?: number; lng?: number };
    suggest: string;
  }>();

  // ── shared op flow — ALL actions funnel through this ─────────────────────
  async function runOps(ops: ReplanOp[], handle: string): Promise<void> {
    if (!idempotencyKey || busy) return;
    busy = handle; flash = null;
    try {
      const r = await replanTrip(idempotencyKey, ops);
      if (isForbidden(r)) {
        // IDOR fix: this session's session_token/owner_token don't match the trip's
        // actual owner — honest refusal, plan left untouched. #203: expired-session
        // vs cross-user-ownership get distinct copy.
        flash = { kind: 'reject', msg: forbiddenMessage(r) };
      } else if (r.outcome === 'plan_ready' && (r.applied?.length ?? 0) > 0) {
        dispatch('replanned', { ...r.plan, idempotency_key: r.idempotency_key ?? r.plan?.idempotency_key });
        flash = r.rejected?.length
          ? { kind: 'reject', msg: `Updated, but: ${r.rejected.map((x) => x.reason).join('; ')}` }
          : r.notes?.length
            ? { kind: 'note', msg: `Updated · ${r.notes.join('; ')}` }
            : { kind: 'ok', msg: 'Updated ✓' };
      } else if (r.outcome === 'noop') {
        flash = { kind: 'reject', msg: `Couldn't apply — the itinerary changed. ${(r.rejected ?? []).map((x) => x.reason).join('; ')}` };
      } else if (r.outcome === 'unknown_plan') {
        flash = { kind: 'reject', msg: 'This plan is no longer available — re-plan to continue.' };
      } else if (r.outcome === 'plan_locked') {
        flash = { kind: 'reject', msg: 'This trip was cancelled — start a new plan.' };
      } else {
        flash = { kind: 'reject', msg: r.reason || "Couldn't apply that change safely. Refresh and try again." };
      }
    } catch (e) {
      flash = { kind: 'reject', msg: `Couldn't reach the planner: ${String(e)}` };
    } finally {
      busy = null;
      if (flash?.kind === 'ok') setTimeout(() => (flash = null), 2500);
    }
  }

  // ── drag-and-drop (HTML5 DnD; activity rows + suggestion chips + day tabs) ─
  interface DragPayload { leg_index: number; day_index: number; pos: number; ref: string; }
  interface SuggestionPayload { kind: 'suggestion'; leg_index: number; ua_ref: string; }

  // module-level flag so onDragOver on items/tabs can check type without reading DnD data.
  // 'day' was added for itinerary-scale-draft4/5.html's day-tab reorder (swap two days'
  // content) — draft5's audit fix requires every drop-target to gate on this so a
  // day-kind payload dropped somewhere that isn't a day-tab is a clean no-op, never
  // misrouted into the activity-move code path below (which used to be the only reader
  // of a dropped payload and would have produced the wrong "between cities" rejection).
  let dragKind: 'activity' | 'suggestion' | 'day' | null = null;

  function onDragStart(e: DragEvent, leg_index: number, day_index: number, pos: number, ref: string) {
    dragKind = 'activity';
    e.dataTransfer!.setData('text/plain', JSON.stringify({ leg_index, day_index, pos, ref }));
    e.dataTransfer!.effectAllowed = 'move';
  }

  // #121: drag a suggestion chip from the per-day pool into the timeline
  function onDragStartSuggestion(e: DragEvent, leg_index: number, ua_ref: string): void {
    dragKind = 'suggestion';
    e.dataTransfer!.setData('text/plain', JSON.stringify({ kind: 'suggestion', leg_index, ua_ref }));
    e.dataTransfer!.effectAllowed = 'copy';
  }

  function onDragOver(e: DragEvent) {
    // draft5.html disambiguation row 5: content drop-zones NEVER accept a 'day' payload
    // (that's exclusively a day-tab-to-day-tab operation) — leave it un-prevented so the
    // browser shows its native "not allowed" cursor and the tab snaps back on release.
    if (dragKind === 'day') return;
    e.preventDefault();
    (e.currentTarget as HTMLElement).classList.add('drop-target');
  }

  function onDragLeave(e: DragEvent) {
    (e.currentTarget as HTMLElement).classList.remove('drop-target');
  }

  function onDrop(e: DragEvent, dstLeg: number, dstDay: number, dstRawIndex: number) {
    e.preventDefault();
    if (dragKind === 'day') {
      // Collision-fix (draft5.html): a day-tab drag released over the CONTENT area,
      // not another tab. No-op — snap back, nothing happens.
      dragKind = null;
      (e.currentTarget as HTMLElement).classList.remove('drop-target');
      return;
    }
    dragKind = null;
    (e.currentTarget as HTMLElement).classList.remove('drop-target');
    let raw: unknown;
    try { raw = JSON.parse(e.dataTransfer!.getData('text/plain')); } catch { return; }

    // Suggestion drop into a drop-zone: add from unscheduled at this position
    const sug = raw as SuggestionPayload;
    if (sug?.kind === 'suggestion') {
      if (sug.leg_index !== dstLeg) {
        flash = { kind: 'reject', msg: "Can't add places from a different city." };
        return;
      }
      runOps(
        [{ op: 'add_place', leg_index: dstLeg, day_index: dstDay, position: dstRawIndex,
           attraction_ref: sug.ua_ref, from: 'unscheduled' }],
        `add:${sug.ua_ref}`,
      );
      return;
    }

    // Existing activity-move logic (unchanged)
    const s = raw as DragPayload;
    if (s.leg_index !== dstLeg) {
      flash = { kind: 'reject', msg: "Can't move places between cities here — refine the trip in chat." };
      return;
    }
    if (s.day_index === dstDay && s.pos === dstRawIndex) return;
    runOps(
      moveOps({ leg_index: dstLeg, src_day: s.day_index, src_pos: s.pos,
                dst_day: dstDay, dst_pos: dstRawIndex, ref: s.ref }),
      `move:${s.ref}`,
    );
  }

  // #121: drop a suggestion ONTO an existing activity → swap (remove + add)
  //
  // Bug fix (contributing factor to "drag from RightRail always lands at the
  // bottom"): this used to gate on the module-local `dragKind` flag alone, which
  // is ONLY set by this component's OWN onDragStart(Suggestion) — a drag that
  // originates in the separate RightRail.svelte component (its onSugDragStart
  // sets no state here at all) left dragKind at its default `null`, so this
  // never called preventDefault() and every `.item` row was a dead (non-
  // accepting) drop target for the entire duration of a RightRail-sourced drag.
  // Per the HTML5 DnD spec, an element that never preventDefault()s on dragover
  // never receives a 'drop' event either — so a RightRail card hovered over an
  // item wouldn't just skip the swap, the browser would refuse to let it drop
  // there at all, pushing the drag onward in search of ANY accepting target
  // (in practice: the big, always-accepting `drop-end` zone at the bottom).
  // Fix: also accept `effectAllowed === 'copy'`, which BOTH suggestion sources
  // (this component's onDragStartSuggestion AND RightRail's onSugDragStart) set
  // at dragstart — effectAllowed (unlike dataTransfer.getData()) is readable at
  // every DnD stage, not just on drop, so this correctly identifies a
  // suggestion-kind drag regardless of which component started it.
  function onDragOverItem(e: DragEvent): void {
    if (dragKind === 'suggestion' || e.dataTransfer?.effectAllowed === 'copy') {
      e.preventDefault();
      (e.currentTarget as HTMLElement).classList.add('swap-target');
    }
  }

  function onDragLeaveItem(e: DragEvent): void {
    (e.currentTarget as HTMLElement).classList.remove('swap-target');
  }

  function onDropOnItem(e: DragEvent, dstLeg: number, dstDay: number, dstPos: number, dstRef: string): void {
    (e.currentTarget as HTMLElement).classList.remove('swap-target');
    let raw: unknown;
    try { raw = JSON.parse(e.dataTransfer!.getData('text/plain')); } catch { dragKind = null; return; }
    const sug = raw as SuggestionPayload;
    dragKind = null;
    if (sug?.kind !== 'suggestion') return;
    e.preventDefault();
    if (sug.leg_index !== dstLeg) {
      flash = { kind: 'reject', msg: "Can't swap places from a different city." };
      return;
    }
    // Swap: remove the existing activity then add the suggestion at the same position
    runOps([
      { op: 'remove_place', leg_index: dstLeg, day_index: dstDay, position: dstPos, attraction_ref: dstRef },
      { op: 'add_place', leg_index: dstLeg, day_index: dstDay, position: dstPos,
        attraction_ref: sug.ua_ref, from: 'unscheduled' },
    ], `swap:${sug.ua_ref}`);
  }

  // ── per-day variant toggle ────────────────────────────────────────────────
  function toggleVariant(leg_index: number, day_index: number, currentVariant: 'fair' | 'wet') {
    const variant: 'fair' | 'wet' = currentVariant === 'wet' ? 'fair' : 'wet';
    runOps([{ op: 'switch_variant', leg_index, day_index, variant }],
           `variant:${leg_index}.${day_index}`);
  }

  // ── reflow ────────────────────────────────────────────────────────────────
  function reflow(leg_index: number) {
    runOps([{ op: 'reflow_from', leg_index, from_day_index: 0 }], `reflow:${leg_index}`);
  }

  // ── day variant helpers (cast inside fn body, not in Svelte template) ──────
  // DayDetail may carry _variant_active/_fair_weather_attractions set by the server
  // (or after a switch_variant call) but the type doesn't declare them — use unknown cast.
  function dayActiveVariant(d: unknown): 'fair' | 'wet' {
    const ext = d as Record<string, unknown>;
    return ext._variant_active === 'fair' ? 'fair' : 'wet';
  }

  function dayHasVariant(d: unknown): boolean {
    const ext = d as Record<string, unknown>;
    return !!(ext.bad_weather && Array.isArray(ext.fair_weather_attractions));
  }

  // ── remove place ─────────────────────────────────────────────────────────
  function removePlace(leg_index: number, day_index: number, position: number, ref: string) {
    runOps([{ op: 'remove_place', leg_index, day_index, position, attraction_ref: ref }],
           `rm:${ref}`);
  }

  // ── #126: locate an itinerary item on the shared map ─────────────────────
  // Shared by locateItem() (click handler) AND the template's `disabled` check
  // (Finding #3, map-pin-bug sweep): curated seed entries can carry null
  // lat/lon (society/poi_supplement.json), and the button used to render
  // enabled-looking regardless, silently no-op-ing on click. One coordinate
  // lookup, one source of truth for "does this item even have a place to show".
  function itemLatLon(it: TimelineItem, day: DayDetail): { lat: number; lon: number } | null {
    let lat: number | undefined, lon: number | undefined;
    if (it.kind === 'activity' && it.attrIndex >= 0) {
      const a = day.attractions?.[it.attrIndex];
      lat = a?.lat; lon = a?.lon;
    } else if (it.kind === 'meal') {
      const mk = it.slot.toLowerCase();
      const ms = day.meals as Record<string, { lat?: number; lon?: number } | null | undefined> | undefined;
      lat = ms?.[mk]?.lat; lon = ms?.[mk]?.lon;
    }
    return (lat != null && lon != null) ? { lat, lon } : null;
  }

  function locateItem(it: TimelineItem, day: DayDetail, city: string | undefined): void {
    const coords = itemLatLon(it, day);
    if (!coords) return; // no-coords case is now disabled in the template — defensive only
    mapFocus.set({
      lat: coords.lat, lng: coords.lon, label: it.name,
      name: it.name,                                   // already displayName()-clean
      city,                                            // may be undefined → handled downstream
      category: it.kind === 'meal' ? 'restaurant' : 'attraction',
    });
    dispatch('locate', { label: it.name, lat: coords.lat, lng: coords.lon });
  }

  // ── meal swap panel ───────────────────────────────────────────────────────
  let mealPanelOpen: { li: number; di: number; slot: string } | null = null;
  function openMealPanel(slot: string, li: number, di: number): void {
    if (mealPanelOpen?.li === li && mealPanelOpen?.di === di && mealPanelOpen?.slot === slot) {
      mealPanelOpen = null; // toggle off
    } else {
      mealPanelOpen = { li, di, slot };
    }
  }

  // (G2, #181) return type widened from {name, cuisine} to the full MealOption so
  // name_en/name_source survive the pool lookup — the swap panel routes each
  // alternative through namePresentation(), which needs those fields to avoid
  // silently dropping a served name_en (the bug this fix closes).
  function getMealPool(day: DayDetail, slot: string): MealOption[] {
    const raw = (day as Record<string, unknown>)['meal_pool'];
    if (!raw || typeof raw !== 'object') return [];
    const pool = (raw as Record<string, unknown[]>)[slot];
    return Array.isArray(pool) ? (pool as MealOption[]) : [];
  }

  // ── add from unscheduled pool ─────────────────────────────────────────────
  function addFromUnscheduled(
    leg_index: number, day_index: number, ref: string,
    endPosition: number,
  ) {
    runOps([{ op: 'add_place', leg_index, day_index, position: endPosition,
              attraction_ref: ref, from: 'unscheduled' }],
           `add:${ref}`);
  }

  // ════════════════════════════════════════════════════════════════════════
  // Part A — itinerary-scale redesign (mockups/itinerary-scale-draft1..5.html)
  // ════════════════════════════════════════════════════════════════════════

  // ── leg accordion (draft1/2/3 Decision A, locked in draft3 State 5) ───────
  // "Collapse-by-default at ≥8 total trip days OR ≥4 legs; otherwise all legs open."
  // Leg 0 is always open by default when the threshold is hit; every leg's open/closed
  // state can be toggled independently (multiple legs CAN be open at once — draft1
  // State 1 annotation: a user comparing Tokyo vs Kyoto plans can open both).
  // legOpenOverride only records EXPLICIT user toggles so the default keeps tracking
  // the threshold across replans (plans is a new array reference on every edit) while
  // still remembering what the user chose to expand/collapse.
  let legOpenOverride: Record<number, boolean> = {};
  $: totalTripDays = plans.reduce((sum, dp) => sum + (dp.days?.length ?? dp.num_days ?? 0), 0);
  $: legCollapseDefault = totalTripDays >= 8 || plans.length >= 4;
  function toggleLeg(li: number, defaultOpen: boolean): void {
    const current = legOpenOverride[li] !== undefined ? legOpenOverride[li] : defaultOpen;
    legOpenOverride = { ...legOpenOverride, [li]: !current };
  }

  // ── per-leg day-tabs threshold (draft3 Decision C, locked) ────────────────
  // "Per-leg, triggered at ≥6 days in that leg. Short legs stay flat."
  const DAY_TABS_THRESHOLD = 6;
  function legUsesTabs(dp: DayPlan): boolean {
    return (dp.days?.length ?? 0) >= DAY_TABS_THRESHOLD;
  }
  function numDaysOf(dp: DayPlan): number {
    return dp.days?.length ?? 0;
  }
  function isBoundaryDay(dp: DayPlan, di: number): boolean {
    const n = numDaysOf(dp);
    return n > 0 && (di === 0 || di === n - 1);
  }
  function boundaryTitle(dp: DayPlan, di: number): string {
    const n = numDaysOf(dp);
    if (di === 0) return 'First day of this leg — arrival, not reorderable';
    if (di === n - 1) return 'Last day of this leg — checkout, not reorderable';
    return '';
  }
  // Honest per-leg collapsed summary — built ONLY from server-supplied names actually
  // in the plan (never fabricated), mirroring draft1/2's "Fushimi Inari, Kinkaku-ji,
  // Gion + 18 more." example.
  function legCollapsedSummary(dp: DayPlan): string {
    const names: string[] = [];
    for (const day of dp.days ?? []) {
      for (const a of day.attractions ?? []) {
        const n = displayName(a);
        if (n) names.push(n);
      }
    }
    if (!names.length) return 'No activities catalogued yet — click to expand.';
    const shown = names.slice(0, 3).join(', ');
    const rest = names.length - 3;
    return rest > 0 ? `${shown} + ${rest} more.` : `${shown}.`;
  }

  let activeDayTab: Record<number, number> = {};

  function setActiveDay(li: number, di: number, dp: DayPlan): void {
    const cur = activeDayTab[li] ?? 0;
    if (cur === di) return;
    activeDayTab = { ...activeDayTab, [li]: di };
    const day = dp.days?.[di];
    if (day) ensureDayPhotosFetched(li, di, dp, day);
  }

  // ── day-tab drag-to-reorder (draft4/5) ─────────────────────────────────────
  // Desktop: plain click-and-drag on an interior tab (native HTML5 DnD, same
  // no-hold-delay behavior as the existing activity drag — draft4's disambiguation
  // table explicitly rejects an artificial hold on desktop: "a mouse can't
  // accidentally trigger a scroll the way a finger can").
  // Mobile/tablet: long-press (draft5 scope-note: this app's FIRST touch-drag
  // surface — implemented below with a hold-timer + elementFromPoint hit-test;
  // deliberately NOT implementing edge-autoscroll, a reasonable v1 simplification
  // for a tab strip — documented in the implementation report, not silently dropped).
  // Boundary lock (draft5 fix): first/last day of a leg is never draggable at all —
  // rejected client-side before any drag starts, never via a server round-trip.
  let dayDragSource: { li: number; di: number } | null = null;
  let dayDragTarget: { li: number; di: number } | null = null;

  function onDayTabDragStart(e: DragEvent, li: number, di: number, dp: DayPlan): void {
    if (isBoundaryDay(dp, di)) { e.preventDefault(); return; }
    dragKind = 'day';
    dayDragSource = { li, di };
    e.dataTransfer!.setData('text/plain', JSON.stringify({ kind: 'day', leg_index: li, day_index: di }));
    e.dataTransfer!.effectAllowed = 'move';
  }

  // ── dwell-switch (draft1/2/3): hovering a day-tab with an activity/suggestion
  // drag in progress swaps the VISIBLE day after 600ms so the target day's own drop
  // zones become reachable. The tab itself never ACCEPTS the drop — only the day
  // content's existing drop-zones do (draft2 tech-note: day panels stay mounted with
  // display:none, never unmounted, so their drop zones survive being off-screen).
  let dwellTimer: ReturnType<typeof setTimeout> | null = null;
  let dwellKey: string | null = null;
  function clearDwell(): void {
    if (dwellTimer) { clearTimeout(dwellTimer); dwellTimer = null; }
    dwellKey = null;
  }

  function onDayTabDragOver(e: DragEvent, li: number, di: number, dp: DayPlan): void {
    if (dragKind === 'day') {
      if (isBoundaryDay(dp, di)) return; // boundary tab is never a valid swap target
      if (dayDragSource && dayDragSource.li === li && dayDragSource.di === di) return;
      if (dayDragSource && dayDragSource.li !== li) return; // tabs never span legs
      e.preventDefault();
      dayDragTarget = { li, di };
      return;
    }
    if (dragKind === 'activity' || dragKind === 'suggestion' || e.dataTransfer?.effectAllowed === 'copy') {
      e.preventDefault();
      const key = `${li}:${di}`;
      if (dwellKey === key) return;
      clearDwell();
      dwellKey = key;
      dwellTimer = setTimeout(() => { setActiveDay(li, di, dp); dwellKey = null; }, 600);
    }
  }

  function onDayTabDragLeave(e: DragEvent, li: number, di: number): void {
    if (dragKind === 'day') {
      if (dayDragTarget?.li === li && dayDragTarget?.di === di) dayDragTarget = null;
      return;
    }
    if (dwellKey === `${li}:${di}`) clearDwell();
  }

  function onDayTabDrop(e: DragEvent, li: number, di: number, dp: DayPlan): void {
    e.preventDefault();
    clearDwell();
    if (dragKind !== 'day') { dayDragTarget = null; return; } // only 'day' payloads land here
    dragKind = null;
    const src = dayDragSource;
    dayDragSource = null; dayDragTarget = null;
    if (!src || src.li !== li || src.di === di) return;
    if (isBoundaryDay(dp, di) || isBoundaryDay(dp, src.di)) return; // client-side boundary lock
    runOps([{ op: 'swap_days', leg_index: li, day_a: src.di, day_b: di }],
           `swap_days:${li}.${src.di}.${di}`);
  }

  function onDayTabDragEnd(): void {
    dragKind = null;
    dayDragSource = null;
    dayDragTarget = null;
    clearDwell();
  }

  // ── mobile/tablet long-press-to-drag (draft4/5) ────────────────────────────
  let longPressTimer: ReturnType<typeof setTimeout> | null = null;
  let longPressStart: { x: number; y: number } | null = null;
  let touchDragLive = false;
  const LONG_PRESS_MS = 500;
  const LONG_PRESS_CANCEL_PX = 10;

  function clearLongPress(): void {
    if (longPressTimer) { clearTimeout(longPressTimer); longPressTimer = null; }
    longPressStart = null;
  }

  function onDayTabTouchStart(e: TouchEvent, li: number, di: number, dp: DayPlan): void {
    if (isBoundaryDay(dp, di)) return; // no drag affordance at all on a boundary day
    const t = e.touches[0];
    longPressStart = { x: t.clientX, y: t.clientY };
    clearLongPress();
    longPressTimer = setTimeout(() => {
      dragKind = 'day';
      dayDragSource = { li, di };
      touchDragLive = true;
    }, LONG_PRESS_MS);
  }

  function onDayTabTouchMove(e: TouchEvent): void {
    const t = e.touches[0];
    if (!touchDragLive) {
      // Still waiting for the hold to fire — if the finger moved first, this is a
      // normal tab-strip SCROLL, not a reorder. Cancel the pending long-press and
      // let the native horizontal scroll continue untouched (no preventDefault).
      if (longPressStart) {
        const dx = Math.abs(t.clientX - longPressStart.x);
        const dy = Math.abs(t.clientY - longPressStart.y);
        if (dx > LONG_PRESS_CANCEL_PX || dy > LONG_PRESS_CANCEL_PX) clearLongPress();
      }
      return;
    }
    // A day-reorder drag is live — stop the strip from scrolling under the finger.
    e.preventDefault();
    const el = document.elementFromPoint(t.clientX, t.clientY) as HTMLElement | null;
    const tabEl = el?.closest('[data-day-tab="1"]') as HTMLElement | null;
    if (!tabEl) { dayDragTarget = null; return; }
    const li2 = Number(tabEl.dataset.legIndex);
    const di2 = Number(tabEl.dataset.dayIndex);
    const dp2 = plans[li2];
    if (!dp2 || li2 !== dayDragSource?.li || di2 === dayDragSource?.di || isBoundaryDay(dp2, di2)) {
      dayDragTarget = null;
    } else {
      dayDragTarget = { li: li2, di: di2 };
    }
  }

  function onDayTabTouchEnd(e: TouchEvent): void {
    if (touchDragLive) {
      e.preventDefault(); // suppress the ghost click that would otherwise follow this touch
      touchDragLive = false;
      const src = dayDragSource, dst = dayDragTarget;
      dragKind = null; dayDragSource = null; dayDragTarget = null;
      if (src && dst && src.li === dst.li && src.di !== dst.di) {
        runOps([{ op: 'swap_days', leg_index: src.li, day_a: src.di, day_b: dst.di }],
               `swap_days:${src.li}.${src.di}.${dst.di}`);
      }
    }
    clearLongPress();
  }

  // ── swipe navigation between adjacent days, mobile/tablet content panel only
  // (draft4 State 1). Desktop never sees these events (no touch surface to fire
  // them from), so no extra guard is needed to keep this mobile/tablet-only.
  let swipeStartX = 0, swipeStartY = 0, swipeTracking = false;
  const SWIPE_MIN_PX = 50;
  function onDayContentTouchStart(e: TouchEvent): void {
    const t = e.touches[0];
    swipeStartX = t.clientX; swipeStartY = t.clientY; swipeTracking = true;
  }
  function onDayContentTouchEnd(e: TouchEvent, li: number, dp: DayPlan): void {
    if (!swipeTracking) return;
    swipeTracking = false;
    const t = e.changedTouches[0];
    const dx = t.clientX - swipeStartX;
    const dy = t.clientY - swipeStartY;
    if (Math.abs(dx) < SWIPE_MIN_PX || Math.abs(dx) < Math.abs(dy) * 1.5) return; // vertical scroll
    const cur = activeDayTab[li] ?? 0;
    const next = dx < 0 ? cur + 1 : cur - 1;
    const n = numDaysOf(dp);
    if (next < 0 || next >= n) return; // no swipe past the leg's own first/last day
    setActiveDay(li, next, dp);
  }

  // local-name / romanization display tiers (draft1 State 3, locked B/draft2-3):
  // hasNonLatinScript()/namePresentation() live in ../itinerary.ts (shared canonical
  // copy also used by RightRail.svelte/Preview.svelte/Map.svelte) -- imported above,
  // not re-declared here, so all surfaces stay in sync (see fix/nonlatin-punctuation-fp).
  function rawNameSourceFor(it: TimelineItem, day: DayDetail): { name?: string; name_en?: string; name_source?: unknown } | null {
    if (it.kind === 'activity' && it.attrIndex >= 0) return (day.attractions?.[it.attrIndex] ?? null) as unknown as { name?: string; name_en?: string; name_source?: unknown } | null;
    if (it.kind === 'meal') {
      const slotKey = it.slot.toLowerCase();
      const meals = day.meals as unknown as Record<string, { name?: string; name_en?: string; name_source?: unknown } | null | undefined> | undefined;
      return meals?.[slotKey] ?? null;
    }
    return null;
  }

  // ════════════════════════════════════════════════════════════════════════
  // Part B — Item 3: per-item photo thumbnails (mockups/image-placement-draft2.html)
  // ════════════════════════════════════════════════════════════════════════
  //
  // Always-visible thumbnails, so they can't wait for a per-item click the way
  // PlacePhotos.svelte's "📷 Photo" toggle does. Fetch is keyed to the ACTIVE
  // day-tab only (or, for a flat/untabbed leg, to every day — all of which are
  // already simultaneously visible, so there is no "inactive day" to withhold a
  // fetch from; see the implementation report for this reasoning). Re-activating
  // a previously-viewed day reuses the cached result (keyed by li:di) rather than
  // re-fetching. The lightbox reuses PlacePhotos.svelte's exact openLightbox/
  // closeLightbox/lb-overlay pattern (same markup, same function names) rather
  // than reinventing it — PlacePhotos.svelte itself is not modified.
  interface ThumbState { photos: string[]; fetched: boolean; loading: boolean; }
  let thumbCache: Record<string, ThumbState> = {};
  function thumbKeyOf(name: string, city: string, country: string): string {
    return `${name}|${city}|${country}`;
  }
  async function fetchThumb(name: string, city: string, country: string): Promise<void> {
    if (!name) return;
    const key = thumbKeyOf(name, city, country);
    const existing = thumbCache[key];
    if (existing?.fetched || existing?.loading) return;
    thumbCache = { ...thumbCache, [key]: { photos: [], fetched: false, loading: true } };
    let photos: string[] = [];
    try {
      const r = await fetch(`${API_BASE}/place_card`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ mode: 'detail', name, city, country }),
      });
      if (r.ok) {
        const d = await r.json();
        if (d.status === 'ok' && d.place) {
          photos = (d.place.photos ?? []).slice(0, 5).map(
            (p: string) => (p.startsWith('http') ? p : `${API_BASE}${p}`),
          );
        }
      }
    } catch {
      // fail silently on the network path — the icon-tile fallback IS the honest
      // "no photo" explanation for this item; no separate error state needed.
    }
    thumbCache = { ...thumbCache, [key]: { photos, fetched: true, loading: false } };
  }

  let dayBatchStarted: Record<string, boolean> = {};
  // Position-keyed fetch guard ("li:di") — must be cleared whenever `plans` gets a new
  // identity (e.g. after a successful replan / day-swap), because the CONTENT at a given
  // "li:di" slot may have changed even though the position hasn't. Without this reset, a
  // swapped-in day would be wrongly treated as "already fetched" and render blank/stale
  // icon-tiles for photos that actually exist. thumbCache itself is correctly content-keyed
  // (name|city|country) and does NOT need resetting here.
  let dayBatchStartedForPlans: DayPlan[] | null = null;
  $: if (plans !== dayBatchStartedForPlans) {
    dayBatchStartedForPlans = plans;
    dayBatchStarted = {};
  }

  function ensureDayPhotosFetched(li: number, di: number, dp: DayPlan, day: DayDetail): void {
    const key = `${li}:${di}`;
    if (dayBatchStarted[key]) return;
    dayBatchStarted = { ...dayBatchStarted, [key]: true };
    for (const it of mealAnchoredTimeline(day)) {
      if (it.name) fetchThumb(it.name, dp.city ?? '', dp.country ?? '');
    }
  }

  // Coordinates Part B's lazy fetch with Part A's leg-open/day-tab state: a
  // collapsed leg's days never fetch (nothing is visible); an open leg with tabs
  // fetches only its active day; an open leg without tabs (flat) fetches every
  // day since they're all simultaneously on screen already. Directly references
  // legOpenOverride/legCollapseDefault/activeDayTab (not via helper functions) so
  // Svelte's reactive-statement dependency tracking picks up every trigger.
  //
  // The leg-hero thumbnail (`leg.hotel_title`) is the one exception: it is fetched
  // EAGERLY for every leg regardless of open/collapsed state (still bounded to one
  // fetch per leg, cheap) — matching Preview.svelte's leg-hero banner, which always
  // fetches with no visibility gating at all. Gating this on `isOpen` would leave a
  // collapsed leg's thumbnail permanently unfetched, so its icon-tile fallback would
  // render "No photo available" — a false "confirmed absent" claim when the truth is
  // just "not yet checked". Fetching eagerly avoids fabricating that claim.
  $: {
    plans.forEach((dp, li) => {
      const leg = legById.get(dp.leg_id ?? '');
      if (leg?.hotel_title) fetchThumb(leg.hotel_title, dp.city ?? '', dp.country ?? '');

      const overridden = legOpenOverride[li];
      const isOpen = overridden !== undefined ? overridden : (!legCollapseDefault || li === 0);
      if (!isOpen) return;
      const days = dp.days ?? [];
      if (legUsesTabs(dp)) {
        const di = activeDayTab[li] ?? 0;
        const day = days[di];
        if (day) ensureDayPhotosFetched(li, di, dp, day);
      } else {
        days.forEach((day, di) => ensureDayPhotosFetched(li, di, dp, day));
      }
    });
  }

  // Lightbox — mirrors PlacePhotos.svelte's openLightbox/closeLightbox/lb-overlay
  // pattern exactly (same names, same behavior), scoped to this component since
  // thumbnails are always-visible rather than embedded <PlacePhotos> instances.
  //
  // B5 spillover (#168, item 4): this overlay is position:fixed/inset:0/z-index 9999 —
  // same class of element as Map.svelte's mobile pin-sheet, which fix/assistant-bubble-
  // overlap already found and fixed covering ChatPane's floating assistant trigger
  // (z-index 300). Verified live (document.elementFromPoint + an actual blocked
  // Playwright click) that THIS lightbox has the exact same collision on mobile and
  // was never wired into hideMobileBubble. Reuses the same seam: a small store
  // (itineraryLightboxOpen, mapStore.ts) App.svelte ORs into ChatPane's
  // hideMobileBubble, not a parallel mechanism.
  let lightboxSrc: string | null = null;
  function openLightbox(src: string): void { lightboxSrc = src; itineraryLightboxOpen.set(true); }
  function closeLightbox(): void { lightboxSrc = null; itineraryLightboxOpen.set(false); }
  // Reset on unmount so navigating away without an explicit close (e.g. "Start new
  // trip") never leaves the assistant bubble stuck hidden on a stale signal — same
  // guard Map.svelte's onDestroy already applies to placeSheetOpen.
  onDestroy(() => itineraryLightboxOpen.set(false));
</script>

<div class="itin" data-testid="itinerary">
  {#if flash}
    <div class="flash {flash.kind}" data-testid="replan-flash" role="status">{flash.msg}</div>
  {/if}

  <!-- Lightbox overlay (Part B) — reuses PlacePhotos.svelte's exact pattern. -->
  {#if lightboxSrc}
    <!-- svelte-ignore a11y-click-events-have-key-events a11y-no-static-element-interactions -->
    <div class="lb-overlay" on:click={closeLightbox}>
      <button class="lb-close" on:click={closeLightbox} aria-label="Close photo">✕</button>
      <!-- svelte-ignore a11y-click-events-have-key-events a11y-no-noninteractive-element-interactions -->
      <img src={lightboxSrc} alt="" class="lb-img" on:click|stopPropagation />
    </div>
  {/if}

  {#each plans as dp, li}
    {@const leg = legById.get(dp.leg_id ?? '')}
    {@const usesTabs = legUsesTabs(dp)}
    {@const isOpen = legOpenOverride[li] !== undefined ? legOpenOverride[li] : (!legCollapseDefault || li === 0)}
    {@const activeDi = activeDayTab[li] ?? 0}
    {@const legThumb = leg?.hotel_title ? thumbCache[thumbKeyOf(leg.hotel_title, dp.city ?? '', dp.country ?? '')] : undefined}
    {@const live = livePriceOverlay(leg)}

    <div class="leg-block">
      <!-- Leg header ALWAYS renders (city/nights/hotel/honesty flags) — only the
           day-by-day BODY collapses (draft1 State 1 annotation). -->
      <div class="leg-h" data-testid="leg-h-{li}">
        {#if leg?.hotel_title}
          <!-- Part B: always-visible ~56px leg-hero thumbnail beside the hotel header. -->
          {@const legThumbLoading = !legThumb?.fetched}
          <button type="button" class="leg-thumb" class:icon-tile={!legThumb?.photos?.length}
            class:thumb-loading={legThumbLoading}
            disabled={!legThumb?.photos?.length}
            aria-label={legThumb?.photos?.length ? `View photos of ${leg.hotel_title}` : legThumbLoading ? `Loading photo of ${leg.hotel_title}` : `No photo available for ${leg.hotel_title}`}
            title={legThumb?.photos?.length ? 'Tap for photos' : legThumbLoading ? 'Loading photo…' : 'No photo available'}
            on:click|stopPropagation={() => legThumb?.photos?.[0] && openLightbox(legThumb.photos[0])}>
            {#if legThumb?.photos?.length}
              <img src={legThumb.photos[0]} alt="" loading="lazy" decoding="async" />
            {:else if legThumbLoading}
              <span class="thumb-glyph thumb-loading-glyph" aria-hidden="true">⋯</span>
            {:else}
              <span class="thumb-glyph">🏨</span>
            {/if}
          </button>
        {/if}
        <div class="leg-h-body">
          <div class="leg-h-top">
            <button type="button" class="leg-h-toggle" on:click={() => toggleLeg(li, !legCollapseDefault || li === 0)}
              aria-expanded={isOpen} data-testid="leg-toggle-{li}">
              <span class="dot"></span>
              <span class="city">{dp.city ?? `Leg ${li + 1}`}</span>
              <span class="meta">
                {dp.checkin ?? ''}{#if dp.checkout} → {dp.checkout}{/if}
                {#if dp.num_days}· {dp.num_days} night{dp.num_days === 1 ? '' : 's'}{/if}
              </span>
              <span class="leg-chevron" class:open={isOpen}>▶</span>
            </button>
            {#if editable}
              <button class="reflow-btn" data-testid="reflow-{li}"
                aria-busy={busy === `reflow:${li}`}
                class:busy={busy === `reflow:${li}`}
                on:click={() => reflow(li)}
                title="Tidy days — rebalance schedule">
                {busy === `reflow:${li}` ? '…' : '↻ Tidy days'}
              </button>
            {/if}
          </div>
          {#if leg?.hotel_title}
            <!-- #168 (honest name-tier B1): the catalog only carries a flat `title`
                 string for lodging (no name/name_en split, no name_source — confirmed
                 by reading ucp-merchant/catalog.go + catalog_supplement.json and the
                 leg-building paths in orchestrator.py/recovery.py). So this is routed
                 through the SAME namePresentation() pipeline as activities/meals with a
                 synthetic { name: hotel_title } object: `local` naturally stays null
                 (nothing to show as a companion — there's only one string), and the new
                 `unreadable` flag fires honestly whenever hotel_title itself is
                 non-Latin script. A real name/name_en split for hotels is a backend
                 follow-up (see report), not faked here. -->
            {@const hnp = namePresentation({ name: leg.hotel_title })}
            <div class="hotel-name" data-testid="hotel-name-{li}">
              <span class="hotel-name-text">🏨 {hnp.primary}</span>
              {#if hnp.local}<span class="local-name">{hnp.local}</span>{/if}
              {#if hnp.badge}<span class="name-src-badge" class:verified={hnp.badge !== 'romanized'} class:romanized={hnp.badge === 'romanized'}>{hnp.badge}</span>{/if}
              {#if hnp.unreadable}<span class="name-src-badge unreadable" title="No English name available">shown in original script — no English name available</span>{/if}
            </div>
          {/if}
          {#if leg?.total_cents != null}
            <!-- #206: per-leg lodging cost (legs[].total_cents), invisible before —
                 a multi-leg trip only ever showed one combined package total, so
                 users couldn't see the per-city cost split (e.g. Singapore $680 +
                 Bangkok $850 folded into a single $1530 aggregate). Only rendered
                 when the server genuinely attaches total_cents on THIS leg — no
                 fabricated $0/placeholder for legs where it's absent. cost_basis_label
                 (core/cost_basis.py, #42 PART A) mirrors the existing
                 transport_pricing.basis_label pattern: reuse .ic-caveat for the same
                 dim/informational honesty-caveat treatment. -->
            <div class="leg-cost" data-testid="leg-cost-{li}">
              <span class="leg-cost-amount" data-testid="leg-cost-amount-{li}">{centsToUsd(leg.total_cents)}</span>
              {#if leg.cost_basis_label}
                <span class="ic-caveat leg-cost-basis" data-testid="leg-cost-basis-{li}">{leg.cost_basis_label}</span>
              {/if}
            </div>
          {/if}
          {#if leg?.unverified_lodging}
            <!-- data-testid intentionally UNINDEXED (kept identical to the pre-redesign
                 markup) — e2e/smoke.spec.ts and e2e/staging/edge-cases.spec.ts query
                 getByTestId('unverified-lodging') exactly and are off-limits to edit.
                 itinerary-scale-draft3.html's per-leg-indexed testid suggestion is
                 flagged, not applied — see implementation report. -->
            <div class="unverified" data-testid="unverified-lodging">⚠ Lodging unverified{#if leg.note} — {leg.note}{/if}</div>
          {/if}
          {#if dp.notes?.length}
            <!-- #190: day-planner honesty disclosures (society/agents/day_planner_agent.py
                 ~L855-876) — cuisine-fallback / supper-left-empty / meal-chain-cap notes.
                 Server-authored text already carries an explicit "(not fabricated)"
                 qualifier, so this is styled as a dim informational caveat (matching
                 .ic-caveat elsewhere in this file), NOT the amber .unverified/.assumption-notes
                 warning treatment — these aren't alarms, just honest disclosure. -->
            <ul class="dayplan-notes" data-testid="dayplan-notes-{li}">
              {#each dp.notes as n}<li>{n}</li>{/each}
            </ul>
          {/if}
        </div>
      </div>

      {#if !isOpen}
        <div class="leg-collapsed-summary" data-testid="leg-collapsed-{li}">{legCollapsedSummary(dp)}</div>
      {:else}
        <div class="leg-body">
          <!-- DISPLAY-ONLY live best-price overlay (PROD/live edition). Renders ONLY when the
               backend attaches a status:'ok' overlay; in UAT (seeded) this is absent and the
               block emits nothing — the seeded view stays byte-identical. -->
          {#if live}
            {@const href = safeHref(live.deeplink)}
            {@const price = live.currency === 'USD'
              ? centsToUsd(live.lowest_price_cents)
              : `${live.currency} ${centsToUsd(live.lowest_price_cents).replace('$', '')}`}
            <div class="live-price" data-testid="live-price-{li}">
              <span class="lp-amount" data-testid="live-price-amount">{price}</span>
              <span class="lp-badge" data-testid="live-price-badge">live</span>
              {#if live.source}<span class="lp-source" data-testid="live-price-source">via {live.source}</span>{/if}
              {#if href}
                <a class="lp-book" data-testid="live-price-deeplink"
                  href={href} target="_blank" rel="noopener noreferrer">Book →</a>
              {/if}
            </div>
          {/if}
          {#each dp.airport_transfer ?? [] as t}
            {#if t.direction !== 'outbound'}
              <!-- B3 fix: t.label is already a fully-formatted string from the server
                   (build_transfer_hops: "~75 min, taxi, est. (from city centre — ...)"),
                   which already embeds minutes/mode. Only synthesize + append minutes
                   from the raw fields when label is absent, so we never show the same
                   duration twice ("~75 min, taxi, est. · ~75 min"). -->
              {@const hasLabel = !!t.label}
              <div class="ribbon">✈️ {hasLabel ? t.label : `${t.from ?? 'Airport'} → ${t.to ?? dp.city}`}{#if !hasLabel && t.minutes} · ~{t.minutes} min{/if}</div>
            {/if}
          {/each}

          <!-- Part A / draft3 Decision C: day-tabs only when this leg is ≥6 days long;
               shorter legs stay flat (below), identical to the pre-redesign markup. -->
          {#if usesTabs}
            <div class="day-tabs" data-testid="day-tabs-{li}">
              {#each dp.days ?? [] as day, di}
                {@const boundary = isBoundaryDay(dp, di)}
                <div class="day-tab"
                  class:active={activeDi === di}
                  class:boundary
                  class:dragging={dayDragSource?.li === li && dayDragSource?.di === di}
                  class:swap-target={dayDragTarget?.li === li && dayDragTarget?.di === di}
                  class:dragover-pending={dwellKey === `${li}:${di}`}
                  data-testid="day-tab-{li}-{di}"
                  data-day-tab="1" data-leg-index={li} data-day-index={di}
                  role="button" tabindex="0"
                  title={boundary ? boundaryTitle(dp, di) : undefined}
                  draggable={!boundary}
                  on:click={() => setActiveDay(li, di, dp)}
                  on:keypress={(e) => e.key === 'Enter' && setActiveDay(li, di, dp)}
                  on:dragstart={(e) => onDayTabDragStart(e, li, di, dp)}
                  on:dragover={(e) => onDayTabDragOver(e, li, di, dp)}
                  on:dragleave={(e) => onDayTabDragLeave(e, li, di)}
                  on:drop={(e) => onDayTabDrop(e, li, di, dp)}
                  on:dragend={onDayTabDragEnd}
                  on:touchstart={(e) => onDayTabTouchStart(e, li, di, dp)}
                  on:touchmove={onDayTabTouchMove}
                  on:touchend={onDayTabTouchEnd}>
                  Day {(day.day_index ?? di) + 1}
                  {#if boundary}<span class="lock">🔒</span>{/if}
                </div>
              {/each}
            </div>
          {/if}

          <div class="day-panel-group"
            on:touchstart={usesTabs ? onDayContentTouchStart : undefined}
            on:touchend={usesTabs ? (e) => onDayContentTouchEnd(e, li, dp) : undefined}>
            {#each dp.days ?? [] as day, di}
              {@const items = mealAnchoredTimeline(day)}
              {@const activeVariant = dayActiveVariant(day)}
              {@const hasVariant = dayHasVariant(day)}
              <div class="day" class:wet={day.bad_weather} class:tab-hidden={usesTabs && activeDi !== di}
                data-testid="day-panel-{li}-{di}">
                <div class="day-top">
                  <span class="day-n">Day {(day.day_index ?? di) + 1}</span>
                  {#if day.bad_weather}<span class="wx">⛈ wet-weather risk · indoor-first</span>{/if}
                  {#if editable && hasVariant}
                    <button class="variant-btn" data-testid="variant-{li}-{di}"
                      aria-busy={busy === `variant:${li}.${di}`}
                      class:busy={busy === `variant:${li}.${di}`}
                      on:click={() => toggleVariant(li, di, activeVariant)}>
                      {busy === `variant:${li}.${di}` ? '…' : activeVariant === 'wet' ? 'Show fair-weather plan ☀' : 'Show wet-weather plan ⛈'}
                    </button>
                  {/if}
                </div>

                {#if items.length}
                  <div class="tl">
                    {#each items as it, ii}
                      {#if ii === 0 || items[ii - 1].slot !== it.slot}
                        <div class="slot">{it.slot}</div>
                      {/if}
                      {#if it.kind === 'activity' && editable}
                        <!-- Drop zone BEFORE this item -->
                        <div class="drop-zone" data-testid="drop-{li}-{di}-{it.attrIndex}"
                          role="button" tabindex="-1"
                          on:dragover={onDragOver}
                          on:dragleave={onDragLeave}
                          on:drop={(e) => onDrop(e, li, di, it.attrIndex)}>
                        </div>
                      {/if}
                      {@const np = namePresentation(rawNameSourceFor(it, day))}
                      {@const thumb = thumbCache[thumbKeyOf(it.name, dp.city ?? '', dp.country ?? '')]}
                      {@const thumbLoading = !thumb?.fetched}
                      {@const hasCoords = !!itemLatLon(it, day)}
                      <div class="item"
                        role="listitem"
                        draggable={editable && it.kind === 'activity'}
                        class:draggable={editable && it.kind === 'activity'}
                        class:busy={busy === `rm:${it.name}` || busy === `move:${it.name}`}
                        aria-busy={busy === `rm:${it.name}` ? true : undefined}
                        data-testid="item-{it.kind}-{ii}"
                        on:dragstart={it.kind === 'activity' ? (e) => onDragStart(e, li, di, it.attrIndex, it.name) : undefined}
                        on:dragover={editable && it.kind === 'activity' ? onDragOverItem : undefined}
                        on:dragleave={editable && it.kind === 'activity' ? onDragLeaveItem : undefined}
                        on:drop={editable && it.kind === 'activity' ? (e) => onDropOnItem(e, li, di, it.attrIndex, it.name) : undefined}>
                        <!-- Part B: always-visible per-item thumbnail (48–56px desktop /
                             44px mobile). Tap → lightbox when a real photo exists; a
                             distinct dashed icon-tile (slot glyph) when it doesn't — the
                             honest per-item "no photo" explanation, never mistakable for
                             a real photo. Its own on:click|stopPropagation keeps a tap
                             from bubbling into the row's native HTML5 drag handlers above
                             (see report: the long-press day-reorder gesture lives entirely
                             on day-TABS, a different DOM region, so there is no gesture
                             conflict to resolve here — this stopPropagation is defensive
                             consistency with the row's other inline buttons, not a fix
                             for an actual collision). -->
                        <button type="button" class="item-thumb" class:icon-tile={!thumb?.photos?.length}
                          class:thumb-loading={thumbLoading}
                          disabled={!thumb?.photos?.length}
                          aria-label={thumb?.photos?.length ? `View photos of ${it.name}` : thumbLoading ? `Loading photo of ${it.name}` : `No photo available for ${it.name}`}
                          title={thumb?.photos?.length ? 'Tap for photos' : thumbLoading ? 'Loading photo…' : 'No photo available'}
                          on:click|stopPropagation={() => thumb?.photos?.[0] && openLightbox(thumb.photos[0])}>
                          {#if thumb?.photos?.length}
                            <img src={thumb.photos[0]} alt="" loading="lazy" decoding="async" />
                          {:else if thumbLoading}
                            <span class="thumb-glyph thumb-loading-glyph" aria-hidden="true">⋯</span>
                          {:else}
                            <span class="thumb-glyph">{slotIcon[it.slot] ?? '•'}</span>
                          {/if}
                        </button>
                        <span class="ic">{slotIcon[it.slot] ?? '•'}</span>
                        <span class="body">
                          <span class="name-row">
                            <span class="name">{np.primary}</span>
                            {#if np.local}<span class="local-name">{np.local}</span>{/if}
                            {#if np.badge}<span class="name-src-badge" class:verified={np.badge !== 'romanized'} class:romanized={np.badge === 'romanized'}>{np.badge}</span>{/if}
                            {#if np.unreadable}<span class="name-src-badge unreadable" title="No English name available">shown in original script — no English name available</span>{/if}
                          </span>
                          <span class="chips">
                            {#if it.category}<span class="chip cat">{it.category}</span>{/if}
                            {#if it.exposure}<span class="chip {it.exposure}">{it.exposure === 'indoor' ? '🏠 indoor' : it.exposure === 'outdoor' ? '🌤 outdoor' : 'indoor/outdoor'}</span>{/if}
                            {#if it.cuisine}{@const cd = cuisineDisplay(it.cuisine)}{#if cd}<span class="chip cuisine" title={cd.truncated ? cd.full : undefined}>{cd.text}</span>{/if}{/if}
                          </span>
                        </span>
                        <!-- #126: locate button — sets mapFocus store → Map.svelte flyTo.
                             Finding #3 fix: disabled (not a dead-looking-enabled no-op) when
                             this item has no coordinates (curated seed entries can carry
                             null lat/lon) — honest degradation instead of a silent click. -->
                        <button class="locate-btn" data-testid="locate-{li}-{di}-{ii}"
                          disabled={!hasCoords}
                          aria-label={hasCoords ? `Show ${it.name} on map` : `Map location not available for ${it.name}`}
                          title={hasCoords ? 'Show on map' : 'Map location not available'}
                          on:click|stopPropagation={() => locateItem(it, day, dp.city)}>📍</button>
                        <!-- #37/#212: per-attraction/per-meal CONSTRUCTED reference/handoff link
                             (society/utils/booking_links.py attraction_link()/restaurant_link()).
                             Compact icon-only affordance (mirrors #211's RightRail.svelte "↗"
                             glyph) — a full label+button per item would clutter this dense
                             timeline (up to ~70 links/trip in the densest fixture). Deliberately
                             muted/borderless like .locate-btn, NOT the colored/bordered .lp-book
                             "Book →" treatment below, which implies a real live/actionable price;
                             these are always-constructed reference links, never a confirmed
                             booking. title/aria-label carry the link's own honesty-caveat label
                             text (e.g. "Official site — … (not a confirmed booking)") since there
                             is no room for it at this density. Only renders when a genuinely safe
                             URL is present — no fabrication for items without a link. -->
                        {#if it.link?.booking_url}
                          {@const linkHref = safeHref(it.link.booking_url)}
                          {#if linkHref}
                            <a class="poi-link-btn" data-testid="poi-link-{li}-{di}-{ii}"
                              href={linkHref} target="_blank" rel="noopener noreferrer"
                              title={it.link.label || 'Reference link'}
                              aria-label={it.link.label || `Reference link for ${it.name}`}
                              on:click|stopPropagation>↗</a>
                          {/if}
                        {/if}
                        {#if it.kind === 'activity'}
                          <span class="fee" class:free={feeLabel(it.fee) === 'Free'}>{feeLabel(it.fee)}</span>
                        {/if}
                        {#if editable && it.kind === 'activity'}
                          <button class="remove-btn" data-testid="remove-{li}-{di}-{it.attrIndex}"
                            aria-label="Remove {it.name}"
                            aria-busy={busy === `rm:${it.name}`}
                            on:click={() => removePlace(li, di, it.attrIndex, it.name)}>×</button>
                        {/if}
                        {#if editable && it.kind === 'meal'}
                          <button class="meal-edit-btn" data-testid="meal-edit-{li}-{di}-{ii}"
                            title="Swap this restaurant"
                            aria-label="Swap {it.slot} restaurant"
                            class:active={mealPanelOpen?.li === li && mealPanelOpen?.di === di && mealPanelOpen?.slot === it.slot}
                            on:click|stopPropagation={() => openMealPanel(it.slot, li, di)}>✎</button>
                        {/if}
                      </div>
                      <!-- Inline meal swap panel — appears below the meal item when ✎ clicked -->
                      {#if editable && it.kind === 'meal' && mealPanelOpen?.li === li && mealPanelOpen?.di === di && mealPanelOpen?.slot === it.slot}
                        {@const pool = getMealPool(day, it.slot)}
                        <div class="meal-swap-panel" data-testid="meal-swap-panel-{li}-{di}-{it.slot}">
                          {#if pool.length}
                            <span class="msp-label">Swap {it.slot} with:</span>
                            {#each pool as alt}
                              {@const anp = namePresentation(alt)}
                              <button class="msp-chip" data-testid="msp-chip-{alt.name}"
                                on:click={() => {
                                  runOps([{ op: 'swap_meal', leg_index: li, day_index: di, slot: it.slot, restaurant_name: alt.name ?? '' }],
                                         `swap_meal:${it.slot}:${alt.name}`);
                                  mealPanelOpen = null;
                                }}>
                                <span class="msp-name-row">
                                  <span class="msp-name">{anp.primary}</span>
                                  {#if anp.local}<span class="local-name">{anp.local}</span>{/if}
                                  {#if anp.badge}<span class="name-src-badge" class:verified={anp.badge !== 'romanized'} class:romanized={anp.badge === 'romanized'}>{anp.badge}</span>{/if}
                                  {#if anp.unreadable}<span class="name-src-badge unreadable" title="No English name available">shown in original script — no English name available</span>{/if}
                                </span>
                                {#if alt.cuisine}{@const acd = cuisineDisplay(alt.cuisine)}{#if acd} · <span class="msp-cuisine" title={acd.truncated ? acd.full : undefined}>{acd.text}</span>{/if}{/if}
                              </button>
                            {/each}
                          {:else}
                            <span class="msp-none">No alternatives on file — ask the assistant to suggest one</span>
                          {/if}
                          <button class="msp-close" aria-label="Close swap panel"
                            on:click={() => { mealPanelOpen = null; }}>✕</button>
                        </div>
                      {/if}
                      <!-- Inline transport gap chip AFTER each item (UI increment #3).
                           Pure presentation: server-computed values only; no client math.
                           null gap = honest suppression (either endpoint lacked coords). -->
                      {@const gap = day.item_transport_gaps?.[ii]}
                      {#if gap}
                        <div class="transport-gap" data-testid="transport-gap-{ii}">
                          <div class="transport-chip mode-{gap.mode}"
                               class:recomputing={!!busy}
                               data-testid="transport-chip-{ii}">
                            <span class="chip-icon">{intraIconFor(gap.mode)}</span>
                            <span class="chip-dur">{fmtMinutes(gap.minutes)}</span>
                            <span class="chip-sep">·</span>
                            <span class="chip-mode">{gap.mode}</span>
                            <span class="chip-est">est.</span>
                          </div>
                        </div>
                      {/if}
                    {/each}
                    {#if editable}
                      <!-- Drop zone at end of day -->
                      <div class="drop-zone drop-end" data-testid="drop-end-{li}-{di}"
                        role="button" tabindex="-1"
                        on:dragover={onDragOver}
                        on:dragleave={onDragLeave}
                        on:drop={(e) => onDrop(e, li, di, (day.attractions ?? []).length)}>
                      </div>
                    {/if}
                  </div>
                {:else}
                  <p class="empty">No activities catalogued for this day yet.</p>
                  {#if editable}
                    <div class="drop-zone drop-empty" data-testid="drop-empty-{li}-{di}"
                      role="button" tabindex="-1"
                      on:dragover={onDragOver}
                      on:dragleave={onDragLeave}
                      on:drop={(e) => onDrop(e, li, di, 0)}>
                      Drop an activity here
                    </div>
                  {/if}
                {/if}

                <!-- #121: per-day suggestion chips (editable mode, from unscheduled pool) -->
                {#if editable && (dp.unscheduled_attractions ?? []).length > 0}
                  <div class="day-sugs" data-testid="day-sugs-{li}-{di}">
                    <span class="day-sugs-label">Click or drag to add →</span>
                    {#each (dp.unscheduled_attractions ?? []).slice(0, 3) as ua}
                      {@const uaRef = attractionRef(ua)}
                      {@const unp = namePresentation(ua)}
                      <div class="day-sug-chip"
                        role="button" tabindex="0"
                        draggable="true"
                        title="Click to add to this day, or drag onto an activity to swap"
                        data-testid="sug-chip-{li}-{di}-{uaRef}"
                        on:dragstart={(e) => onDragStartSuggestion(e, li, uaRef)}
                        on:click={() => addFromUnscheduled(li, di, uaRef, (day.attractions?.length ?? 0))}
                        on:keypress={(e) => e.key === 'Enter' && addFromUnscheduled(li, di, uaRef, (day.attractions?.length ?? 0))}>
                        <span class="sug-name-row">
                          <span class="sug-name">{unp.primary}</span>
                          {#if unp.local}<span class="local-name">{unp.local}</span>{/if}
                          {#if unp.badge}<span class="name-src-badge" class:verified={unp.badge !== 'romanized'} class:romanized={unp.badge === 'romanized'}>{unp.badge}</span>{/if}
                          {#if unp.unreadable}<span class="name-src-badge unreadable" title="No English name available">shown in original script — no English name available</span>{/if}
                        </span>
                        {#if ua.category}<span class="sug-cat">{ua.category.split('=').pop()}</span>{/if}
                      </div>
                    {/each}
                  </div>
                {/if}

                <!-- Day-end hop list (B4 fix): this used to render EVERY hop for the day
                     (hotel→act1→act2→…→hotel), which fully duplicated the per-item
                     item_transport_gaps chips above for every POI-to-POI pair — same
                     underlying haversine/mode data shown twice ("kept N copies of the
                     same data in sync for no reason", same class of bug as the
                     Suggestions/unscheduled-pool removal below). item_transport_gaps is
                     POI-to-POI only (no hotel endpoint — see intracity_transport.py's
                     _timeline_for_gaps), so it never covers the hotel⇄first-activity /
                     last-activity⇄hotel commute legs; those aren't shown anywhere else,
                     so we keep just those here instead of dropping the block entirely.
                     B3 fix: hop.label is already a fully-formatted string from the
                     server (_make_hop: "~18 min, tram, est."), which already embeds
                     minutes/mode — only synthesize + append them from the raw fields
                     when label is absent, so we never show the same duration twice.
                     Endpoint-disambiguation fix: the hotel has no real coordinate, so
                     every hotel-endpoint hop (morning hotel→first-attraction, evening
                     last-attraction→hotel) proxies it with the city centroid. When both
                     hops land in the same distance/mode bucket, hop.label comes back
                     byte-identical for two genuinely different journeys. hop.from/
                     hop.to are always present and differ by direction even when the
                     label doesn't, so we prefix every hop (labelled or not) with them —
                     additive disambiguation, the label content itself is never altered. -->
                {#each (day.intracity_hops ?? []).filter((h) => h.from_kind === 'hotel' || h.to_kind === 'hotel') as hop}
                  {@const hasLabel = !!hop.label}
                  <!-- #222 fix: hop.from/hop.to are raw strings (often hotel_title,
                       which has no name_en in the schema — same data gap as the
                       #168 leg-header hotel-name block above) that used to render
                       verbatim with NO name-tier handling at all — the one consumer
                       of hotel_title that #168 never wired. Routed through the SAME
                       namePresentation() pipeline (synthetic { name } object, exactly
                       like the leg-header hnp above): local naturally stays null
                       (nothing to show as a companion — there's only one string) and
                       `unreadable` fires honestly whenever the endpoint name is
                       non-Latin script, reusing the identical badge markup/classes. -->
                  {@const hnpFrom = namePresentation({ name: hop.from })}
                  {@const hnpTo = namePresentation({ name: hop.to })}
                  {@const fromIsHotel = hop.from_kind === 'hotel'}
                  {@const toIsHotel = hop.to_kind === 'hotel'}
                  <!-- dailies-review fix (issue #4): the old single-line render crammed
                       an unlabeled endpoint name together with an inline "shown in
                       original script" caveat, so on a hotel-endpoint hop the caveat
                       had no stated referent (which side is it about?) — and if BOTH
                       endpoints were unreadable, both caveats ran together in the same
                       sentence. Name the hotel side explicitly ("Hotel:") in the main
                       line, and move each caveat to its own line below, each stating
                       which endpoint it belongs to. -->
                  <div class="hop">
                    <div class="hop-line">🚇 {#if fromIsHotel}Hotel: {/if}{hnpFrom.primary} → {#if toIsHotel}Hotel: {/if}{hnpTo.primary}{#if hasLabel}: {hop.label}{:else}{#if hop.minutes} · ~{hop.minutes} min{/if}{#if hop.mode} · {hop.mode}{/if}{/if}</div>
                    {#if hnpFrom.unreadable}<div class="hop-caveat" data-testid="hop-caveat-from">{fromIsHotel ? 'Hotel name' : 'Destination name'}: <span class="name-src-badge unreadable" title="No English name available">shown in original script — no English name available</span></div>{/if}
                    {#if hnpTo.unreadable}<div class="hop-caveat" data-testid="hop-caveat-to">{toIsHotel ? 'Hotel name' : 'Destination name'}: <span class="name-src-badge unreadable" title="No English name available">shown in original script — no English name available</span></div>{/if}
                  </div>
                {/each}
              </div>
            {/each}
          </div>

          {#if usesTabs}
            <!-- draft4 State 1: lightweight "you are here" carousel dots, mobile/tablet
                 only (hidden ≥768px via CSS — the tab strip itself is the equivalent
                 affordance on desktop). -->
            <div class="swipe-dots" data-testid="swipe-dots-{li}">
              {#each dp.days ?? [] as _day, di}
                <span class="swipe-dot" class:active={activeDi === di}></span>
              {/each}
            </div>
          {/if}

          <!-- Per-leg "Suggestions / unscheduled" pool removed: fully redundant with
               the per-day inline chips above and RightRail's Suggested tab (now
               scrollable/swipeable/draggable) -- kept 3 copies of the same data
               in sync for no reason. -->
        </div>
      {/if}
    </div>

    {#if li < plans.length - 1}
      {@const nextDp = plans[li + 1]}
      {@const edge = dp.leg_id ? edgeByFromLeg.get(dp.leg_id) : undefined}
      {@const nextLeg = legById.get(nextDp.leg_id ?? '')}
      {@const fallback = nextLeg?.inbound_transport}
      {#if edge}
        <!-- Rich inter-city transport connector from server's transport_edges -->
        <div class="intercity-connector" data-testid="intercity-connector-{li}">
          <div class="ic-line"></div>
          <div class="ic-pill" data-testid="intercity-pill-{li}">
            <span class="ic-icon" data-testid="intercity-mode-{li}">{iconForMode(edge.mode)}</span>
            <span class="ic-route">{edge.from_city ?? dp.city} → {edge.to_city ?? nextDp.city}</span>
            <span class="ic-sep">·</span>
            <span class="ic-mode-lbl">{edge.mode}</span>
            {#if fmtMinutes(edge.transfer_minutes)}
              <span class="ic-sep">·</span>
              <span class="ic-dur" data-testid="intercity-duration-{li}">{fmtMinutes(edge.transfer_minutes)}</span>
            {/if}
            {#if edge.rail_tier === 'hsr'}
              <span class="ic-hsr" data-testid="intercity-hsr-{li}">HSR</span>
            {/if}
          </div>
          {#if !edge.feasible && edge.reason}
            <div class="ic-infeasible" data-testid="intercity-infeasible-{li}">⚠ {edge.reason}</div>
          {/if}
          {#if transport_pricing?.basis_label}
            <div class="ic-caveat" data-testid="intercity-caveat-{li}">{transport_pricing.basis_label}</div>
          {/if}
        </div>
      {:else if fallback}
        <!-- Fallback: leg's inbound_transport ready string -->
        <div class="ribbon" data-testid="intercity-connector-{li}">🚌 {fallback}</div>
      {:else}
        <!-- Minimal onward cue when no transport data available -->
        <div class="ribbon onward" data-testid="intercity-connector-{li}">🧭 Onward to {nextDp.city ?? 'next city'}</div>
      {/if}
    {/if}
  {/each}
</div>

<style>
  .itin { display: flex; flex-direction: column; gap: 12px; }

  /* ── leg header (Part A: accordion chevron + Part B: leg-hero thumbnail) ── */
  .leg-block { display: flex; flex-direction: column; gap: 0; }
  .leg-h { display: flex; align-items: flex-start; gap: 12px; margin: 6px 2px 0; }
  .leg-thumb {
    width: 60px; height: 60px; border-radius: 10px; flex-shrink: 0; padding: 0;
    border: 1px solid #ece2d5; background: #f4ede3; cursor: pointer; overflow: hidden;
    display: block;
  }
  .leg-thumb img { display: block; width: 100%; height: 100%; object-fit: cover; }
  .leg-thumb.icon-tile {
    display: flex; align-items: center; justify-content: center; font-size: 24px;
    background: #efe8dd; border: 1px dashed #ddd3c4; cursor: default;
  }
  .leg-thumb:disabled { cursor: default; }
  .thumb-glyph { line-height: 1; }
  .thumb-loading-glyph { color: #a89e94; animation: pulse-gap 1.1s ease-in-out infinite; }
  .leg-h-body { display: flex; flex-direction: column; gap: 3px; flex: 1; min-width: 0; }
  /* toggle + reflow action share one row; action pinned right, off the content column */
  .leg-h-top { display: flex; align-items: flex-start; gap: 8px; }
  .leg-h-toggle {
    flex: 1; min-width: 0;
    display: flex; align-items: center; gap: 9px; flex-wrap: wrap;
    background: none; border: none; padding: 0; cursor: pointer; font: inherit;
    text-align: left; color: inherit;
  }
  .leg-h-top .reflow-btn { flex-shrink: 0; align-self: flex-start; }
  .dot { width: 9px; height: 9px; border-radius: 50%; background: #d9774a; flex-shrink: 0; }
  .city { font-weight: 700; font-size: 16px; line-height: 1.25; }
  .meta { color: #7c7468; font-size: 12.5px; }
  .leg-chevron { font-size: 11px; color: #a89e94; transition: transform .15s; }
  .leg-chevron.open { transform: rotate(90deg); }
  .leg-collapsed-summary { font-size: 12.5px; color: #7c7468; padding: 4px 2px 0 72px; font-style: italic; }

  .ribbon { font-size: 12.5px; color: #7c7468; background: #f6f0e8; border: 1px dashed #ece2d5;
    border-radius: 10px; padding: 7px 11px; }
  .ribbon.onward { background: #eef0f6; border-color: #cfd6e8; color: #5a73b0; }
  /* hotel name DEMOTED from bordered pill → quiet caption tied to the thumbnail.
     Loses the border/fill so the city clearly leads; truncates instead of wrapping. */
  .hotel-name {
    display: flex; align-items: baseline; gap: 6px; flex-wrap: wrap;
    font-size: 12.5px; color: #6f665b; font-weight: 500;
    background: none; border: none; padding: 0; margin-top: 1px; align-self: flex-start;
    max-width: 100%;
  }
  /* the hotel-name string itself still truncates on one line; badges/companion
     text sit alongside it and are free to wrap onto their own line if needed. */
  .hotel-name-text { white-space: nowrap; overflow: hidden; text-overflow: ellipsis; min-width: 0; max-width: 100%; }
  /* #206: per-leg cost total — quiet, same weight class as .hotel-name (not a
     bordered pill); .leg-cost-basis reuses .ic-caveat for the honesty label. */
  .leg-cost { display: flex; align-items: baseline; gap: 6px; flex-wrap: wrap; margin-top: 1px; }
  .leg-cost-amount { font-size: 12.5px; font-weight: 600; color: #6f665b; }
  .leg-cost-basis { padding-left: 0; }
  /* unverified: the ONE colored honesty chip — amber with a left accent bar so it
     reads as a chip rather than a full uniform pill competing with hotel-name */
  .unverified {
    font-size: 12px; color: #b07d1f; background: #fbf0db;
    border: 1px solid #ecd9ad; border-left: 3px solid #d9a441;
    border-radius: 6px; padding: 4px 9px; font-weight: 600;
    align-self: flex-start; margin-top: 2px;
  }
  /* dayplan-notes: honest per-leg disclosures from the day-planner (#190) — dim/italic
     like .ic-caveat, NOT the amber .unverified warning treatment. These are informational
     ("fell back to the best available venue, not fabricated"), not alarms. */
  .dayplan-notes {
    list-style: none; margin: 3px 0 0; padding: 0;
    display: flex; flex-direction: column; gap: 1px;
    align-self: flex-start; max-width: 100%;
  }
  .dayplan-notes li {
    font-size: 11px; color: #a89e94; font-style: italic;
    word-break: break-word;
  }
  .leg-body { display: flex; flex-direction: column; gap: 8px; margin-top: 6px; }
  /* live best-price overlay (PROD only; absent in UAT) */
  .live-price { display: flex; align-items: center; gap: 8px; flex-wrap: wrap;
    font-size: 13px; background: #eef6f0; border: 1px solid #bfe0c8; border-radius: 8px;
    padding: 5px 10px; }
  .lp-amount { font-weight: 700; color: #2f6b46; font-variant-numeric: tabular-nums; }
  .lp-badge { font-size: 10px; font-weight: 700; text-transform: uppercase; letter-spacing: .5px;
    color: #fff; background: #3a9b63; border-radius: 5px; padding: 1px 6px; }
  .lp-source { font-size: 11.5px; color: #5a7a66; }
  .lp-book { margin-left: auto; font-size: 12.5px; font-weight: 600; color: #2f6b46;
    text-decoration: none; border: 1px solid #bfe0c8; border-radius: 6px; padding: 2px 9px;
    background: #fff; }
  .lp-book:hover { border-color: #3a9b63; background: #f3fbf5; }

  /* ── Part A: per-leg day-tab strip (draft3 Decision C, draft4 drag states) ── */
  .day-tabs {
    display: flex; gap: 4px; overflow-x: auto; padding: 4px 2px 8px;
    border-bottom: 1px solid #ece2d5; scrollbar-width: thin;
  }
  .day-tab {
    flex-shrink: 0; font-size: 12.5px; font-weight: 700; padding: 6px 12px;
    border-radius: 8px 8px 0 0; border: 1px solid transparent; color: #7c7468;
    cursor: pointer; user-select: none; transition: background .12s, border-color .12s;
    display: flex; align-items: center; gap: 3px;
  }
  .day-tab:hover { background: #fff4ee; color: #d9774a; }
  .day-tab.active { color: #d9774a; background: #fff4ee; border-color: #f0c4a0; }
  /* dwell-switch (activity/suggestion drag hovering a tab) — distinct SOLID fill,
     deliberately different from .active (bordered) and .swap-target (dashed
     purple) so a mid-drag user can tell the three states apart at a glance
     (draft4 State 2 annotation). */
  .day-tab.dragover-pending { background: #d9774a; color: #fff; }
  /* day-reorder drag source */
  .day-tab.dragging { opacity: .45; border: 1.5px dashed #d9774a; background: #fff4ee; }
  /* day-reorder hover target */
  .day-tab.swap-target { background: #eeeafb; border: 1.5px dashed #7a6fc9; color: #5a4f9b; font-weight: 800; }
  .day-tab.swap-target::after { content: " ⇄"; }
  /* boundary day (first/last of leg) — not draggable, honest lock affordance up front */
  .day-tab.boundary { color: #a89e94; background: #f3f0ea; cursor: not-allowed; }
  .day-tab .lock { font-size: 10px; }

  .day-panel-group { display: flex; flex-direction: column; gap: 12px; }
  .day.tab-hidden { display: none; } /* mounted, never unmounted — keeps drop zones alive */

  /* draft4 State 1: swipe-dot carousel indicator, mobile/tablet only */
  .swipe-dots { display: none; justify-content: center; gap: 4px; margin-top: 2px; }
  .swipe-dot { width: 5px; height: 5px; border-radius: 50%; background: #ddd5c8; }
  .swipe-dot.active { background: #d9774a; width: 14px; border-radius: 3px; }
  @media (max-width: 768px) {
    .swipe-dots { display: flex; }
  }

  .day { border: 1px solid #ece2d5; border-radius: 12px; padding: 10px 12px; }
  .day.wet { background: #fffaf4; border-color: #caa75b; }
  .day-top { display: flex; align-items: center; gap: 10px; padding-bottom: 6px; flex-wrap: wrap; }
  .day-n { font-weight: 700; }
  .wx { font-size: 12px; color: #c98a2b; background: #fbf0db; border-radius: 7px; padding: 2px 8px; font-weight: 600; }
  .tl { border-left: 2px solid #ece2d5; margin-left: 6px; padding-left: 13px;
    display: flex; flex-direction: column; gap: 7px; }
  .slot { font-size: 11px; text-transform: uppercase; letter-spacing: .6px; color: #caa75b;
    font-weight: 700; margin-top: 3px; }
  .item { display: flex; align-items: flex-start; gap: 9px; }
  .item.draggable { cursor: grab; }
  .item.draggable:active { cursor: grabbing; }
  .item.busy { opacity: 0.5; pointer-events: none; }
  .ic { width: 20px; text-align: center; flex: 0 0 auto; }
  .body { flex: 1; min-width: 0; }
  .name-row { display: flex; align-items: baseline; gap: 6px; flex-wrap: wrap; }
  .name { font-weight: 600; }
  /* ── Part A: local-name / romanization display tiers (draft1 State 3) ──── */
  .local-name { font-size: 11.5px; color: #7c7468; font-weight: 500; }
  .name-src-badge { font-size: 9px; font-weight: 700; text-transform: uppercase; letter-spacing: .3px;
    border-radius: 4px; padding: 1px 5px; vertical-align: middle; }
  .name-src-badge.verified { background: #e6f1e9; color: #5a8a63; }
  .name-src-badge.romanized { background: #fbf0db; color: #8a6a10; }
  /* #168: honest "unreadable primary" indicator — no name_en fallback exists at all,
     so the displayed name is genuinely non-Latin script with nothing else to show.
     Same visual family as the other name-src-badge tiers, own neutral-grey modifier
     (not verified-green, not romanized-amber — this isn't a verification tier). */
  .name-src-badge.unreadable {
    background: #f1eee7; color: #7c7468; text-transform: none; letter-spacing: normal;
    font-weight: 600; font-size: 10.5px; white-space: normal;
  }
  .chips { display: flex; flex-wrap: wrap; gap: 5px; margin-top: 2px; }
  .chip { font-size: 11.5px; border-radius: 6px; padding: 1px 7px; font-weight: 600; }
  .chip.cat { background: #f3eee4; color: #7c7468; }
  .chip.indoor { background: #eaeefb; color: #5a73b0; }
  .chip.outdoor { background: #e6f1e9; color: #5a8a63; }
  .chip.mixed { background: #f3eee4; color: #7c7468; }
  /* (B2) cuisine chips carry raw OSM tag text — token count is capped in JS
     (cuisineDisplay), but this is a CSS safety net for any single long token /
     unexpected server value: truncate to one line inside the chips row rather
     than overflowing the item container. Full text is on the title tooltip
     when truncated (JS-side) so info isn't silently lost. */
  .chip.cuisine {
    background: #f6ece1; color: #a86a3c;
    max-width: 100%; overflow: hidden; text-overflow: ellipsis;
    white-space: nowrap; display: inline-block; vertical-align: top;
  }
  .fee { margin-left: auto; font-variant-numeric: tabular-nums; color: #7c7468; font-size: 12.5px; }
  .fee.free { color: #5a8a63; }
  .hop { padding: 3px 0 0 8px; }
  .hop-line { font-size: 12px; color: #7c7468; }
  .hop-caveat { font-size: 11px; color: #9c8d78; padding-top: 2px; }
  .empty { color: #998a78; font-size: 13px; margin: 4px 0; }

  /* ── Part B: always-visible per-item thumbnail ──────────────────────────── */
  .item-thumb {
    width: 52px; height: 52px; border-radius: 8px; flex-shrink: 0; padding: 0;
    border: 1px solid #ece2d5; background: #f4ede3; cursor: pointer; overflow: hidden;
    display: block; margin-top: 1px;
  }
  .item-thumb img { display: block; width: 100%; height: 100%; object-fit: cover; }
  .item-thumb.icon-tile {
    display: flex; align-items: center; justify-content: center; font-size: 18px;
    background: #efe8dd; color: #a89e94; border: 1px dashed #ddd3c4; cursor: default;
  }
  .item-thumb:disabled { cursor: default; }
  @media (max-width: 768px) {
    .item-thumb, .leg-thumb { width: 44px; height: 44px; }
  }

  /* #126: locate button — shown for every timeline item, muted until hover.
     Finding #3 fix: :disabled (no coords) gets its OWN dimmer, non-interactive
     treatment so it doesn't look identical to a live, clickable button. */
  .locate-btn {
    background: none; border: none; cursor: pointer; font-size: 13px; padding: 0 2px;
    opacity: 0.35; transition: opacity .15s; flex-shrink: 0; line-height: 1;
  }
  .locate-btn:hover { opacity: 1; }
  .locate-btn:disabled { opacity: 0.15; cursor: default; }
  .locate-btn:disabled:hover { opacity: 0.15; }
  /* #37/#212: compact icon-only reference/handoff link, per attraction/meal item.
     Muted + borderless (same weight as .locate-btn), deliberately NOT the colored/
     bordered .lp-book "Book →" treatment below — these are CONSTRUCTED reference
     links, never a live/confirmed price. text-decoration:none by default (this is
     content, not prose) with an underline-on-hover affordance instead. */
  .poi-link-btn {
    color: #8a7a5c; font-size: 12px; padding: 0 2px; flex-shrink: 0; line-height: 1;
    opacity: 0.5; transition: opacity .15s; text-decoration: none; font-weight: 700;
  }
  .poi-link-btn:hover { opacity: 1; text-decoration: underline; }
  /* meal edit button — opens chat with a suggested change message */
  /* meal swap panel */
  .meal-swap-panel {
    display: flex; flex-wrap: wrap; align-items: center; gap: 6px;
    margin: 3px 0 6px 24px; padding: 8px 10px;
    background: #faf6f0; border: 1px solid #e5d9c8; border-radius: 8px;
  }
  .msp-label { font-size: 11px; color: #7c7468; font-weight: 600; white-space: nowrap; }
  .msp-chip {
    border: 1px solid #d4c9bb; background: #fff; border-radius: 6px;
    padding: 3px 10px; font-size: 12px; color: #5a4f44; cursor: pointer;
    transition: background .1s, border-color .1s;
    /* (G2, #181) same overflow guard as .chip.cuisine (B2) / .day-sug-chip (G1) —
       alt.name is a raw served name that can be arbitrarily long/unbroken. */
    display: inline-flex; align-items: baseline; gap: 4px; max-width: 220px;
  }
  .msp-chip:hover { background: #fde8d8; border-color: #d9774a; color: #c05a28; }
  /* (G2) wraps the primary/local-name/badge trio so only the primary name (the
     one most likely to be long) truncates; badges stay whole and visible. */
  .msp-name-row { display: inline-flex; align-items: baseline; gap: 4px; min-width: 0; overflow: hidden; }
  .msp-name { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; min-width: 0; }
  .msp-cuisine { color: #9e9288; font-size: 11px; flex-shrink: 0; }
  .msp-none { font-size: 11px; color: #b0a899; font-style: italic; }
  .msp-close {
    margin-left: auto; border: none; background: none;
    color: #b0a899; cursor: pointer; font-size: 14px; padding: 0 4px; line-height: 1;
  }
  .msp-close:hover { color: #7c7468; }
  .meal-edit-btn.active { background: #fde8d8; border-color: #d9774a; color: #d9774a; }

  .meal-edit-btn {
    background: none; border: 1px solid #ece2d5; border-radius: 6px; cursor: pointer;
    font-size: 12px; padding: 1px 5px; color: #7c7468; flex-shrink: 0; line-height: 1;
    transition: border-color .15s, color .15s;
  }
  .meal-edit-btn:hover { border-color: #d9774a; color: #d9774a; }

  /* #121: per-day suggestion chips */
  .day-sugs {
    display: flex; flex-wrap: wrap; gap: 6px; align-items: center;
    margin-top: 10px; padding-top: 9px; border-top: 1px dashed #ece2d5;
  }
  .day-sugs-label {
    font-size: 11px; text-transform: uppercase; letter-spacing: .5px;
    color: #a89e94; font-weight: 700; flex-shrink: 0;
  }
  .day-sug-chip {
    display: inline-flex; align-items: center; gap: 5px;
    font-size: 12.5px; font-weight: 600; color: #5a4f44;
    background: #f1ece4; border: 1px dashed #d4c9bb; border-radius: 8px;
    padding: 3px 9px; cursor: pointer; user-select: none;
    transition: background .1s, border-color .1s, transform .1s;
    /* (G1, #181) a long unbroken served name (no CJK/Thai/etc. wrap points) must
       not overflow the day card — same guard class as .chip.cuisine (B2). */
    max-width: 240px;
  }
  .day-sug-chip:hover { background: #eae1d5; border-color: #d9774a; color: #d9774a; transform: translateY(-1px); }
  .day-sug-chip:active { cursor: grabbing; transform: translateY(0); }
  /* (G1) wraps the primary/local-name/badge trio so only the primary name (the
     one most likely to be long) truncates; badges stay whole and visible. */
  .sug-name-row { display: inline-flex; align-items: baseline; gap: 4px; min-width: 0; overflow: hidden; }
  .sug-name { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; min-width: 0; }
  .sug-cat {
    font-size: 10.5px; font-weight: 600; color: #a89e94;
    background: #e8e2d8; border-radius: 4px; padding: 0 5px;
    flex-shrink: 0; /* short closed-set OSM category tail (e.g. "attraction") — already safe */
  }

  /* #121: item swap-drop highlight (drag-suggestion over an existing activity) */
  :global(.item.swap-target) {
    background: #fff4ee !important; border-radius: 8px;
    outline: 2px dashed #d9774a !important;
  }

  /* ── inter-city transport connector ────────────────────────────────────── */
  .intercity-connector {
    display: flex; flex-direction: column; align-items: flex-start; gap: 4px;
    padding: 4px 0;
  }
  .ic-line {
    width: 2px; height: 18px; background: #ddd5c8;
    border-radius: 1px; margin-left: 22px; flex-shrink: 0;
  }
  .ic-pill {
    display: inline-flex; align-items: center; flex-wrap: wrap; gap: 5px;
    padding: 5px 12px; background: #f6f0e8;
    border: 1px solid #e2d8cb; border-radius: 20px;
    font-size: 12.5px; color: #5a4f44; font-weight: 500;
  }
  .ic-icon { font-size: 14px; flex-shrink: 0; }
  .ic-route { font-weight: 700; color: #3a322b; }
  .ic-sep { color: #bfb5a8; flex-shrink: 0; }
  .ic-mode-lbl { color: #7c7468; }
  .ic-dur { font-variant-numeric: tabular-nums; color: #5a4f44; font-weight: 600; }
  .ic-hsr {
    font-size: 10px; font-weight: 800; text-transform: uppercase; letter-spacing: .5px;
    color: #fff; background: #3a7fd4; border-radius: 5px; padding: 1px 6px;
    flex-shrink: 0;
  }
  .ic-infeasible {
    font-size: 12px; color: #b05050; background: #fff5f5;
    border: 1px solid #f0c4c4; border-radius: 8px; padding: 4px 10px;
    font-weight: 600;
  }
  .ic-caveat {
    font-size: 11px; color: #a89e94; font-style: italic;
    padding-left: 4px; max-width: 100%; word-break: break-word;
  }
  /* Responsive: pill wraps cleanly on narrow viewports */
  @media (max-width: 480px) {
    .ic-pill { padding: 5px 10px; gap: 4px; font-size: 12px; }
    .ic-route { font-size: 12px; }
  }

  /* edit-lane controls */
  .reflow-btn, .variant-btn, .remove-btn {
    border: 1px solid #ece2d5; border-radius: 7px; background: #faf6f0; cursor: pointer;
    font-family: inherit; font-size: 12px; color: #7c7468; padding: 2px 8px;
    transition: border-color .15s, background .15s;
  }
  .reflow-btn:hover, .variant-btn:hover { border-color: #d9774a; color: #d9774a; }
  .remove-btn { color: #b05050; font-size: 14px; font-weight: 700; padding: 0 6px;
    line-height: 1.4; border-color: #f0d8d8; background: #fff5f5; }
  .remove-btn:hover { background: #f0d8d8; }
  .reflow-btn.busy, .variant-btn.busy, .remove-btn.busy { opacity: 0.5; pointer-events: none; }

  /* drop zones
     Bug fix: the "drop BEFORE this item" hit target used to be a bare 8px strip —
     fine when items were single-line text rows, but once Part B added the
     always-visible 52-56px thumbnail + multi-line body, that 8px sliver became a
     tiny target sandwiched between much taller rows. Real (imprecise) mouse/
     trackpad drags routinely missed it, fell through onto the (now large) `.item`
     row instead, and — since a suggestion dropped ON an item only swaps when the
     item itself is the target — most misses simply never registered a valid
     drop-zone at all and the drag continued down to the big, easy-to-hit
     `drop-end` zone at the bottom of the day. Net effect: a card dropped ANYWHERE
     mid-list would appear to "always land at the bottom".
     Fix: grow the ACTUAL interactive box (not just the hover-expanded visual
     line) from 8px to 24px — matching `drop-end`'s already-comfortable target
     size. It stays invisible-at-rest in the sense that no border/background is
     painted (border stays transparent, no background) — but it is NOT a
     zero-footprint overlay: `.drop-zone` is a normal space-occupying child of
     `.tl`'s `gap: 7px` flex column, so this DOES add ~16px of real blank space
     before every activity item in edit mode (measured: item-to-item gap with a
     transport chip between them went from 79px to 95px). Audited and judged an
     acceptable tradeoff — the extra breathing room reads as a slightly airier
     timeline, not broken/sparse — versus the alternative of an absolutely-
     positioned hit-area overlay, which would risk stealing dragover/drop/click
     events from the neighboring `.item` row's own handlers (thumb button,
     locate button, remove button) for a demo-critical surface with limited time
     to verify that risk away. Only `.drop-target` (live dragover hover) paints
     the dashed line/background, same as before. */
  .drop-zone { height: 24px; border-radius: 4px; border: 2px dashed transparent;
    transition: border-color .1s, background .1s; }
  .drop-zone.drop-end { height: 24px; border: 2px dashed #ece2d5; color: #ccc;
    font-size: 12px; display: flex; align-items: center; justify-content: center; }
  .drop-zone.drop-empty { height: 36px; border: 2px dashed #ece2d5; color: #bbb;
    font-size: 12px; border-radius: 8px; display: flex; align-items: center;
    justify-content: center; margin-top: 4px; }
  :global(.drop-zone.drop-target) { border-color: #d9774a !important;
    background: #fff4ee !important; }

  /* ── inline transport gap chips (UI increment #3) ──────────────────────────
     Reuses the warm palette of .intercity-connector / .ic-pill.
     PURE PRESENTATION: chips render server values; no client math.
     null gap → chip suppressed entirely (honest suppression via {#if}).
  ─────────────────────────────────────────────────────────────────────────── */
  .transport-gap {
    display: flex;
    align-items: center;
    /* No vertical padding here — .tl's flex `gap: 7px` already spaces every
       child equally; adding padding on top of that gap was double-counting
       and made the gap around a transport chip visibly taller than the gap
       between two plain items. */
  }
  .transport-chip {
    display: inline-flex;
    align-items: center;
    gap: 5px;
    padding: 3px 10px 3px 8px;
    background: #f9f5ef;
    border: 1px solid #e6ddd0;
    border-radius: 14px;
    font-size: 12px;
    color: #5a4f44;
    font-weight: 500;
    white-space: nowrap;
    transition: background 0.15s, border-color 0.15s;
    cursor: default;
  }
  .transport-chip:hover { background: #f3ece2; border-color: #d9774a; }
  .transport-chip :global(.chip-icon) { font-size: 13px; flex-shrink: 0; }
  .transport-chip :global(.chip-dur) {
    font-variant-numeric: tabular-nums;
    font-weight: 700;
    color: #3a322b;
  }
  .transport-chip :global(.chip-sep) { color: #c4b9ae; margin: 0 1px; }
  .transport-chip :global(.chip-mode) { color: #7c7468; font-size: 11.5px; }
  .transport-chip :global(.chip-est) {
    font-size: 10.5px;
    color: #a89e94;
    font-style: italic;
    margin-left: 2px;
  }
  /* Mode-specific tints */
  .transport-chip.mode-walk { background: #f0f7f0; border-color: #c6dfc6; }
  .transport-chip.mode-walk :global(.chip-dur) { color: #3a6b3a; }
  .transport-chip.mode-metro { background: #eef2fb; border-color: #c4cef0; }
  .transport-chip.mode-metro :global(.chip-dur) { color: #3a4f9b; }
  .transport-chip.mode-taxi { background: #fdf6e6; border-color: #e8d48e; }
  .transport-chip.mode-taxi :global(.chip-dur) { color: #8a6a10; }
  /* Recomputing state: shown while a /replan is in flight (busy !== null).
     Chips pulse to signal "estimates may change" — never a client recompute. */
  .transport-chip.recomputing {
    background: #fff4ee;
    border-color: #f0c4a0;
    animation: pulse-gap 1s ease-in-out infinite;
  }
  .transport-chip.recomputing :global(.chip-dur),
  .transport-chip.recomputing :global(.chip-mode) { color: #d9774a; }
  @keyframes pulse-gap {
    0%, 100% { opacity: 1; }
    50%       { opacity: 0.55; }
  }

  /* flash bar */
  .flash { font-size: 13px; border-radius: 8px; padding: 7px 12px; font-weight: 600;
    border: 1px solid transparent; }
  .flash.ok { background: #e6f4ec; border-color: #a3d0b0; color: #3a7a52; }
  .flash.reject { background: #fef2f2; border-color: #f0c4c4; color: #b05050; }
  .flash.note { background: #fff8e6; border-color: #f0d98a; color: #8a6a10; }


  /* ── lightbox overlay (Part B — mirrors PlacePhotos.svelte's .lb-overlay) ── */
  .lb-overlay {
    position: fixed; inset: 0; z-index: 9999;
    background: rgba(0, 0, 0, 0.82);
    display: flex; align-items: center; justify-content: center;
    cursor: zoom-out;
  }
  .lb-img {
    max-width: min(92vw, 900px); max-height: 85vh;
    object-fit: contain; border-radius: 6px;
    box-shadow: 0 8px 40px rgba(0,0,0,0.6);
  }
  .lb-close {
    position: absolute; top: 16px; right: 20px;
    background: rgba(255,255,255,0.15); border: none; color: #fff;
    font-size: 20px; width: 36px; height: 36px; border-radius: 50%;
    cursor: pointer; display: flex; align-items: center; justify-content: center;
  }
  .lb-close:hover { background: rgba(255,255,255,0.28); }
</style>
