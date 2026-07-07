<script lang="ts">
  import { createEventDispatcher } from 'svelte';
  import { centsToUsd } from '../api';
  import type { NegotiateResult, DayPlan, DayDetail } from '../api';
  import { mealAnchoredTimeline, budgetRows, namePresentation } from '../itinerary';
  import type { TimelineItem } from '../itinerary';
  import { downloadIcs, assistantSummary } from '../ics';
  import PlacePhotos from './PlacePhotos.svelte';

  // Draft 2 of the trip-summary/save-to-phone redesign, reconciled and reviewed.
  // Source of truth for markup/CSS: an internal design draft — hero band
  // header, top-aligned (not sticky) accent budget card on desktop, left-edge
  // timeline day styling.
  //
  // Mobile budget presentation (reviewed and confirmed): a persistent
  // collapsed bottom bar (total + chevron + Save)
  // that slides up a full-breakdown sheet docked above it, reusing ChatPane's
  // .mob-sheet transform/transition/radius pattern. Supersedes Draft 2's mobile
  // sticky-bar + always-reflowed budget section (not a literal accordion).

  export let result: NegotiateResult;
  const dispatch = createEventDispatcher<{ close: void }>();
  let budgetExpanded = false;   // mobile-only: budget-sheet open/closed (Draft 3)

  $: plans = result.day_plans ?? [];
  $: narrative = assistantSummary(result);       // LLM-on summary, or null (no fabrication)
  // Honesty caveat: the backend flags the narrative stale after a structural /replan
  // edit — it was written before that edit and may still describe stops that were
  // since removed/added/reordered. Only shown when both the narrative AND the flag
  // are present; never invented client-side.
  $: staleNarrative = narrative && result.itinerary_narrative?.stale === true;
  $: staleReason = result.itinerary_narrative?.stale_reason
    ?? 'This summary may be outdated after your latest edit.';
  $: rows = budgetRows(result);
  // Money-itemization fix: fall back to the LODGING figure (package_total_cents), never
  // the with-fees total — fee estimates (insurance/visa) are never actually charged, so
  // a missing wallet.debit_cents (e.g. the atomic commit path) must not silently present
  // the with-fees amount as what was charged.
  $: charged = result.wallet?.debit_cents ?? result.package_total_cents ?? result.package_total_with_fees_cents;
  // Label follows the source: a real wallet debit is "Charged"; without one (rare —
  // pre-booking preview / degraded envelope) it's honestly the trip total, not a charge.
  $: chargedLabel = result.wallet?.debit_cents != null ? 'Charged' : 'Trip total';
  // split the narrative into paragraphs for legibility (presentation only)
  $: paras = (narrative ?? '').split(/\n{2,}|\n/).map((p) => p.trim()).filter(Boolean);
  $: route = plans.map((p) => p.city).filter(Boolean).join(' → ');
  // Only show a country suffix when every leg shares exactly one country — a
  // multi-country route (e.g. Tokyo → Seoul) must not be attributed to the
  // first leg's country alone (that would fabricate "Seoul, Japan").
  $: routeCountry = (() => {
    const cs = new Set(plans.map((p) => p.country).filter(Boolean));
    // require EVERY leg to have a known country too — a mix of one known-country
    // leg and one unknown-country leg must not misattribute the unknown leg.
    return cs.size === 1 && plans.every((p) => p.country) ? [...cs][0] : '';
  })();
  // budgetRows() has no single served "total" field — 'Bookable package' (no-fee trips)
  // or 'Trip total (lodging + est. fees)' (fee-split trips, money-itemization fix) is the
  // row that carries the trip-total amount; 'Your budget' is a ceiling, not a total, so it
  // is deliberately NOT highlighted the same way. This reuses real row labels rather than
  // inventing a total field.
  $: totalRowLabel = rows.some((r) => r.label === 'Trip total (lodging + est. fees)') ? 'Trip total (lodging + est. fees)'
    : rows.some((r) => r.label === 'Bookable package') ? 'Bookable package' : null;

  function dateOf(checkin: string | undefined, dayIndex: number): string {
    const m = /^(\d{4})-(\d{2})-(\d{2})/.exec(checkin ?? '');
    if (!m) return '';
    const d = new Date(Date.UTC(+m[1], +m[2] - 1, +m[3]));
    d.setUTCDate(d.getUTCDate() + dayIndex);
    return d.toLocaleDateString('en-US', { weekday: 'short', month: 'short', day: 'numeric', timeZone: 'UTC' });
  }

  // Leg-hero band source (an internal design draft Item 1, "Leg-hero
  // source + fetch" decision): the leg's MARQUEE activity — the first activity-kind
  // item (never a meal) across that leg's days in mealAnchoredTimeline order — so the
  // photo is a real place already in the itinerary, not an unrelated stock shot.
  function legMarqueeName(dp: DayPlan): string | null {
    for (const day of dp.days ?? []) {
      const act = mealAnchoredTimeline(day).find((it) => it.kind === 'activity');
      if (act) return act.name;
    }
    return null;
  }

  // (G3, #181) TimelineItem.name is already displayName()-collapsed (no name_en/
  // name_source survives mealAnchoredTimeline), so recover the raw served object per
  // item — mirrors Itinerary.svelte's rawNameSourceFor exactly — and route it through
  // the SAME shared namePresentation() pipeline as the main itinerary timeline. A user
  // exporting/saving their trip should see the same honest local-companion name /
  // unreadable-primary indicator they saw while planning, not a silently-flattened one.
  function rawNameSourceFor(it: TimelineItem, day: DayDetail): { name?: string; name_en?: string; name_source?: unknown } | null {
    if (it.kind === 'activity' && it.attrIndex >= 0) return (day.attractions?.[it.attrIndex] ?? null) as unknown as { name?: string; name_en?: string; name_source?: unknown } | null;
    if (it.kind === 'meal') {
      const slotKey = it.slot.toLowerCase();
      const meals = day.meals as unknown as Record<string, { name?: string; name_en?: string; name_source?: unknown } | null | undefined> | undefined;
      return meals?.[slotKey] ?? null;
    }
    return null;
  }
</script>

<section class="preview" data-testid="preview">
  <div class="hero">
    <button class="icon-btn back" aria-label="Back" data-testid="preview-back" on:click={() => dispatch('close')}>←</button>
    <button class="icon-btn save" aria-label="Save to calendar" data-testid="save-ics" on:click={() => downloadIcs(result)}>📅</button>
    <div class="hero-center">
      {#if result.booking_ref}<div class="hero-ref">{result.booking_ref}</div>{/if}
      <div class="hero-title">Your trip</div>
      <div class="hero-route">{route}{#if routeCountry}, {routeCountry}{/if}</div>
      <!-- hidden on mobile only when the budget bar exists to carry the total instead
           (rows.length) — otherwise this is the only place the total is shown, so it
           must stay visible even on mobile when there's no budget bar. -->
      <div class="hero-price" class:mob-hide={rows.length > 0}>{centsToUsd(charged)}<span class="tag">SIMULATED prepaid</span></div>
    </div>
  </div>

  <div class="body-pad">
    {#if narrative}
      <div class="narrative" data-testid="narrative">
        <div class="nlabel">✨ Your assistant's summary</div>
        {#each paras as p}<p>{p}</p>{/each}
        {#if staleNarrative}
          <div class="narrative-stale" data-testid="narrative-stale">{staleReason}</div>
        {/if}
      </div>
    {/if}

    <div class="layout" class:single={!rows.length}>
      <main>
        {#each plans as dp, pi}
          {@const marquee = legMarqueeName(dp)}
          <article class="leg">
            <h2>{dp.city}{#if dp.country}<span class="cc">, {dp.country}</span>{/if}</h2>
            <!-- Leg-hero band (an internal design draft Item 1): exactly one
                 per city leg, between <h2> and the first .day, itinerary column only.
                 Renders nothing if there's no marquee activity or the fetch comes back
                 empty/failed — PlacePhotos variant="banner" fails silent by design. -->
            {#if marquee}
              <PlacePhotos name={marquee} city={dp.city ?? ''} country={dp.country ?? ''} variant="banner" />
            {/if}
            {#each dp.days ?? [] as day, di}
              {@const items = mealAnchoredTimeline(day)}
              <div class="day">
                <div class="dhead">Day {(day.day_index ?? di) + 1}<span class="ddate">{dateOf(dp.checkin ?? result.legs?.[pi]?.checkin, day.day_index ?? di)}</span></div>
                {#if items.length}
                  {#each items as it}
                    {@const inp = namePresentation(rawNameSourceFor(it, day))}
                    <div class="titem">
                      <span class="tslot">{it.slot}</span>
                      <span class="tname">{inp.primary}</span>
                      {#if inp.local}<span class="local-name">{inp.local}</span>{/if}
                      {#if inp.badge}<span class="name-src-badge" class:verified={inp.badge !== 'romanized'} class:romanized={inp.badge === 'romanized'}>{inp.badge}</span>{/if}
                      {#if inp.unreadable}<span class="name-src-badge unreadable" title="No English name available">shown in original script — no English name available</span>{/if}
                      {#if it.category}<span class="tcat">{it.category}</span>{/if}
                    </div>
                  {/each}
                {:else}
                  <div class="tempty">Free day — no fixed activities.</div>
                {/if}
              </div>
            {/each}
          </article>
        {/each}
      </main>

      {#if rows.length}
        <aside>
          <div class="budget-card">
            <h2>Budget</h2>
            {#each rows as r}
              <div class="brow" class:total={r.label === totalRowLabel}><span>{r.label}</span><span class="bv">{r.value}</span></div>
            {/each}
            <button class="dl-link" data-testid="save-ics-card" on:click={() => downloadIcs(result)}>📅 Save to calendar (.ics)</button>
          </div>
        </aside>
      {/if}
    </div>
  </div>

  <footer class="p-footer">Saved offline-ready. Re-open anytime; changes &amp; rebooking happen on the webpage for security.</footer>

  {#if rows.length}
    <!-- Mobile-only slide-up breakdown sheet (Draft 3) — docked above the bar,
         not overlapping it; reuses ChatPane's .mob-sheet transform/transition/
         radius pattern verbatim (an internal design draft lines 144-154). -->
    <div class="budget-sheet" id="budgetSheet" class:bs-open={budgetExpanded} aria-hidden={!budgetExpanded}>
      <div class="bs-hdr">
        <span class="bs-ttl">Budget breakdown</span>
        <button class="bs-close" aria-label="Close" on:click={() => (budgetExpanded = false)}>▼</button>
      </div>
      <div class="bs-body">
        {#each rows as r}
          <div class="brow" class:total={r.label === totalRowLabel}><span>{r.label}</span><span class="bv">{r.value}</span></div>
        {/each}
      </div>
    </div>
    <!-- Mobile-only persistent collapsed bar (Draft 3) — total + expand chevron +
         Save; never disappears, acts as the sheet's anchor (draft3.html lines
         291-297 / 372-378). Save lives ONLY here, not duplicated into the sheet. -->
    <div class="budget-bar">
      <button class="bb-toggle" aria-expanded={budgetExpanded} aria-controls="budgetSheet"
        on:click={() => (budgetExpanded = !budgetExpanded)}>
        <span class="bb-total"><span class="lbl">{chargedLabel}</span>{centsToUsd(charged)}</span>
        <span class="bb-chevron" class:bb-open={budgetExpanded}>▲</span>
      </button>
      <button class="bb-save" data-testid="save-ics-sticky" on:click={() => downloadIcs(result)}>📅 Save</button>
    </div>
  {/if}
</section>

<style>
  /* Palette pulled verbatim from an internal design draft :root */
  .preview {
    --accent: #d9774a; --accent-lt: #fff4ee; --accent-md: #f0c4a0;
    --bg: #faf6f0; --bg-card: #ffffff; --border: #ece2d5; --border-md: #ddd5c8;
    --text: #2d2a26; --text-mute: #7c7468; --text-dim: #a89e94;
    max-width: 1140px; margin: 0 auto; padding: 0 0 24px; background: var(--bg); color: var(--text);
  }

  /* ── Hero band header — full-width tinted band; back/save are corner icon-buttons
     so title+route+price own the center (mockup .hero / .hero-center / .icon-btn). ── */
  .hero { background: linear-gradient(135deg, var(--accent-lt), #fff); border-bottom: 1px solid var(--border);
    padding: 18px 24px 20px; position: relative; }
  .icon-btn { position: absolute; top: 18px; width: 34px; height: 34px; border-radius: 50%;
    border: 1px solid var(--border); background: #fff; display: flex; align-items: center; justify-content: center;
    font-size: 14px; cursor: pointer; }
  .icon-btn.back { left: 24px; }
  .icon-btn.save { right: 24px; }
  .icon-btn:hover { border-color: var(--accent); }
  .hero-center { text-align: center; padding: 0 50px; }
  .hero-ref { font-size: 11px; font-weight: 700; color: var(--accent); text-transform: uppercase; letter-spacing: .5px; }
  .hero-title { font-size: 22px; font-weight: 800; letter-spacing: -.3px; margin-top: 2px; }
  .hero-route { font-size: 13px; color: var(--text-mute); margin-top: 4px; }
  .hero-price { font-size: 26px; font-weight: 800; color: var(--text); margin-top: 10px; }
  .hero-price .tag { display: block; font-size: 10.5px; font-weight: 500; color: var(--text-dim); margin-top: 2px; }

  .body-pad { padding: 20px 24px 0; }

  .narrative { background: var(--bg-card); border: 1px solid var(--border); border-radius: 10px;
    padding: 14px 18px; margin-bottom: 20px; }
  .nlabel { font-size: 11px; font-weight: 700; text-transform: uppercase; letter-spacing: .5px;
    color: var(--accent); margin-bottom: 6px; }
  .narrative p { font-size: 13.5px; color: var(--text); margin-top: 6px; }
  .narrative p:first-of-type { margin-top: 0; }
  /* Honest "may be outdated" caveat — muted italic caption, NOT an alarm color;
     matches the .translation-note / .ic-caveat honesty-note pattern used elsewhere
     in this codebase (Aftercare.svelte, Itinerary.svelte). */
  .narrative-stale { font-size: 11.5px; color: var(--text-dim); font-style: italic; margin-top: 8px; }

  /* ── Two-column layout — budget card TOP-ALIGNED (not sticky): a heavier
     accent-bordered card, meant to be seen once rather than tracked while
     scrolling (mockup .layout, decided tradeoff vs Draft 1's sticky rail). ── */
  .layout { display: grid; grid-template-columns: 1fr 300px; gap: 20px; align-items: start; }
  .layout.single { grid-template-columns: 1fr; }

  .leg { margin-bottom: 22px; }
  .leg h2 { font-size: 16px; font-weight: 700; margin-bottom: 10px; }
  .leg h2 .cc { font-weight: 400; color: var(--text-dim); font-size: 13px; }

  /* ── Timeline-dot day treatment — a connecting rail down the left edge
     instead of per-day cards (mockup .day / .day::before). ── */
  .day { position: relative; padding: 4px 0 4px 22px; margin-bottom: 14px; border-left: 2px solid var(--border); }
  .day::before { content: ''; position: absolute; left: -6px; top: 4px; width: 10px; height: 10px;
    border-radius: 50%; background: var(--accent); border: 2px solid #fff; box-shadow: 0 0 0 1px var(--accent); }
  .dhead { font-weight: 700; font-size: 13.5px; margin-bottom: 6px; }
  .ddate { font-weight: 400; color: var(--text-dim); font-size: 11.5px; margin-left: 6px; }
  .titem { display: flex; gap: 8px; align-items: center; padding: 4px 0; }
  .tslot { font-size: 10px; font-weight: 700; text-transform: uppercase; letter-spacing: .3px; color: #fff;
    background: var(--accent-md); border-radius: 5px; padding: 2px 6px; flex-shrink: 0; }
  .tname { font-size: 13px; }
  .tcat { color: var(--text-dim); font-size: 11.5px; }
  .tempty { color: var(--text-dim); font-size: 12.5px; padding: 4px 0; }

  /* (G3, #181) same honest name-tier visual family as Itinerary.svelte's
     .local-name/.name-src-badge (see that file for the original) — the trip
     summary must show the same local-companion name / unreadable-primary
     indicator the user saw while planning. */
  .local-name { font-size: 11.5px; color: var(--text-mute); font-weight: 500; }
  .name-src-badge { font-size: 9px; font-weight: 700; text-transform: uppercase; letter-spacing: .3px;
    border-radius: 4px; padding: 1px 5px; vertical-align: middle; }
  .name-src-badge.verified { background: #e6f1e9; color: #5a8a63; }
  .name-src-badge.romanized { background: #fbf0db; color: #8a6a10; }
  .name-src-badge.unreadable {
    background: #f1eee7; color: var(--text-mute); text-transform: none; letter-spacing: normal;
    font-weight: 600; font-size: 10.5px; white-space: normal;
  }

  .budget-card { background: var(--bg-card); border: 1px solid var(--accent-md); border-left: 4px solid var(--accent);
    border-radius: 12px; padding: 16px 18px; }
  .budget-card h2 { font-size: 14px; font-weight: 700; margin-bottom: 10px; }
  .brow { display: flex; justify-content: space-between; padding: 7px 0; border-top: 1px solid #f2ece2; font-size: 13px; }
  .brow.total { border-top: 2px solid var(--border); margin-top: 4px; padding-top: 10px; font-weight: 800; font-size: 15px; }
  .brow .bv { font-weight: 600; font-variant-numeric: tabular-nums; }
  .brow.total .bv { color: var(--accent); }
  .dl-link { display: block; width: 100%; text-align: center; margin-top: 14px; font-size: 12px; font-weight: 700;
    color: var(--accent); text-decoration: none; border: 1px solid var(--accent-md); border-radius: 9px;
    padding: 9px; background: var(--accent-lt); cursor: pointer; font-family: inherit; }

  .p-footer { color: var(--text-dim); font-size: 11.5px; margin-top: 22px; padding: 14px 24px 0;
    border-top: 1px solid var(--border); }

  /* Mobile budget bar/sheet (Draft 3, an internal design draft lines
     117-154): hidden on desktop, shown under the same 768px breakpoint
     ChatPane's hideMobileBubble uses so the two transitions line up (no window
     where both the chat trigger AND this bar are visible/hidden inconsistently).
     Bar carries the total at all times — the honest reason the hero-price is
     hidden on mobile below, so the number appears exactly once. */
  .budget-bar, .budget-sheet { display: none; }

  /* ── Mobile: hero band shrinks, price moves to the budget bar (shown once,
     not duplicated); desktop's top-aligned budget-card is hidden entirely —
     Draft 3 replaces it with the collapsed bar + slide-up breakdown sheet, so
     the full line-item breakdown lives in exactly one place on mobile, not two
     (mockup m-hero / budget-bar / budget-sheet). ── */
  @media (max-width: 768px) {
    .preview { padding-bottom: 78px; }
    .hero { padding: 14px 16px 16px; text-align: center; }
    .icon-btn { top: 14px; width: 30px; height: 30px; font-size: 13px; }
    .icon-btn.back { left: 14px; }
    /* Save lives only in the persistent budget-bar on mobile (.bb-save) — this
       hero icon duplicated it; hidden here, still shown on desktop where there's
       no persistent bar and it's the sole Save affordance. */
    .icon-btn.save { display: none; }
    .hero-title { font-size: 18px; }
    .hero-route { font-size: 12px; }
    .hero-price.mob-hide { display: none; }
    .body-pad { padding: 16px 16px 0; }
    .narrative { padding: 12px 14px; font-size: 12.5px; margin-bottom: 14px; }
    .layout { grid-template-columns: 1fr; gap: 0; }
    aside { display: none; }

    /* Collapsed persistent bar — draft3.html lines 126-138 (.budget-bar/.bb-*),
       same 58px footprint + z-index 320 as Draft 2's sticky-bar it replaces. */
    .budget-bar { display: flex; position: fixed; left: 0; right: 0; bottom: 0; height: 58px;
      background: #fff; border-top: 1px solid var(--border); padding: 0 14px; align-items: center;
      gap: 10px; box-shadow: 0 -4px 16px rgba(0,0,0,.06); z-index: 320; }
    .bb-toggle { flex: 1; display: flex; align-items: center; gap: 10px; background: none; border: none;
      text-align: left; cursor: pointer; padding: 6px 4px; font: inherit; color: inherit; border-radius: 8px; }
    .bb-toggle:active { background: var(--accent-lt); }
    .bb-total { font-size: 15px; font-weight: 800; font-variant-numeric: tabular-nums; }
    .bb-total .lbl { display: block; font-size: 9.5px; font-weight: 600; color: var(--text-dim); text-transform: uppercase; }
    .bb-chevron { width: 26px; height: 26px; border-radius: 50%; background: var(--accent-lt);
      border: 1px solid var(--accent-md); display: flex; align-items: center; justify-content: center;
      font-size: 11px; color: var(--accent); flex-shrink: 0; transition: transform .2s ease; }
    .bb-chevron.bb-open { transform: rotate(180deg); }
    .bb-save { flex-shrink: 0; background: var(--accent); color: #fff; border: none; border-radius: 9px;
      padding: 10px 14px; font-weight: 700; font-size: 12.5px; cursor: pointer; font-family: inherit; }

    /* Slide-up sheet — mirrors ChatPane's .mob-sheet verbatim (transform,
       transition timing/easing, top-corner radius, box-shadow); docked ABOVE
       the bar (bottom: 58px) rather than overlapping it, per draft3.html
       lines 140-148 / 300-317, so the bar's Save button is never covered and
       the sheet's own bottom rows are never hidden behind the bar. z-index
       319/320 is a separate band from ChatPane's 309/310 — no collision, and
       the only ChatPane element that could co-exist (the floating
       .bot-trigger/.flyout) is already hidden via hideMobileBubble. */
    .budget-sheet { position: fixed; left: 0; right: 0; bottom: 58px; max-height: 65vh; background: #fff;
      border-radius: 16px 16px 0 0; box-shadow: 0 -4px 28px rgba(0,0,0,.16); z-index: 319; display: flex;
      flex-direction: column; overflow: hidden; transform: translateY(100%);
      transition: transform .28s cubic-bezier(.22,.61,.36,1); }
    .budget-sheet.bs-open { transform: translateY(0); }
    .bs-hdr { display: flex; align-items: center; justify-content: space-between; padding: 12px 16px 10px;
      border-bottom: 1px solid var(--border); background: var(--bg); flex-shrink: 0; }
    .bs-ttl { font-size: 13px; font-weight: 700; color: var(--text); letter-spacing: .3px; }
    .bs-close { border: none; background: #ede4da; border-radius: 8px; width: 28px; height: 28px; cursor: pointer;
      font-size: 12px; display: flex; align-items: center; justify-content: center; color: var(--text-mute); }
    .bs-body { flex: 1; overflow-y: auto; -webkit-overflow-scrolling: touch; padding: 4px 16px 18px; }
  }
</style>
