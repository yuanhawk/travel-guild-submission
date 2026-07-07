<script context="module" lang="ts">
  import type { PlaceCard } from '../api';

  export type CardState =
    | { kind: 'loading' }
    | { kind: 'ok'; card: PlaceCard }
    | { kind: 'unavailable' }
    | { kind: 'error' };
</script>

<script lang="ts">
  // PinDetailContent — the INNER body of the Map pin-detail popup (photo, rating,
  // open-now, review, honest approx/loading/unavailable/error states), factored out
  // of Map.svelte's .tg-card so it can be shared by TWO thin container wrappers:
  // the desktop floating .tg-card (unchanged) and the mobile slide-up .pin-sheet
  // (see mockups/image-placement-draft1.html Item 2 — "Implementation mechanism").
  //
  // No fetch/state logic lives here — selectedPin/cardState are owned by Map.svelte
  // and passed in as props; this component only renders and re-dispatches `retry`.
  import { createEventDispatcher } from 'svelte';
  import type { MapPin } from '../api';
  import { placePhotoUrl } from '../api';

  export let selectedPin: MapPin;
  export let cardState: CardState | null;
  // Distinct testid namespace per container so desktop (place-card-*, unchanged —
  // existing map.test.ts / e2e/place-card.spec.ts keep matching exactly one element)
  // and mobile (place-sheet-*, new) never collide when both containers are mounted
  // at once (CSS display:none only hides one; the DOM node is still queryable).
  export let testidPrefix = 'place-card';
  // Mobile sheet shows the city line in the body (desktop shows it in the header
  // instead — see mockup's .ps-body vs .tg-card-header).
  export let includeCity = false;

  const dispatch = createEventDispatcher<{ retry: void }>();

  function hideImg(e: Event): void {
    const img = e.currentTarget as HTMLImageElement;
    img.style.display = 'none';
  }
</script>

{#if includeCity && selectedPin.city}
  <div class="tg-card-city" data-testid="{testidPrefix}-city">{selectedPin.city}</div>
{/if}

{#if selectedPin.category === 'lodging' || !cardState}
  <!-- Lodging pins are city-centroid approximations — no specific place to query -->
  <div class="tg-card-approx" data-testid="{testidPrefix}-approx">
    Approximate location (city centre). Live place details aren't available for lodging.
  </div>
{:else if cardState.kind === 'loading'}
  <div class="tg-card-loading" data-testid="{testidPrefix}-loading">
    Loading live details…
  </div>
{:else if cardState.kind === 'ok'}
  {@const card = cardState.card}
  {#if card.photo_refs?.length}
    <img
      class="tg-card-photo"
      data-testid="{testidPrefix}-photo"
      src={placePhotoUrl(card.photo_refs[0])}
      alt={card.name ?? selectedPin.name}
      loading="lazy"
      on:error={hideImg}
    />
  {/if}
  {#if card.rating != null}
    <div class="tg-card-rating" data-testid="{testidPrefix}-rating">
      ★ {card.rating.toFixed(1)}{#if card.user_rating_count != null}&nbsp;({card.user_rating_count.toLocaleString()}){/if}
    </div>
  {/if}
  {#if card.open_now === true || card.open_now === false}
    <div class="tg-card-open" data-testid="{testidPrefix}-open">
      <span class="status-chip" class:tg-open={card.open_now} class:tg-closed={!card.open_now}>
        {card.open_now ? 'Open now' : 'Closed now'}
      </span>
      {#if card.opening_hours?.length}
        <details class="tg-card-hours">
          <summary>Hours</summary>
          {#each card.opening_hours as line}
            <div class="tg-card-hours-line">{line}</div>
          {/each}
        </details>
      {/if}
    </div>
  {/if}
  {#if card.reviews?.length}
    {@const rev = card.reviews[0]}
    {#if rev.text}
      <div class="tg-card-review" data-testid="{testidPrefix}-review">
        <span class="tg-card-review-text">
          {rev.text.length > 160 ? rev.text.slice(0, 157) + '…' : rev.text}
        </span>
        {#if rev.author_name}
          <span class="tg-card-review-author"> — {rev.author_name}</span>
        {/if}
        {#if rev.relative_time_description}
          <span class="tg-card-review-time"> · {rev.relative_time_description}</span>
        {/if}
      </div>
    {/if}
  {/if}
  <div class="tg-card-source">Live from Google Places</div>
{:else if cardState.kind === 'unavailable'}
  <div class="tg-card-unavailable" data-testid="{testidPrefix}-unavailable">
    <strong>Live details unavailable</strong>
    <p>We couldn't fetch live ratings/photos for this place right now.</p>
  </div>
{:else if cardState.kind === 'error'}
  <div class="tg-card-error" data-testid="{testidPrefix}-error">
    <strong>Couldn't load live details</strong>
    <button class="tg-card-retry" data-testid="{testidPrefix}-retry" on:click={() => dispatch('retry')}>Retry</button>
  </div>
{/if}

<style>
  /* Verbatim from Map.svelte's real .tg-card-* rules (unchanged values) — shared
     by both the desktop .tg-card and the mobile .pin-sheet, since only the outer
     container differs, not this inner content. */
  .tg-card-city { font-size: 11px; color: #7c7468; margin-top: 1px; margin-bottom: 6px; }
  .tg-card-approx, .tg-card-loading { color: #7c7468; font-style: italic; font-size: 12px; }
  .tg-card-photo {
    width: 100%; border-radius: 5px;
    margin-bottom: 8px; display: block;
    max-height: 140px; object-fit: cover;
  }
  .tg-card-rating { font-size: 13px; margin-bottom: 6px; color: #c98a2b; }
  .tg-card-open { font-size: 12px; margin-bottom: 6px; display: flex; align-items: center; gap: 6px; flex-wrap: wrap; }
  /* Status chip — same padding/radius/weight convention as RightRail .lvl / Aftercare .sev-chip */
  .status-chip { font-size: 11px; font-weight: 700; border-radius: 6px; padding: 2px 8px; }
  .tg-open { background: #e6f1e9; color: #4f8a63; }
  .tg-closed { background: #f8e4de; color: #c0563f; }
  .tg-card-hours { margin-top: 2px; }
  .tg-card-hours summary { cursor: pointer; font-size: 11px; color: #7c7468; }
  .tg-card-hours-line { font-size: 11px; color: #7c7468; }
  .tg-card-review {
    background: #faf6f0; border-radius: 4px;
    padding: 6px 8px; font-size: 12px; color: #2d2a26;
    margin-bottom: 6px; line-height: 1.4;
  }
  .tg-card-review-author { font-style: italic; color: #7c7468; }
  .tg-card-review-time { color: #998a78; }
  .tg-card-source { font-size: 10px; color: #998a78; text-align: right; margin-top: 4px; }
  .tg-card-unavailable { font-size: 12px; color: #7c7468; }
  .tg-card-unavailable p { margin: 4px 0 0; }
  .tg-card-error { font-size: 12px; color: #c0563f; display: flex; align-items: center; gap: 8px; }
  .tg-card-retry {
    background: #d9774a; color: #fff; border: none;
    border-radius: 8px; padding: 3px 8px; cursor: pointer; font-size: 11px;
  }
  .tg-card-retry:hover { background: #c4693e; }
</style>
