<script lang="ts">
  import { onMount, onDestroy, createEventDispatcher } from 'svelte';
  import maplibregl from 'maplibre-gl';
  import 'maplibre-gl/dist/maplibre-gl.css';
  import type { MapPin } from '../api';
  import { placeCard } from '../api';
  import { mapFocus, placeSheetOpen } from '../mapStore';
  import { hasNonLatinScript } from '../itinerary';
  import PinDetailContent from './PinDetailContent.svelte';
  import type { CardState } from './PinDetailContent.svelte';

  export let pins: MapPin[] = [];

  const dispatch = createEventDispatcher<{ pinclick: MapPin }>();
  let container: HTMLDivElement;
  let map: maplibregl.Map | undefined;
  let markers: maplibregl.Marker[] = [];
  let ready = false;

  // ── place-card overlay state ──────────────────────────────────────────────
  // Shared by BOTH containers below: the desktop floating .tg-card and the
  // mobile slide-up .pin-sheet render the SAME selectedPin/cardState via
  // <PinDetailContent> — only the container differs (mockups/image-placement-
  // draft1.html Item 2). No new fetch path, no new state.
  let selectedPin: MapPin | null = null;
  let cardState: CardState | null = null;
  let reqSeq = 0; // guards out-of-order responses on fast pin switches

  // Free OSM raster base — no API key. Attribution is required + shown.
  // (Phase 3: swap to a vector style / Places-API rich cards on pin click.)
  const STYLE: maplibregl.StyleSpecification = {
    version: 8,
    sources: {
      osm: {
        type: 'raster',
        tiles: ['https://tile.openstreetmap.org/{z}/{x}/{y}.png'],
        tileSize: 256,
        attribution: '© OpenStreetMap contributors',
      },
    },
    layers: [{ id: 'osm', type: 'raster', source: 'osm' }],
  };

  const CAT_COLOR: Record<string, string> = {
    lodging: '#e95f26',
    restaurant: '#a5653b',
    attraction: '#847059',
    transit: '#3b393b',
  };

  onMount(() => {
    map = new maplibregl.Map({
      container,
      style: STYLE,
      center: [10, 25],
      zoom: 1.3,
    });
    map.addControl(new maplibregl.NavigationControl({ showCompass: false }), 'top-right');
    map.on('load', () => { ready = true; renderPins(); });
  });

  // B5 fix: the sheet only actually renders/overlaps on mobile (Map.svelte's own
  // ≤768px media query), but this component only exists while the Map tab is
  // mounted, so it's safe/cheap to keep the store synced unconditionally — desktop
  // widths just never read it as true in a way that hides anything (hideMobileBubble's
  // CSS effect is itself media-gated).
  $: placeSheetOpen.set(selectedPin !== null);

  onDestroy(() => {
    map?.remove();
    // Reset so switching away from the Map tab (which unmounts this component —
    // RightRail's tab panes are {#if}-gated) doesn't leave the assistant bubble
    // hidden forever on a stale "sheet open" signal.
    placeSheetOpen.set(false);
  });

  // Finding #7 (map-pin-bug sweep): two or more pins can carry the EXACT same
  // lat/lng (observed live: "A Baiuca" / "Alcaçarias do Duque" in Lisbon) — MapLibre
  // stacks their marker elements directly on top of each other, and only the
  // topmost (last-added) one ever receives pointer events, so the lower pin was
  // silently unclickable. Rather than dropping/merging data, spread exact
  // duplicates onto a tiny circle around their shared point (~13m radius — well
  // under "same place" at any zoom level a traveler would read the map at) so
  // EVERY pin stays independently clickable. Only the on-map marker position is
  // jittered; the pin object dispatched to pinclick/openCard (and used for
  // fitBounds) is otherwise untouched, so /place_card lookups (keyed on
  // name+city, not coordinates) are unaffected.
  function jitteredPositions(valid: MapPin[]): { p: MapPin; lat: number; lng: number }[] {
    const groups = new Map<string, MapPin[]>();
    for (const p of valid) {
      const key = `${p.lat.toFixed(5)},${p.lng.toFixed(5)}`;
      const g = groups.get(key);
      if (g) g.push(p); else groups.set(key, [p]);
    }
    const out: { p: MapPin; lat: number; lng: number }[] = [];
    const r = 0.00012; // ~13m
    for (const group of groups.values()) {
      if (group.length === 1) { out.push({ p: group[0], lat: group[0].lat, lng: group[0].lng }); continue; }
      group.forEach((p, i) => {
        const angle = (2 * Math.PI * i) / group.length;
        out.push({ p, lat: p.lat + r * Math.sin(angle), lng: p.lng + r * Math.cos(angle) });
      });
    }
    return out;
  }

  function renderPins(): void {
    if (!map || !ready) return;
    markers.forEach((m) => m.remove());
    markers = [];
    const valid = pins.filter((p) => Number.isFinite(p.lat) && Number.isFinite(p.lng));
    if (!valid.length) return;
    const bounds = new maplibregl.LngLatBounds();
    for (const { p, lat, lng } of jitteredPositions(valid)) {
      const el = document.createElement('div');
      el.className = 'tg-pin';
      el.style.background = CAT_COLOR[p.category ?? ''] ?? '#e95f26';
      el.title = p.label ?? '';
      el.addEventListener('click', () => { dispatch('pinclick', p); openCard(p); });
      markers.push(new maplibregl.Marker({ element: el }).setLngLat([lng, lat]).addTo(map!));
      bounds.extend([lng, lat]);
    }
    if (valid.length === 1) map.flyTo({ center: [valid[0].lng, valid[0].lat], zoom: 11, duration: 600 });
    else map.fitBounds(bounds, { padding: 60, maxZoom: 12, duration: 600 });
  }

  // re-render whenever pins change after the style is loaded
  $: if (ready) { void pins; renderPins(); }

  // #126: fly to location when mapFocus store is updated, then CONSUME it (set null).
  // One-shot: prevents the focus going stale across a new trip/plan (RightRail has already
  // switched to the map tab by the time this fires; resetting won't bounce the tab back).
  $: if ($mapFocus != null && map && ready) {
    const mf = $mapFocus;
    map.flyTo({ center: [mf.lng, mf.lat], zoom: 14, duration: 800 });
    // Parity with a direct pin click: open the shared detail card — but ONLY when we
    // have a real place to query. Honest degradation: no name/city → pan only, and
    // clear any stale card so we never show a mismatched popup.
    if (mf.name && mf.city && mf.category !== 'lodging') {
      void openCard({ lat: mf.lat, lng: mf.lng, label: mf.label,
                      name: mf.name, city: mf.city, category: mf.category });
    } else {
      closeCard();
    }
    mapFocus.set(null);   // one-shot consume — unchanged
  }

  // ── place card open/close ─────────────────────────────────────────────────
  async function openCard(p: MapPin): Promise<void> {
    selectedPin = p;
    // Lodging pins are city-centroid approximations — no specific place to query.
    if (!p.name || !p.city || p.category === 'lodging') { cardState = null; return; }
    const seq = ++reqSeq;
    cardState = { kind: 'loading' };
    try {
      const c = await placeCard(p.name, p.city); // cached per (name,city)
      if (seq !== reqSeq) return; // a newer click won — drop
      cardState = c.status === 'ok' ? { kind: 'ok', card: c } : { kind: 'unavailable' };
    } catch {
      if (seq !== reqSeq) return;
      cardState = { kind: 'error' };
    }
  }

  // (G4, #181) MapPin.name is ALREADY the collapsed displayName() primary (see
  // pinOf() in itinerary.ts: `name: displayName(x)`) — the raw `name`/`name_en`
  // pair used by namePresentation() elsewhere never survives into MapPin, so this
  // component can't reconstruct the local-companion-script or verified/romanized
  // badge tiers without a materially bigger change (threading name_en/name_source
  // through MapPin + every pinOf() call site). That part is a documented, known
  // minor gap — NOT fixed here. What IS reproduced cleanly: the same "unreadable
  // primary" signal (non-Latin script with no readable fallback) the main timeline
  // shows, computed off the same already-collapsed string via the shared
  // hasNonLatinScript() helper — a true positive whenever no name_en existed to
  // collapse to in the first place.
  function pinUnreadable(p: MapPin | null): boolean {
    return !!p?.name && hasNonLatinScript(p.name);
  }

  function closeCard(): void { selectedPin = null; cardState = null; }

  function retryCard(): void {
    if (selectedPin) void openCard(selectedPin);
  }
</script>

<!-- Finding #4 (map-pin-bug sweep): .tg-card is `position:absolute`, which resolves
     against the nearest POSITIONED ancestor. Map.svelte's top-level markup used to
     put .tg-map and .tg-card as plain siblings with no positioned wrapper of their
     own, so on pages where the nearest positioned ancestor happened to be the
     sticky right rail, the card anchored to the RAIL (covering the tab bar) instead
     of the map beneath it. This wrapper is the map's own positioning context. -->
<div class="tg-map-wrap">
  <div class="tg-map" bind:this={container}></div>

  {#if selectedPin}
    <!-- Desktop floating popup — UNCHANGED (mockups/image-placement-draft1.html Item 2:
         "keep the floating .tg-card popup completely unchanged on desktop"). Hidden
         ≤768px via CSS only; the .pin-sheet below takes over on mobile. -->
    <div class="tg-card" data-testid="place-card">
      <button class="tg-card-x" data-testid="place-card-close" on:click={closeCard} aria-label="Close">×</button>
      <div class="tg-card-header" style="border-color: {CAT_COLOR[selectedPin.category ?? ''] ?? '#e95f26'}">
        <div class="tg-card-title">{selectedPin.name ?? selectedPin.label}</div>
        {#if pinUnreadable(selectedPin)}<div class="tg-card-unreadable" title="No English name available">shown in original script — no English name available</div>{/if}
        {#if selectedPin.city}
          <div class="tg-card-city">{selectedPin.city}</div>
        {/if}
      </div>
      <PinDetailContent {selectedPin} {cardState} on:retry={retryCard} />
    </div>
  {/if}
</div>

<!-- Mobile-only slide-up bottom sheet (≤768px) — same 768px breakpoint as
     ChatPane's hideMobileBubble / Preview's budget-bar. SAME content/state
     machine as .tg-card above via <PinDetailContent>, only the container
     changes (mockups/image-placement-draft1.html Item 2). Applies to every
     pin category identically, same as .tg-card — lodging pins already collapse
     to the text-only "Approximate location" state inside PinDetailContent, so
     no separate category gate is needed here.
     Always mounted (like ChatPane's .mob-sheet / Preview's .budget-sheet) so
     CSS can transition `transform` on open/close rather than snapping via #if. -->
<div class="pin-sheet" class:ps-open={!!selectedPin} aria-hidden={!selectedPin} data-testid="place-sheet">
  {#if selectedPin}
    <div class="ps-hdr">
      <span class="ps-drag" aria-hidden="true"></span>
      <span class="ps-ttl">{selectedPin.name ?? selectedPin.label}</span>
      <button class="ps-close" data-testid="place-sheet-close" on:click={closeCard} aria-label="Close">▼</button>
    </div>
    {#if pinUnreadable(selectedPin)}<div class="ps-unreadable" title="No English name available">shown in original script — no English name available</div>{/if}
    <div class="ps-body">
      <PinDetailContent {selectedPin} {cardState} testidPrefix="place-sheet" includeCity on:retry={retryCard} />
    </div>
  {/if}
</div>

<style>
  /* Finding #4 fix: the positioning context for the floating .tg-card (and
     nothing else — .pin-sheet below is `position:fixed`, viewport-relative,
     unaffected by this). Deliberately a plain block (no height:100% of its
     own) so it keeps taking its size from .tg-map's height, same as before
     this wrapper existed (e.g. RightRail's `.mappane :global(.tg-map){height:
     280px}` still governs the visible map/card area unchanged). */
  .tg-map-wrap { position: relative; width: 100%; }
  .tg-map { width: 100%; height: 100%; min-height: 340px; position: relative; }
  :global(.tg-pin) {
    width: 14px; height: 14px; border-radius: 50%;
    border: 2px solid #fff; cursor: pointer;
    box-shadow: 0 1px 3px rgba(0, 0, 0, 0.4);
  }

  /* ── place card overlay ──
     Matches the app-wide floating-card convention (see ChatPane .flyout): warm
     off-white surface, hairline border, soft dual-layer shadow, 14px radius. */
  .tg-card {
    position: absolute;
    top: 12px; right: 12px;
    width: 260px;
    max-height: calc(100% - 24px);
    overflow-y: auto;
    background: #fff;
    border: 1px solid #ece2d5;
    border-radius: 14px;
    box-shadow: 0 8px 32px rgba(0, 0, 0, 0.16), 0 2px 8px rgba(0, 0, 0, 0.08);
    padding: 12px;
    z-index: 10;
    font-size: 13px;
    color: #2d2a26;
  }
  .tg-card-x {
    position: absolute; top: 6px; right: 8px;
    background: none; border: none; cursor: pointer;
    font-size: 18px; color: #7c7468; line-height: 1;
    padding: 0;
  }
  .tg-card-x:hover { color: #2d2a26; }
  .tg-card-header {
    border-left: 3px solid #e95f26;
    padding-left: 8px;
    margin-bottom: 8px;
    padding-right: 18px; /* room for × */
  }
  .tg-card-title { font-weight: 600; font-size: 14px; line-height: 1.3; }
  .tg-card-city { font-size: 11px; color: #7c7468; margin-top: 1px; }
  /* (G4, #181) same "unreadable primary" honesty indicator as the itinerary
     timeline's .name-src-badge.unreadable — own neutral-grey block treatment
     since this sits in the card header, not inline in a chip row. */
  .tg-card-unreadable, .ps-unreadable {
    font-size: 10.5px; font-weight: 600; color: #7c7468;
    background: #f1eee7; border-radius: 5px; padding: 3px 7px; margin-top: 4px;
  }
  .ps-unreadable { margin: 0 16px 8px; }
  /* All other .tg-card-* body/state rules (approx/loading/photo/rating/open/status-chip/
     hours/review/source/unavailable/error/retry) now live in PinDetailContent.svelte,
     shared verbatim by this desktop card AND the mobile .pin-sheet below. */

  /* Desktop keeps the floating popup; mobile swaps to the slide-up sheet below
     (mockups/image-placement-draft1.html Item 2 — same 768px breakpoint as
     ChatPane's hideMobileBubble / Preview's budget-bar/-sheet). */
  @media (max-width: 768px) {
    .tg-card { display: none; }
  }

  /* ── mobile pin-detail slide-up sheet ──
     Radius/shadow/transform/transition/65vh cap copied verbatim from ChatPane's
     .mob-sheet / Preview's .budget-sheet (both real, already-shipped precedents
     for this exact pattern) — see mockup Item 2 "Sheet styling precedent". Always
     mounted (like both precedents) so `transform` can transition on open/close;
     hidden entirely on desktop. */
  .pin-sheet { display: none; }
  @media (max-width: 768px) {
    .pin-sheet {
      display: flex;
      position: fixed;
      left: 0; right: 0; bottom: 0;
      max-height: 65vh;
      background: #fff;
      border-radius: 16px 16px 0 0;
      box-shadow: 0 -4px 28px rgba(0, 0, 0, 0.16);
      z-index: 315;
      flex-direction: column;
      overflow: hidden;
      transform: translateY(100%);
      transition: transform .28s cubic-bezier(.22, .61, .36, 1);
    }
    .pin-sheet.ps-open { transform: translateY(0); }

    .ps-hdr {
      display: flex; align-items: center; justify-content: space-between;
      padding: 12px 16px 10px;
      border-bottom: 1px solid #ece2d5;
      background: #faf6f0;
      flex-shrink: 0;
      position: relative;
    }
    .ps-drag {
      width: 34px; height: 4px; border-radius: 3px; background: #ddd0c2;
      position: absolute; top: 6px; left: 50%; transform: translateX(-50%);
    }
    .ps-ttl { font-size: 13px; font-weight: 700; color: #2d2a26; letter-spacing: .3px; }
    .ps-close {
      border: none; background: #ede4da; border-radius: 8px;
      width: 28px; height: 28px; cursor: pointer; font-size: 12px;
      display: flex; align-items: center; justify-content: center; color: #7c7468;
    }
    .ps-body { flex: 1; overflow-y: auto; -webkit-overflow-scrolling: touch; padding: 10px 16px 16px; }
  }
</style>
