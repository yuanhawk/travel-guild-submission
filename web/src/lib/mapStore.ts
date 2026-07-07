import { writable } from 'svelte/store';
import type { MapPin } from './api';   // type-only import; no runtime cycle

/** Set this store to pan+zoom the shared MapLibre map to a specific location.
 *  Itinerary.svelte sets it on 📍 click; Map.svelte reacts with flyTo.
 *  RightRail.svelte switches to the map tab on any non-null value.
 *
 *  lat/lng/label pan the map; name/city/category (optional) let Map.svelte open the
 *  SAME place-detail card a direct pin click opens. When name/city are absent the
 *  consumer pans only (honest degradation — never a fabricated card). */
export interface MapFocus {
  lat: number;
  lng: number;
  label?: string;
  name?: string;                 // clean place name for /place_card {place}
  city?: string;                 // city for /place_card {city}
  category?: MapPin['category']; // 'restaurant' | 'attraction' — card fetch + pin colour
}

export const mapFocus = writable<MapFocus | null>(null);

/** True while Map.svelte's mobile place-detail bottom sheet (.pin-sheet, ≤768px)
 *  is on-screen. .pin-sheet is `position: fixed; bottom: 0` and z-index 315 —
 *  higher than ChatPane's floating .bot-trigger (300), so when both are visible
 *  the sheet fully covers the assistant bubble at the shared bottom-left/bottom
 *  edge (B5). App.svelte ORs this into ChatPane's `hideMobileBubble` prop, the
 *  same mechanism already used to keep Preview's .budget-bar from colliding with
 *  the bubble — see Preview.svelte's tech-note on hideMobileBubble. */
export const placeSheetOpen = writable<boolean>(false);

/** B5 spillover (#168, item 4 — found while auditing OTHER fixed-position overlays
 *  for the same "covers the floating assistant trigger" collision fix/assistant-
 *  bubble-overlap already resolved for Map.svelte's mobile pin-sheet): true while
 *  Itinerary.svelte's own item-thumb/leg-thumb photo lightbox (.lb-overlay,
 *  position:fixed, inset:0, z-index 9999) is on-screen. Verified live (not just by
 *  CSS inspection) via document.elementFromPoint at the assistant trigger's screen
 *  coordinates + an actual Playwright click attempt: the lightbox DOES sit on top
 *  and DOES intercept the click, unlike SafetyWatch's overlay (z-index 50, well
 *  below the trigger's 300) which does not. Not map-specific, but colocated with
 *  `mapFocus` for the exact same reason: a single small store any top-level
 *  fixed-position overlay can flip so App.svelte can OR it into ChatPane's
 *  `hideMobileBubble`, same seam as `placeSheetOpen` above, no parallel mechanism. */
export const itineraryLightboxOpen = writable<boolean>(false);
