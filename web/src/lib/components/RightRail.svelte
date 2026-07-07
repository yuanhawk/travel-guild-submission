<script lang="ts">
  import { createEventDispatcher } from 'svelte';
  import type { NegotiateResult, Attraction, BookingLink } from '../api';
  import { riskSignalList, centsToUsd, fmtIndicative, replanTrip, isForbidden, forbiddenMessage, safeHref } from '../api';
  import {
    hazardLines, budgetRows, budgetPct, planPins, displayName, prettyCategory, attractionRef,
    namePresentation,
  } from '../itinerary';
  import Map from './Map.svelte';
  import Aftercare from './Aftercare.svelte';
  import { mapFocus } from '../mapStore';

  export let result: NegotiateResult;
  export let user_id: string | undefined = undefined;

  type Tab = 'suggested' | 'map' | 'budget' | 'safety' | 'aftercare';
  let tab: Tab = 'suggested';

  const dispatch = createEventDispatcher<{ replanned: NegotiateResult }>();

  let railEl: HTMLDivElement;

  // #126: auto-switch to map tab when a locate action is fired.
  // On mobile (grid collapses to 1 col at ≤1100px) the rail is below the fold —
  // scroll it into view so the user can see the map react.
  $: if ($mapFocus != null) {
    tab = 'map';
    if (typeof window !== 'undefined' && window.innerWidth <= 1100) {
      setTimeout(() => railEl?.scrollIntoView({ behavior: 'smooth', block: 'start' }), 80);
    }
  }

  // Show aftercare tab only when the trip is booked (charged or has booking_ref)
  $: isBooked = result?.payment_status === 'charged' || !!result?.booking_ref;

  $: legs = result.legs ?? [];
  $: plans = result.day_plans ?? [];
  $: risks = riskSignalList(result.risk_signals);
  // Adversarial review (2026-07-06): result.advisories[]
  // (orchestrator.py's D2 block) lifts every risk_signals.per_leg[].advisory[].detail
  // verbatim into the trip-level list "so a flag is never buried inside risk_signals" —
  // but this component then rendered BOTH the trip-level list (below) AND the per-leg
  // advisory detail (in the leg-risk loop), so every advisory paragraph doubled on
  // screen. Filter out any trip-level advisory whose text exactly matches a per-leg
  // advisory detail already shown elsewhere on this tab; compliance/health/day-planner
  // notes that AREN'T duplicated (they have no per-leg advisory counterpart) still show.
  $: perLegAdvisoryDetails = new Set(
    risks.flatMap((r) => (r.advisory ?? []).map((a) => a.detail).filter((d): d is string => !!d))
  );
  $: dedupedAdvisories = (result.advisories ?? []).filter((adv) => !perLegAdvisoryDetails.has(adv));
  $: pins = planPins(plans, legs);
  $: rows = budgetRows(result);
  $: usedCents = result.package_total_with_fees_cents ?? result.package_total_cents;
  $: pct = budgetPct(usedCents, result.total_budget_cents);
  $: emergencies = result.active_emergencies ?? [];

  // #211 — trip/leg-level booking_links (lodging/transport/visa/health/insurance).
  // Deliberately reads ONLY result.booking_links (the top-level block), never
  // legs[].booking_link — the latter is the SAME object duplicated in place by
  // the backend (society/utils/booking_links.py build_booking_links) onto each
  // leg; rendering both would show every lodging link twice. attractions[]/
  // restaurants[] (per-entity, high-cardinality) are out of scope — see #212.
  interface BookingLinkGroup { key: string; title: string; entries: BookingLink[]; }
  $: bookingLinkGroups = ((): BookingLinkGroup[] => {
    const bl = result.booking_links;
    if (!bl) return [];
    const groups: BookingLinkGroup[] = [
      { key: 'lodging', title: 'Lodging', entries: bl.lodging ?? [] },
      { key: 'transport', title: 'Transport', entries: bl.transport ?? [] },
      { key: 'visa', title: 'Visa / entry', entries: bl.visa ? [bl.visa] : [] },
      { key: 'health', title: 'Health', entries: bl.health ? [bl.health] : [] },
      { key: 'insurance', title: 'Insurance', entries: bl.insurance ? [bl.insurance] : [] },
    ];
    return groups.filter((g) => g.entries.length > 0);
  })();

  // Keep leg_index with each suggestion so + Add knows where to insert
  interface SugMeta { a: Attraction; legIdx: number; city: string; numDays: number; }
  // No cap: the list is now genuinely scrollable (desktop) / swipeable (mobile) via
  // .sug-list, so truncating to a fixed count would just hide real candidates.
  $: sugMeta = plans.flatMap((p, li) =>
    (p.unscheduled_attractions ?? [])
      .filter((a) => displayName(a))
      .map((a): SugMeta => ({ a, legIdx: li, city: p.city ?? '', numDays: p.days?.length ?? 1 }))
  );

  // Per-suggestion target day (0-based); default 0 (Day 1)
  let targetDay: Record<string, number> = {};

  const tierClass: Record<string, string> = { HIGH: 'high', MED: 'med', LOW: 'low' };

  // ── add suggestion from right rail ────────────────────────────────────────
  let addBusy: string | null = null;
  let addFlash: { ok: boolean; msg: string } | null = null;

  // Drag a card straight into the itinerary. Same wire shape Itinerary.svelte's own
  // per-day suggestion chips already use (onDragStartSuggestion / SuggestionPayload) --
  // Itinerary's drop handler reads `kind` off the parsed payload itself, not off any
  // RightRail-local state, so this cross-component drag needs no changes on that side.
  function onSugDragStart(e: DragEvent, s: SugMeta): void {
    e.dataTransfer?.setData('text/plain',
      JSON.stringify({ kind: 'suggestion', leg_index: s.legIdx, ua_ref: attractionRef(s.a) }));
    if (e.dataTransfer) e.dataTransfer.effectAllowed = 'copy';
  }

  async function addSuggestion(s: SugMeta): Promise<void> {
    const ik = result.idempotency_key;
    if (!ik || addBusy) return;
    const ref = attractionRef(s.a);
    const key = `${s.legIdx}:${ref}`;
    const di = targetDay[key] ?? 0;
    const endPos = (plans[s.legIdx]?.days?.[di]?.attractions?.length ?? 0);
    addBusy = key; addFlash = null;
    try {
      const r = await replanTrip(ik, [{
        op: 'add_place', leg_index: s.legIdx, day_index: di,
        position: endPos, attraction_ref: ref, from: 'unscheduled',
      }]);
      if (isForbidden(r)) {
        // IDOR fix: this session's session_token/owner_token don't match the trip's
        // actual owner — honest refusal, plan left untouched. #203: expired-session
        // vs cross-user-ownership get distinct copy.
        addFlash = { ok: false, msg: forbiddenMessage(r) };
      } else if (r.outcome === 'plan_ready' && (r.applied?.length ?? 0) > 0) {
        dispatch('replanned', { ...r.plan, idempotency_key: r.idempotency_key ?? r.plan?.idempotency_key });
        addFlash = { ok: true, msg: 'Added ✓' };
      } else {
        addFlash = { ok: false, msg: r.reason || "Couldn't add — try from the itinerary." };
      }
    } catch (e) {
      addFlash = { ok: false, msg: String(e) };
    } finally {
      addBusy = null;
      if (addFlash?.ok) setTimeout(() => (addFlash = null), 2000);
    }
  }
</script>

<div class="rail" bind:this={railEl} data-testid="right-rail">
  <div class="tabs">
    <button class:on={tab === 'suggested'} on:click={() => (tab = 'suggested')}>Suggested</button>
    <button class:on={tab === 'map'} on:click={() => (tab = 'map')}>Map</button>
    <button class:on={tab === 'budget'} on:click={() => (tab = 'budget')}>Budget</button>
    <button class:on={tab === 'safety'} on:click={() => (tab = 'safety')}>Safety</button>
    {#if isBooked}
      <button class:on={tab === 'aftercare'} on:click={() => (tab = 'aftercare')} data-testid="tab-aftercare-btn">Monitor</button>
    {/if}
  </div>

  {#if tab === 'suggested'}
    <div class="pane" data-testid="tab-suggested">
      {#if addFlash}
        <div class="add-flash" class:ok={addFlash.ok} data-testid="sug-flash" role="status">{addFlash.msg}</div>
      {/if}
      <div class="sug-list" data-testid="sug-list">
        {#each sugMeta as s}
          {@const key = `${s.legIdx}:${attractionRef(s.a)}`}
          <!-- B1 spillover (#168): same honest name-tier pipeline as the itinerary
               timeline (Itinerary.svelte) — local-script companion, verified/romanized
               badge, and the "unreadable primary" indicator when no name_en fallback
               exists at all. Shared via namePresentation() in itinerary.ts so this and
               the timeline never drift. -->
          {@const np = namePresentation(s.a)}
          <div class="sug" data-testid="sug-item-{attractionRef(s.a)}"
            role="button" tabindex="0"
            draggable="true"
            on:dragstart={(e) => onSugDragStart(e, s)}
            on:keypress={(e) => e.key === 'Enter' && addSuggestion(s)}>
            <span class="thumb">📍</span>
            <span class="sug-b">
              <span class="nm-row">
                <span class="nm">{np.primary}</span>
                {#if np.badge}<span class="name-src-badge" class:verified={np.badge !== 'romanized'} class:romanized={np.badge === 'romanized'}>{np.badge}</span>{/if}
              </span>
              {#if np.local}<span class="local-name">{np.local}</span>{/if}
              {#if np.unreadable}<span class="name-src-badge unreadable" title="No English name available">shown in original script — no English name available</span>{/if}
              <span class="mt">{prettyCategory(s.a.category)}{#if s.city} · {s.city}{/if}</span>
            </span>
            <span class="sug-ctrl">
              {#if s.numDays > 1}
                <select class="day-pick" data-testid="sug-day-{attractionRef(s.a)}"
                  bind:value={targetDay[key]}
                  on:change={() => { targetDay = targetDay; }}>
                  {#each { length: s.numDays } as _, di}
                    <option value={di}>Day {di + 1}</option>
                  {/each}
                </select>
              {/if}
              <button class="add active" data-testid="sug-add-{attractionRef(s.a)}"
                aria-busy={addBusy === key}
                class:busy={addBusy === key}
                on:click={() => addSuggestion(s)}>
                {addBusy === key ? '…' : '+ Add'}
              </button>
            </span>
          </div>
        {:else}
          <p class="empty">No extra candidates for this trip.</p>
        {/each}
      </div>
    </div>
  {:else if tab === 'map'}
    <div class="pane mappane" data-testid="tab-map">
      <Map {pins} />
      <p class="mappane-hint">Real coordinates (OSM). Hotel pins show the geocoded location where available, else “approx (city centre)”.</p>
    </div>
  {:else if tab === 'budget'}
    <div class="pane" data-testid="tab-budget">
      {#each rows as r}
        <div class="brow"><span>{r.label}</span><span class="v">{r.value}</span></div>
      {:else}
        <p class="empty">No budget breakdown served.</p>
      {/each}
      {#if pct != null}
        <div class="bar"><i style="width:{pct}%"></i></div>
        <p class="hint">{pct}% of your budget · {centsToUsd(usedCents)} of {centsToUsd(result.total_budget_cents)}</p>
      {/if}
      {#if result.insurance}<p class="hint">Travel insurance premium is a seeded estimate included in the trip total — Travel Guild doesn't sell or charge for insurance; compare providers independently (see Safety tab).</p>{/if}
      {#if result.currency_review}
        {@const ind = fmtIndicative(result.currency_review)}
        {#if ind}
          <p class="hint indicative" data-testid="budget-indicative">
            {ind} <span class="tag">indicative</span>
          </p>
          <p class="hint disclaimer">{result.currency_review.disclaimer}</p>
          {#if result.currency_review.exchange_timing}
            <p class="hint timing">{result.currency_review.exchange_timing.guidance}</p>
          {/if}
        {/if}
      {/if}
    </div>
  {:else if tab === 'safety'}
    <div class="pane" data-testid="tab-safety">
      {#if dedupedAdvisories.length}
        <!-- #205: result.advisories[] — orchestrator-lifted honesty advisories
             (society/orchestration/orchestrator.py ~L2118-2164), pooled from
             compliance/health/risk flags + day-planner notes into one flat
             list[str] with no kind/category field. Trip-level (not per-leg), so
             it sits above the per-leg risk cards. Styled as a dim informational
             disclosure list (.adv-detail), matching the planning_note (#189) and
             dayplan-notes (#190) precedent — not an alarm treatment. Deduped
             against per-leg advisory details, see dedupedAdvisories above. -->
        <div class="trip-advisories" data-testid="trip-advisories">
          <div class="ta-h">Advisories</div>
          {#each dedupedAdvisories as adv, i (i)}
            <div class="adv-detail" data-testid="advisory-item">{adv}</div>
          {/each}
        </div>
      {/if}
      {#each risks as r, i}
        {@const lines = hazardLines(r)}
        <div class="leg-risk">
          <div class="lr-h">
            <span class="lvl {tierClass[r.alert_tier ?? ''] ?? 'low'}">{r.alert_tier ?? 'LOW'}</span>
            <span class="lr-city">{legs[i]?.city ?? r.city ?? `Leg ${i + 1}`}</span>
          </div>
          {#each lines as h}
            <div class="haz"><span class="hl">{h.label}</span><span class="hp">~{h.pct}%</span></div>
          {/each}
          {#each r.advisory ?? [] as a}
            <!-- Finding #6 (map/mobile UX sweep): a.type is a raw served snake_case
                 key (e.g. "median_delay", "seismic_resilience") — was rendered
                 verbatim, violating the #201 user-facing-register bar (no raw
                 keys in copy). prettyCategory() is the SAME humanizer already
                 used for suggestion-card categories two lines below (and
                 Itinerary.svelte's timeline chips) — reused rather than a new
                 bespoke formatter for one more snake_case field. -->
            <!-- the literal " · " text used to sit as leading whitespace inside the
                 {#if} block, which Svelte's default whitespace handling silently
                 trimmed at the block boundary, producing "Median Delay· info" with
                 no space (found in adversarial review). Force
                 it as an explicit string expression so it's never trimmed. -->
            <div class="haz advisory" data-testid="advisory"><span class="hl">{prettyCategory(a.type)}{#if a.severity}{' · '}{a.severity}{/if}</span></div>
            {#if a.detail}<div class="adv-detail">{a.detail}</div>{/if}
          {/each}
          {#if !lines.length && !(r.advisory?.length)}
            <div class="haz muted">No notable hazards for these dates.</div>
          {/if}
          {#if r.planning_note}<div class="adv-detail" data-testid="planning-note">{r.planning_note}</div>{/if}
          {#if r.decisions?.avoid_window}<div class="verdict avoid">AVOID this window</div>
          {:else if r.decisions?.flag}<div class="verdict flag">FLAG — review</div>{/if}
        </div>
      {/each}

      {#if emergencies.length}
        <div class="emg-list">
          {#each emergencies as e}
            <div class="emg {e.status}">
              {#if e.status === 'active'}WARNING DO NOT TRAVEL{:else if e.status === 'monitoring'}Monitoring{:else if e.status === 'unavailable'}Feed unavailable{:else}Clear{/if}
              · {e.city ?? e.leg_id ?? ''}{#if e.headline} · {e.headline}{/if}
            </div>
          {/each}
        </div>
      {/if}

      {#if bookingLinkGroups.length}
        <!-- #211: result.booking_links.{lodging,transport,visa,health,insurance}
             (society/utils/booking_links.py) — CONSTRUCTED reference/handoff deep-links,
             never fetched/live-verified and never a confirmed booking. Styled deliberately
             muted (reuses .adv-detail's informational weight, NOT Itinerary.svelte's
             .lp-book "Book →" treatment, which implies a real live/actionable price) so the
             honesty distinction survives visually as well as in copy. Each entry's own
             `label` already states the honesty caveat in prose (e.g. "not a confirmed
             booking", "verify … yourself"); the `kind` discriminator is not additionally
             surfaced as a badge — that would be over-engineering on top of the label text. -->
        <div class="trip-links" data-testid="trip-booking-links">
          <div class="ta-h">Reference links</div>
          {#each bookingLinkGroups as g (g.key)}
            <div class="link-group" data-testid="booking-link-group-{g.key}">
              <div class="lg-h">{g.title}</div>
              {#each g.entries as entry, i (i)}
                <div class="link-row" data-testid="booking-link-item">
                  <!-- dir="auto" (#233 root cause D): the backend already isolates any
                       embedded RTL name with FSI/PDI marks, but this is defense-in-depth
                       for the rendered DOM — a flex item is its own bidi paragraph and
                       defaults to LTR, so without this a raw RTL label would still be
                       base-direction-LTR here. -->
                  <span class="link-label" dir="auto">{entry.label}</span>
                  {#if entry.providers?.length}
                    <span class="link-providers">
                      {#each entry.providers as p, pi (p.name + pi)}
                        {@const phref = safeHref(p.url)}
                        {#if phref}
                          <a class="link-ext" data-testid="booking-link-anchor"
                            href={phref} target="_blank" rel="noopener noreferrer">{p.name} ↗</a>
                        {/if}
                      {/each}
                    </span>
                  {:else}
                    {@const href = safeHref(entry.booking_url)}
                    {#if href}
                      <a class="link-ext" data-testid="booking-link-anchor"
                        href={href} target="_blank" rel="noopener noreferrer">↗</a>
                    {/if}
                  {/if}
                </div>
              {/each}
            </div>
          {/each}
        </div>
      {/if}
    </div>
  {:else if tab === 'aftercare'}
    <div class="pane" data-testid="tab-aftercare">
      <Aftercare {result} {user_id} />
    </div>
  {/if}
</div>

<style>
  /* Sticky so the rail follows scroll on the wide-screen 2-col layout.
     min-width:0 mirrors App.svelte's sibling grid item (.center) for the same
     reason: a CSS Grid item's default min-width is content-based (its
     min-content size), NOT 0, UNLESS the item is itself a scroll container
     (overflow != visible) — but mobile's own .rail override below sets
     overflow:visible (deliberately, for iOS Safari's nested-scroll issue), which
     re-enables that content-based auto-min. Finding #5's fix made the Suggested
     tab's mobile carousel a real horizontal-scrolling row of fixed 240px cards;
     without this explicit min-width:0, THAT row's min-content size propagated
     all the way up into .rail's grid-item sizing and blew the whole single-col
     grid track (hence the whole page) out past the viewport width. */
  .rail { background: #fff; border: 1px solid #ece2d5; border-radius: 14px; overflow: hidden;
    position: sticky; top: 14px; max-height: calc(100vh - 28px); min-width: 0; }
  .tabs { display: flex; gap: 3px; padding: 9px 10px 0; border-bottom: 1px solid #ece2d5; }
  .tabs button { border: 0; background: transparent; padding: 7px 10px; border-radius: 8px 8px 0 0;
    cursor: pointer; font-weight: 600; font-size: 13px; color: #7c7468; }
  .tabs button.on { background: #fff; color: #2d2a26; border: 1px solid #ece2d5; border-bottom-color: #fff; margin-bottom: -1px; }
  .pane { padding: 13px; overflow-y: auto; max-height: calc(100vh - 28px - 44px);
    -webkit-overflow-scrolling: touch; }

  /* Below 1100px the 2-col grid collapses to 1 col (mirrors the JS breakpoint above) and
     the rail is no longer a sticky sidebar -- it's inline page content. Sticky positioning
     + a 100vh-based height cap + overflow:hidden on the outer wrapper made the inner .pane's
     scroll unreliable on mobile (iOS Safari in particular won't reliably deliver touch-scroll
     into a nested overflow:auto container sitting inside a position:sticky ancestor). Let the
     page scroll naturally on mobile instead of nesting a nested scroll region. */
  @media (max-width: 1100px) {
    .rail { position: static; max-height: none; overflow: visible; }
    .pane { max-height: none; overflow-y: visible; -webkit-overflow-scrolling: unset; }
  }
  .mappane { padding: 0; }
  .mappane :global(.tg-map) { height: 280px; }
  .mappane-hint { font-size: 11.5px; color: #7c7468; margin: 8px 0 0; padding: 8px 13px; }
  .hint { font-size: 11.5px; color: #7c7468; margin: 8px 0 0; }
  .empty { color: #998a78; font-size: 13px; }
  /* Desktop: plain vertical list -- .pane's own overflow-y:auto already makes it
     scrollable (no per-card change needed). Mobile below turns this into a
     horizontal swipeable carousel instead. */
  .sug-list { display: flex; flex-direction: column; }
  .sug { display: flex; gap: 10px; padding: 9px 0; border-bottom: 1px solid #f0e8db; align-items: center;
    cursor: grab; }
  .sug:active { cursor: grabbing; }
  .thumb { width: 34px; height: 34px; border-radius: 9px; background: #f1eadf; display: flex;
    align-items: center; justify-content: center; flex: 0 0 auto; }
  .sug-b { flex: 1; display: flex; flex-direction: column; min-width: 0; }
  /* B1 spillover (#168): same visual family as Itinerary.svelte's .name-row/.name-src-badge/
     .local-name (see that file for the original). .nm-row wraps the primary name + badge so
     the badge doesn't force the name off its line; .nm itself keeps its one-line truncation. */
  .nm-row { display: flex; align-items: baseline; gap: 6px; min-width: 0; }
  .nm { font-weight: 600; font-size: 14px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; min-width: 0; }
  .local-name { font-size: 11.5px; color: #7c7468; font-weight: 500; }
  .name-src-badge { font-size: 9px; font-weight: 700; text-transform: uppercase; letter-spacing: .3px;
    border-radius: 4px; padding: 1px 5px; vertical-align: middle; flex: 0 0 auto; }
  .name-src-badge.verified { background: #e6f1e9; color: #5a8a63; }
  .name-src-badge.romanized { background: #fbf0db; color: #8a6a10; }
  .name-src-badge.unreadable {
    background: #f1eee7; color: #7c7468; text-transform: none; letter-spacing: normal;
    font-weight: 600; font-size: 10.5px; white-space: normal; display: inline-block; margin-top: 1px;
  }
  .mt { color: #7c7468; font-size: 12.5px; }
  .sug-ctrl { display: flex; flex-direction: column; align-items: flex-end; gap: 4px; flex-shrink: 0; }

  /* Phone width: Suggested-tab cards become a horizontal swipeable carousel instead
     of a tall vertical list -- mirrors Itinerary.svelte's own mobile breakpoint.
     Finding #5 fix: MUST come AFTER the base .sug-list/.sug/.sug-ctrl rules above —
     these share the exact same selectors at equal specificity, so with the media
     query declared FIRST (as it previously was, ~30 lines up) the cascade's
     source-order tiebreak let the later, unconditional base rules win even on
     mobile: .sug-list stayed flex-direction:column (the carousel never activated)
     and .sug fell back to align-items:center (not flex-start). Also adds
     width:100% to .sug-b (missing before): under column layout + flex-start,
     flex items are NOT stretched to the card's width by default, so .nm's
     white-space:nowrap name had no bound to ellipsis against and rendered at its
     full natural width, overflowing the 240px card (and the viewport). */
  @media (max-width: 768px) {
    .sug-list { flex-direction: row; overflow-x: auto; gap: 10px;
      scroll-snap-type: x proximity; -webkit-overflow-scrolling: touch; padding-bottom: 4px; }
    .sug { flex: 0 0 auto; width: 240px; scroll-snap-align: start;
      flex-direction: column; align-items: flex-start; border: 1px solid #ece2d5;
      border-radius: 10px; padding: 10px; border-bottom: 1px solid #ece2d5; }
    .sug-b { width: 100%; max-width: 100%; }
    .sug-ctrl { flex-direction: row; align-items: center; width: 100%;
      justify-content: space-between; margin-top: 6px; }

    /* Finding #8 (map/mobile UX sweep): ChatPane's floating .bot-trigger FAB
       (fixed, bottom-left, 52px + 16px bottom offset ≈ 68px footprint) sat on
       top of whatever this pane's own scroll happened to bring to the bottom
       — the Safety tab's insurance reference link and the Map tab's OSM
       attribution corner were the two reported cases. B5 already reserves this
       same space for SHEETS (hideMobileBubble), but ordinary scrolled-under
       pane content had no reservation at all. Same padding-bottom-reservation
       pattern Preview.svelte already uses for its own persistent bottom bar
       (.preview{padding-bottom:78px}) — sized to clear the trigger regardless
       of which tab is open, not just Safety/Map. */
    .pane { padding-bottom: 84px; }
  }

  .day-pick { font-size: 11.5px; border: 1px solid #ece2d5; border-radius: 6px; padding: 2px 5px;
    background: #fff; color: #2d2a26; cursor: pointer; }
  .add { border: 1px solid #ece2d5; background: #fff; border-radius: 8px; padding: 5px 10px;
    font-size: 12.5px; font-weight: 600; color: #998a78; cursor: default; }
  .add.active { color: #d9774a; border-color: #f0c4a0; cursor: pointer; }
  .add.active:hover { background: #fff4ee; border-color: #d9774a; }
  .add.busy { opacity: 0.6; pointer-events: none; }
  .add-flash { font-size: 12px; border-radius: 7px; padding: 5px 10px; margin-bottom: 6px;
    font-weight: 600; border: 1px solid transparent; }
  .add-flash.ok { background: #e6f4ec; border-color: #a3d0b0; color: #3a7a52; }
  .add-flash:not(.ok) { background: #fef2f2; border-color: #f0c4c4; color: #b05050; }
  .brow { display: flex; justify-content: space-between; padding: 7px 0; border-bottom: 1px solid #f0e8db; font-size: 14px; }
  .brow .v { font-variant-numeric: tabular-nums; font-weight: 600; }
  .bar { height: 9px; border-radius: 6px; background: #eee3d4; overflow: hidden; margin: 10px 0 0; }
  .bar > i { display: block; height: 100%; background: #4f8a63; }
  .trip-advisories { padding: 0 0 9px; border-bottom: 1px solid #f0e8db; }
  .ta-h { font-weight: 600; font-size: 13px; color: #2d2a26; margin-bottom: 2px; }
  .leg-risk { padding: 9px 0; border-bottom: 1px solid #f0e8db; }
  .lr-h { display: flex; align-items: center; gap: 8px; }
  .lvl { font-size: 11px; font-weight: 700; border-radius: 6px; padding: 2px 8px; }
  .lvl.low { background: #e6f1e9; color: #4f8a63; }
  .lvl.med { background: #fbf0db; color: #c98a2b; }
  .lvl.high { background: #f8e4de; color: #c0563f; }
  .lr-city { font-weight: 600; }
  .haz { display: flex; justify-content: space-between; font-size: 13px; padding: 3px 0 0; }
  .haz.muted { color: #998a78; }
  .haz.advisory .hl { color: #c0563f; font-weight: 600; text-transform: capitalize; }
  .adv-detail { font-size: 12px; color: #7c7468; padding: 0 0 2px 2px; }
  .hp { font-variant-numeric: tabular-nums; color: #7c7468; }
  .verdict { font-size: 12px; font-weight: 700; margin-top: 4px; }
  .verdict.avoid { color: #c0563f; }
  .verdict.flag { color: #c98a2b; }
  .emg { font-size: 12.5px; padding: 7px 0; }
  .emg.active { color: #c0563f; font-weight: 700; }
  .emg.monitoring { color: #5a73b0; }
  .emg-list { margin-top: 8px; border-top: 1px solid #ece2d5; padding-top: 6px; }
  /* #211: deliberately muted/informational — reuses .trip-advisories/.ta-h's visual
     weight, NOT Itinerary.svelte's .lp-book "Book →" chip (that styling implies a
     live, actionable price; these are always-present constructed reference links). */
  .trip-links { margin-top: 8px; border-top: 1px solid #ece2d5; padding-top: 8px; }
  .link-group { padding: 4px 0; }
  .lg-h { font-size: 11px; font-weight: 700; text-transform: uppercase; letter-spacing: .3px;
    color: #998a78; margin: 4px 0 2px; }
  .link-row { display: flex; justify-content: space-between; align-items: baseline; gap: 8px;
    font-size: 12px; color: #7c7468; padding: 2px 0; }
  .link-label { flex: 1; }
  .link-providers { display: flex; gap: 8px; flex-shrink: 0; }
  .link-ext { color: #8a7a5c; font-size: 11.5px; font-weight: 600; text-decoration: none;
    flex-shrink: 0; white-space: nowrap; }
  .link-ext:hover { text-decoration: underline; }
  .indicative { color: #5a73b0; font-size: 13px; font-weight: 600; margin-top: 10px; border-top: 1px solid #f0e8db; padding-top: 8px; }
  .tag { background: #e8edf8; color: #5a73b0; border-radius: 5px; padding: 1px 6px; font-size: 10.5px; font-weight: 700; margin-left: 4px; }
  .disclaimer { font-size: 10.5px; color: #998a78; margin-top: 2px; }
  .timing { font-size: 11px; color: #7c7468; margin-top: 3px; font-style: italic; }
</style>
