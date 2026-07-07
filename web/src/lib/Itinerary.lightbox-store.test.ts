// @vitest-environment jsdom
//
// COMPONENT test — #168 B5 spillover, item 4 (found while auditing OTHER fixed-position
// overlays for the same "covers the floating assistant trigger" collision
// fix/assistant-bubble-overlap already resolved for Map.svelte's mobile pin-sheet).
//
// Itinerary.svelte's own item-thumb/leg-thumb photo lightbox (.lb-overlay,
// position:fixed, inset:0, z-index 9999) has the identical collision on mobile —
// confirmed live via document.elementFromPoint + an actual Playwright click attempt
// (see e2e/place-photos.spec.ts's "mobile: item-thumb lightbox does not cover the
// assistant trigger" describe block for the user-facing regression test). This file
// locks the underlying store contract at the component level: itineraryLightboxOpen
// (src/lib/mapStore.ts) tracks lightbox open/close, and — since .lb-overlay is a
// full-screen modal with no UI-reachable "leave without closing" path (see
// place-photos.spec.ts's NOTE) — the onDestroy reset is only testable here, not via E2E.

import { describe, it, expect, beforeEach } from 'vitest';
import { get } from 'svelte/store';
import { render } from '@testing-library/svelte';
import Itinerary from './components/Itinerary.svelte';
import { itineraryLightboxOpen } from './mapStore';
import type { DayPlan, Leg } from './api';

function planWithPhotoActivity(): DayPlan[] {
  return [{
    leg_id: 'leg-0',
    city: 'Tokyo',
    country: 'Japan',
    num_days: 1,
    days: [{
      day_index: 0, bad_weather: false,
      attractions: [{ name: 'Fushimi Inari', category: 'tourism=shrine', lat: 1, lon: 1 }],
      meals: {},
    }],
  }] as unknown as DayPlan[];
}

describe('#168 B5 spillover: itineraryLightboxOpen store lifecycle', () => {
  // itineraryLightboxOpen is a module-level singleton store shared across tests in this
  // file (and, in the real app, across every mounted consumer) — reset it explicitly so
  // test order/isolation doesn't depend on the previous test's cleanup behavior.
  beforeEach(() => itineraryLightboxOpen.set(false));

  it('starts false (no lightbox open on initial render)', () => {
    render(Itinerary, {
      props: { plans: planWithPhotoActivity(), legs: [{ leg_id: 'leg-0', city: 'Tokyo' }] as unknown as Leg[], idempotencyKey: '' },
    });
    expect(get(itineraryLightboxOpen)).toBe(false);
  });

  it('resets to false on unmount even if a lightbox happened to be left open', async () => {
    // Directly exercises the store the same way openLightbox() does (component internals
    // aren't otherwise reachable without a live /place_card fetch + thumbnail click in
    // this render) — the behavior under test is the onDestroy guard itself, which fires
    // regardless of how the store got set to true.
    itineraryLightboxOpen.set(true);
    const { unmount } = render(Itinerary, {
      props: { plans: planWithPhotoActivity(), legs: [{ leg_id: 'leg-0', city: 'Tokyo' }] as unknown as Leg[], idempotencyKey: '' },
    });
    unmount();
    expect(get(itineraryLightboxOpen)).toBe(false);
  });
});
