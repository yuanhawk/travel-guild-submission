<script lang="ts">
  /** Photo strip for a named place — calls /place_card on demand (user-triggered).
   *  Click 📷 → show 1 full-width photo + meta. Click "Show more" → expand all.
   *  Click any photo → lightbox overlay for full-size view.
   *
   *  variant="banner" (mockups/image-placement-draft1.html Item 1 tech-note): eager,
   *  chrome-less mode for Preview.svelte's leg-hero band — fetches on mount instead of
   *  on click, renders only photos[0] full-width via .ph-banner, and fails COMPLETELY
   *  silent (no "No photos available" text) since an always-on banner showing an empty-
   *  state message would read as a visible defect, not an honest absence. */
  import { onMount } from 'svelte';
  import { API_BASE } from '../api';

  export let name: string;
  export let city: string = '';
  export let country: string = '';
  export let variant: 'toggle' | 'banner' = 'toggle';

  let open = false;
  let showAll = false;
  let loading = false;
  let photos: string[] = [];
  let rating: number | null = null;
  let reviewCount: number | null = null;
  let openNow: boolean | null = null;
  let fetched = false;
  let lightboxSrc: string | null = null;

  async function fetchPhotos() {
    if (fetched) return;
    loading = true;
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
            (p: string) => p.startsWith('http') ? p : `${API_BASE}${p}`
          );
          rating = d.place.rating ?? null;
          reviewCount = d.place.user_rating_count ?? null;
          openNow = d.place.open_now ?? null;
        }
      }
    } catch {
      // fail silently — photos are display-only
    } finally {
      loading = false;
      fetched = true;
    }
  }

  function toggle() {
    open = !open;
    if (!open) { showAll = false; return; }
    fetchPhotos();
  }

  function openLightbox(src: string) { lightboxSrc = src; }
  function closeLightbox() { lightboxSrc = null; }

  $: visiblePhotos = showAll ? photos : photos.slice(0, 1);

  // banner: eager fetch on mount, no click needed — one fetch per mounted instance,
  // matching the toggle variant's "fetched" guard so a re-mount never double-fetches.
  onMount(() => {
    if (variant === 'banner') fetchPhotos();
  });
</script>

<!-- Lightbox overlay — rendered at top of component so it escapes card stacking context -->
{#if lightboxSrc}
  <!-- svelte-ignore a11y-click-events-have-key-events a11y-no-static-element-interactions -->
  <div class="lb-overlay" on:click={closeLightbox}>
    <button class="lb-close" on:click={closeLightbox} aria-label="Close photo">✕</button>
    <!-- svelte-ignore a11y-click-events-have-key-events a11y-no-noninteractive-element-interactions -->
    <img src={lightboxSrc} alt={name} class="lb-img" on:click|stopPropagation />
  </div>
{/if}

{#if variant === 'banner'}
  <!-- Banner mode (mockups/image-placement-draft1.html Item 1): no trigger, no toggle
       chrome, no loading/empty-state text — renders photos[0] once fetched, or nothing
       at all on failure/empty result (never a placeholder or broken-image glyph). -->
  {#if photos.length}
    <img src={photos[0]} alt={name} class="ph-banner" data-testid="leg-hero-banner"
      loading="lazy" decoding="async" />
  {/if}
{:else}
<div class="ph-wrap">
  <button class="ph-btn" on:click|stopPropagation={toggle}
    title={open ? 'Hide photos' : 'View photos'}
    aria-label={open ? `Hide photos for ${name}` : `View photos for ${name}`}>
    {#if open}▲ hide{:else}📷 Photo{/if}
  </button>

  {#if open}
    <div class="ph-panel">
      {#if loading}
        <div class="ph-shimmer"></div>
      {:else if photos.length}
        <div class="ph-gallery">
          {#each visiblePhotos as src, i}
            <button class="ph-img-btn" on:click|stopPropagation={() => openLightbox(src)}
              aria-label="View full size photo {i + 1} of {name}">
              <img {src} alt={name} class="ph-img" loading="lazy" decoding="async" />
            </button>
          {/each}
        </div>

        {#if !showAll && photos.length > 1}
          <button class="ph-more" on:click|stopPropagation={() => { showAll = true; }}>
            Show {photos.length - 1} more photo{photos.length > 2 ? 's' : ''}
          </button>
        {/if}

        <div class="ph-meta">
          {#if rating !== null}
            <span class="ph-rating">⭐ {rating.toFixed(1)}</span>
            {#if reviewCount}<span class="ph-count">({reviewCount.toLocaleString()} reviews)</span>{/if}
          {/if}
          {#if openNow !== null}
            <span class="ph-status" class:open-now={openNow}>{openNow ? 'Open now' : 'Closed'}</span>
          {/if}
        </div>
      {:else}
        <span class="ph-none">No photos available</span>
      {/if}
    </div>
  {/if}
</div>
{/if}

<style>
  /* ── banner mode (mockups/image-placement-draft1.html Item 1: .leg-hero /
     "Leg-hero sizing" decision) — full column width, object-fit: cover,
     border-radius 12px (matching .budget-card, the higher end of the app's
     10-12px radius range), ~130px desktop / ~140px mobile via the same
     768px breakpoint the rest of Preview.svelte's mobile rules use. ── */
  .ph-banner {
    display: block; width: 100%; height: 130px; object-fit: cover;
    border-radius: 12px; border: 1px solid #ece2d5; background: #f4ede3;
    margin: 0 0 12px;
  }
  @media (max-width: 768px) {
    .ph-banner { height: 140px; }
  }

  /* ── trigger button ─────────────────────────────────────────────────── */
  .ph-wrap { display: block; margin-top: 4px; }
  .ph-btn {
    border: 1px solid #e0d5c8; background: #faf6f0; border-radius: 5px;
    cursor: pointer; font-size: 12px; padding: 2px 8px; color: #7c7468;
    line-height: 1.6;
  }
  .ph-btn:hover { background: #f4ede3; border-color: #c9a882; color: #5a4e44; }

  /* ── expanded panel ─────────────────────────────────────────────────── */
  .ph-panel { margin-top: 6px; }

  /* ── photo gallery ──────────────────────────────────────────────────── */
  .ph-gallery { display: flex; flex-direction: column; gap: 6px; }
  .ph-img-btn {
    display: block; padding: 0; border: none; background: none;
    cursor: zoom-in; border-radius: 8px; overflow: hidden;
    width: 100%;
  }
  .ph-img {
    display: block; width: 100%; height: 180px;
    object-fit: cover; border-radius: 8px;
    border: 1px solid #ece2d5; background: #f4ede3;
    transition: opacity 0.15s;
  }
  .ph-img-btn:hover .ph-img { opacity: 0.92; }

  /* ── shimmer ────────────────────────────────────────────────────────── */
  .ph-shimmer {
    width: 100%; height: 180px; border-radius: 8px;
    background: linear-gradient(90deg, #f4ede3 25%, #e8ddd0 50%, #f4ede3 75%);
    background-size: 200% 100%;
    animation: ph-shimmer 1.4s infinite;
  }
  @keyframes ph-shimmer {
    0%   { background-position: 200% 0; }
    100% { background-position: -200% 0; }
  }

  /* ── show-more button ───────────────────────────────────────────────── */
  .ph-more {
    display: block; width: 100%; margin-top: 4px;
    border: 1px dashed #ddd3c4; background: #faf6f0; border-radius: 6px;
    cursor: pointer; font-size: 12px; color: #7c7468; padding: 6px 0;
    text-align: center;
  }
  .ph-more:hover { background: #f4ede3; border-color: #c9a882; color: #5a4e44; }

  /* ── meta row ───────────────────────────────────────────────────────── */
  .ph-meta {
    display: flex; gap: 6px; align-items: center; flex-wrap: wrap;
    font-size: 11px; color: #7c7468; margin-top: 5px;
  }
  .ph-rating { font-weight: 700; color: #c98a2b; }
  .ph-count { color: #9e9288; }
  .ph-status {
    font-size: 10px; font-weight: 700; border-radius: 4px;
    padding: 1px 5px; background: #fde8e8; color: #b84040;
  }
  .ph-status.open-now { background: #e8f5ec; color: #2f6b46; }
  .ph-none { font-size: 11px; color: #c4b5a8; font-style: italic; }

  /* ── lightbox overlay ───────────────────────────────────────────────── */
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
