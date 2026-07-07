// @vitest-environment jsdom
/// <reference types="@testing-library/jest-dom" />
//
// #110 — COMPONENT test for <Map> (MapLibre place-pin map + Places detail card).
//
// MapLibre needs WebGL, which jsdom lacks. We DO NOT render the GL map or emulate
// WebGL: the `maplibre-gl` module is replaced by a thin no-op double (a clean library
// mock — Map/Marker/LngLatBounds/NavigationControl), so the component's onMount runs
// without a real canvas. We then assert ONLY the non-WebGL logic that is the component's
// actual job:
//   • props -> pin/marker DERIVATION (finite-coord filter, [lng,lat] order, category colour);
//   • pin click -> `pinclick` dispatch + place-card OPEN;
//   • place-card open/close STATE;
//   • honest states: lodging "approximate" (no query), loading, ok, unavailable, error+retry;
//   • the out-of-order reqSeq guard on fast pin switches.
//
// The /place_card network contract itself is covered separately in placecard.test.ts; here
// we mock only that one seam and keep placePhotoUrl (and every other api export) real.

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, fireEvent } from '@testing-library/svelte';
import { tick } from 'svelte';
import type { MapPin } from './api';

// ── maplibre-gl double — captures created markers; fires 'load' synchronously ──────────
const store = vi.hoisted(() => ({ markers: [] as MarkerDouble[], flyToCalls: [] as unknown[] }));
interface MarkerDouble {
  element: HTMLElement;
  lngLat: [number, number] | null;
  removed: boolean;
  setLngLat(ll: [number, number]): MarkerDouble;
  addTo(): MarkerDouble;
  remove(): void;
}

vi.mock('maplibre-gl', () => {
  class Map {
    constructor(_o: unknown) {}
    addControl() { return this; }
    on(ev: string, cb: () => void) { if (ev === 'load') cb(); return this; } // load fires now
    remove() {}
    flyTo(o: unknown) { store.flyToCalls.push(o); }
    fitBounds() {}
  }
  class Marker {
    element: HTMLElement;
    lngLat: [number, number] | null = null;
    removed = false;
    constructor(o: { element: HTMLElement }) { this.element = o.element; store.markers.push(this as unknown as MarkerDouble); }
    setLngLat(ll: [number, number]) { this.lngLat = ll; return this; }
    addTo() { return this; }
    remove() { this.removed = true; }
  }
  class LngLatBounds { extend() {} }
  class NavigationControl { constructor(_o: unknown) {} }
  return { default: { Map, Marker, LngLatBounds, NavigationControl } };
});
// CSS side-import — return an empty module so resolution never matters in jsdom.
vi.mock('maplibre-gl/dist/maplibre-gl.css', () => ({}));

// ── the one network seam ──────────────────────────────────────────────────────────────
vi.mock('./api', async (orig) => {
  const actual = await (orig() as Promise<Record<string, unknown>>);
  return { ...actual, placeCard: vi.fn() };
});
import { placeCard } from './api';
import MapComponent from './components/Map.svelte';
import { get } from 'svelte/store';
import { mapFocus } from './mapStore';
const placeCardMock = placeCard as unknown as ReturnType<typeof vi.fn>;

// Live (not-yet-removed) markers, in creation order. The component re-renders pins on the
// reactive pass (remove-then-recreate), so we filter out the superseded set.
const live = () => store.markers.filter((m) => !m.removed);

beforeEach(() => {
  store.markers.length = 0;
  store.flyToCalls.length = 0;
  placeCardMock.mockReset();
  placeCardMock.mockResolvedValue({ status: 'unavailable' });
  mapFocus.set(null);
});
afterEach(() => vi.clearAllMocks());

function mount(pins: MapPin[]) {
  return render(MapComponent, { props: { pins } });
}

// ── 1. pin/marker derivation — finite-coord filter + [lng,lat] order ──────────────────
describe('Map — pin → marker derivation', () => {
  it('creates one marker per finite-coord pin and drops NaN/Infinity coords', async () => {
    mount([
      { lat: 35.0, lng: 135.7, label: 'Kyoto', category: 'attraction' },
      { lat: NaN, lng: 139.7, label: 'bad-lat', category: 'attraction' },
      { lat: 1.3, lng: Infinity, label: 'bad-lng', category: 'lodging' },
      { lat: 48.85, lng: 2.35, label: 'Paris', category: 'restaurant' },
    ]);
    await tick();
    const m = live();
    expect(m.length).toBe(2); // only the two finite pins
  });

  it('passes coordinates to the marker as [lng, lat] (not [lat, lng])', async () => {
    mount([{ lat: 35.0, lng: 135.7, label: 'Kyoto', category: 'attraction' }]);
    await tick();
    expect(live()[0].lngLat).toEqual([135.7, 35.0]);
  });

  it('renders no markers when no pin has finite coords', async () => {
    const { queryByTestId } = mount([{ lat: NaN, lng: NaN, label: 'nowhere' }]);
    await tick();
    expect(live().length).toBe(0);
    expect(queryByTestId('place-card')).toBeNull();
  });

  // REGRESSION (Finding #7, map-pin-bug sweep): pins sharing the EXACT same lat/lng
  // (e.g. two restaurants geocoded to the same building) used to stack their marker
  // elements directly on top of one another — only the topmost ever received clicks,
  // so the lower pin was permanently, silently unclickable. Duplicates must now be
  // spread onto distinct nearby positions so every pin is independently clickable.
  it('spreads pins sharing the exact same lat/lng onto distinct nearby positions', async () => {
    mount([
      { lat: 38.7, lng: -9.14, label: 'A Baiuca', category: 'restaurant' },
      { lat: 38.7, lng: -9.14, label: 'Alcaçarias do Duque', category: 'restaurant' },
    ]);
    await tick();
    const [a, b] = live();
    expect(a.lngLat).not.toBeNull();
    expect(b.lngLat).not.toBeNull();
    // Distinct on-map positions (neither marker is hidden directly under the other)...
    expect(a.lngLat).not.toEqual(b.lngLat);
    // ...but still close enough to read as "the same place" (well under 100m).
    const [lngA, latA] = a.lngLat!;
    const [lngB, latB] = b.lngLat!;
    expect(Math.abs(lngA - lngB)).toBeLessThan(0.001);
    expect(Math.abs(latA - latB)).toBeLessThan(0.001);
  });

  it('a single pin keeps its exact coordinates (no jitter applied to non-duplicates)', async () => {
    mount([{ lat: 35.0, lng: 135.7, label: 'Kyoto', category: 'attraction' }]);
    await tick();
    expect(live()[0].lngLat).toEqual([135.7, 35.0]);
  });
});

// ── 2. category → marker colour + title ───────────────────────────────────────────────
describe('Map — marker colour + title from category', () => {
  it('distinct categories get distinct colours; unknown category falls back to the lodging default', async () => {
    mount([
      { lat: 1, lng: 1, category: 'lodging', label: 'L' },
      { lat: 2, lng: 2, category: 'restaurant', label: 'R' },
      { lat: 3, lng: 3, category: 'space-elevator', label: 'U' }, // unknown → default
    ]);
    await tick();
    const [lodging, restaurant, unknown] = live().map((m) => m.element.style.background);
    expect(restaurant).not.toBe(lodging);            // categories are colour-coded
    expect(unknown).toBe(lodging);                    // default === lodging colour (#e95f26)
  });

  it('sets the marker tooltip from the pin label', async () => {
    mount([{ lat: 1, lng: 1, category: 'attraction', label: 'Senso-ji' }]);
    await tick();
    expect(live()[0].element.title).toBe('Senso-ji');
  });
});

// ── 3. pin click → dispatch + card open ───────────────────────────────────────────────
describe('Map — pin click opens the place card and dispatches pinclick', () => {
  it('clicking a marker dispatches `pinclick` with the pin and shows the card titled by name', async () => {
    const pin: MapPin = { lat: 35.7, lng: 139.8, name: 'Senso-ji', city: 'Tokyo', category: 'attraction', label: 'Senso-ji Temple' };
    placeCardMock.mockResolvedValue({ status: 'ok', rating: 4.6 });
    const { component, getByTestId, findByTestId } = mount([pin]);
    const got: MapPin[] = [];
    component.$on('pinclick', (e) => got.push((e as CustomEvent<MapPin>).detail));
    await tick();

    await fireEvent.click(live()[0].element);
    await findByTestId('place-card');

    expect(got).toEqual([pin]);                       // exact pin object dispatched upward
    expect(getByTestId('place-card')).toHaveTextContent('Senso-ji');
    expect(placeCardMock).toHaveBeenCalledWith('Senso-ji', 'Tokyo');
  });

  it('close button tears the card down', async () => {
    placeCardMock.mockResolvedValue({ status: 'ok', rating: 4.0 });
    const { getByTestId, findByTestId, queryByTestId } = mount([
      { lat: 1, lng: 1, name: 'A', city: 'C', category: 'attraction' },
    ]);
    await tick();
    await fireEvent.click(live()[0].element);
    await findByTestId('place-card');
    await fireEvent.click(getByTestId('place-card-close'));
    expect(queryByTestId('place-card')).toBeNull();
  });

  // REGRESSION (Finding #4, map-pin-bug sweep): .tg-card is `position:absolute`, so it
  // anchors to its nearest POSITIONED ancestor. .tg-card and .tg-map used to be plain
  // sibling elements with no positioned wrapper of their own, so on any host page where
  // the nearest positioned ancestor happened to be something else (e.g. a sticky right
  // rail), the card would anchor to THAT instead of the map — covering unrelated UI
  // (tab bar) rather than floating over the map. jsdom can't compute real layout/
  // offsetParent, but it CAN assert the DOM containment this depends on: the card must
  // live inside Map.svelte's own wrapper, not float free as a sibling of it.
  it('renders the place card INSIDE the same wrapper as the map (its positioning context)', async () => {
    placeCardMock.mockResolvedValue({ status: 'ok', rating: 4.6 });
    const { container, findByTestId } = mount([
      { lat: 1, lng: 1, name: 'A', city: 'C', category: 'attraction' },
    ]);
    await tick();
    await fireEvent.click(live()[0].element);
    const card = await findByTestId('place-card');
    const wrap = container.querySelector('.tg-map-wrap');
    expect(wrap).not.toBeNull();
    expect(wrap?.contains(card)).toBe(true);
    expect(wrap?.querySelector('.tg-map')).not.toBeNull();
  });
});

// ── 4. honest states ──────────────────────────────────────────────────────────────────
describe('Map — honest place-card states', () => {
  it('lodging pin shows the "approximate location" note and never queries Places', async () => {
    const { getByTestId, findByTestId } = mount([
      { lat: 1, lng: 1, name: 'Some Hotel', city: 'Tokyo', category: 'lodging', label: 'Some Hotel' },
    ]);
    await tick();
    await fireEvent.click(live()[0].element);
    await findByTestId('place-card-approx');
    expect(getByTestId('place-card-approx')).toBeInTheDocument();
    expect(placeCardMock).not.toHaveBeenCalled(); // city-centroid → no specific place to query
  });

  it('non-lodging pin with no name shows the approximate note and does not query', async () => {
    const { findByTestId } = mount([{ lat: 1, lng: 1, city: 'Tokyo', category: 'attraction', label: 'Mystery' }]);
    await tick();
    await fireEvent.click(live()[0].element);
    await findByTestId('place-card-approx');
    expect(placeCardMock).not.toHaveBeenCalled();
  });

  it('shows the loading state while the lookup is in flight', async () => {
    placeCardMock.mockReturnValue(new Promise(() => {})); // never resolves
    const { findByTestId } = mount([{ lat: 1, lng: 1, name: 'A', city: 'C', category: 'attraction' }]);
    await tick();
    await fireEvent.click(live()[0].element);
    expect(await findByTestId('place-card-loading')).toBeInTheDocument();
  });

  it('status:ok renders rating + the "Live from Google Places" provenance line', async () => {
    placeCardMock.mockResolvedValue({ status: 'ok', rating: 4.7, user_rating_count: 1200 });
    const { findByTestId, getByTestId } = mount([{ lat: 1, lng: 1, name: 'Senso-ji', city: 'Tokyo', category: 'attraction' }]);
    await tick();
    await fireEvent.click(live()[0].element);
    const rating = await findByTestId('place-card-rating');
    expect(rating).toHaveTextContent('4.7');
    // Provenance line has no test-id — assert via the card's text.
    expect(getByTestId('place-card')).toHaveTextContent('Live from Google Places');
  });

  it('status:unavailable renders the honest "unavailable" panel (no fabricated data)', async () => {
    placeCardMock.mockResolvedValue({ status: 'unavailable' });
    const { findByTestId } = mount([{ lat: 1, lng: 1, name: 'Ghost', city: 'Nowhere', category: 'attraction' }]);
    await tick();
    await fireEvent.click(live()[0].element);
    expect(await findByTestId('place-card-unavailable')).toBeInTheDocument();
  });

  it('a thrown lookup renders the error panel with a Retry affordance', async () => {
    placeCardMock.mockRejectedValue(new Error('network down'));
    const { findByTestId, getByTestId } = mount([{ lat: 1, lng: 1, name: 'A', city: 'C', category: 'attraction' }]);
    await tick();
    await fireEvent.click(live()[0].element);
    await findByTestId('place-card-error');
    expect(getByTestId('place-card-retry')).toBeInTheDocument();
  });
});

// ── 4b. G4 (#181) — pin title "unreadable primary" indicator ─────────────────────────
// MapPin.name is already the collapsed displayName() primary (pinOf() in itinerary.ts
// never threads name_en/name_source through), so this can't reproduce the FULL
// namePresentation() tiers (local companion, verified/romanized badge) — that's a
// documented, known minor gap (see Map.svelte's pinUnreadable() comment). What IS
// reproduced: the same "unreadable primary" honesty signal, off the same string.
describe('Map — G4 (#181): pin card/sheet title shows the "unreadable primary" indicator', () => {
  it('a pin whose name is non-Latin script with no readable fallback shows the indicator', async () => {
    placeCardMock.mockResolvedValue({ status: 'unavailable' });
    const { getByTestId, findByTestId } = mount([
      { lat: 35.7, lng: 139.8, name: '築地場外市場', city: 'Tokyo', category: 'attraction' },
    ]);
    await tick();
    await fireEvent.click(live()[0].element);
    await findByTestId('place-card');
    expect(getByTestId('place-card')).toHaveTextContent('shown in original script — no English name available');
  });

  it('an ordinary Latin-name pin shows no unreadable indicator (no regression)', async () => {
    placeCardMock.mockResolvedValue({ status: 'unavailable' });
    const { getByTestId, findByTestId } = mount([
      { lat: 35.7, lng: 139.8, name: 'Senso-ji', city: 'Tokyo', category: 'attraction' },
    ]);
    await tick();
    await fireEvent.click(live()[0].element);
    await findByTestId('place-card');
    expect(getByTestId('place-card')).not.toHaveTextContent('shown in original script');
  });

  it('a name already resolved to English (name_en-collapsed) shows no unreadable indicator', async () => {
    // Simulates the common case: MapPin.name already carries the name_en value
    // (pinOf() collapses to displayName()), so this is indistinguishable from an
    // ordinary Latin name at this layer — expected given the documented gap.
    placeCardMock.mockResolvedValue({ status: 'unavailable' });
    const { getByTestId, findByTestId } = mount([
      { lat: 35.7, lng: 139.8, name: 'Tsukiji Outer Market', city: 'Tokyo', category: 'attraction' },
    ]);
    await tick();
    await fireEvent.click(live()[0].element);
    await findByTestId('place-card');
    expect(getByTestId('place-card')).not.toHaveTextContent('shown in original script');
  });
});

// ── 5. out-of-order guard (reqSeq) ────────────────────────────────────────────────────
describe('Map — fast pin switch drops the stale (out-of-order) response', () => {
  it('a late response from the FIRST pin does not overwrite the SECOND pin’s card', async () => {
    const resolvers: Array<(v: unknown) => void> = [];
    placeCardMock.mockImplementation(() => new Promise((res) => resolvers.push(res)));

    const { findByTestId, getByTestId } = mount([
      { lat: 1, lng: 1, name: 'First', city: 'C', category: 'attraction' },
      { lat: 2, lng: 2, name: 'Second', city: 'C', category: 'attraction' },
    ]);
    await tick();
    const [first, second] = live();

    await fireEvent.click(first.element);  // seq 1, pending
    await fireEvent.click(second.element); // seq 2, pending — now the active selection

    // The newer request resolves first.
    resolvers[1]({ status: 'ok', rating: 2.2 });
    const rating = await findByTestId('place-card-rating');
    expect(rating).toHaveTextContent('2.2');

    // The older (superseded) request resolves LATER — it must be ignored.
    resolvers[0]({ status: 'ok', rating: 9.9 });
    await tick();
    expect(getByTestId('place-card-rating')).toHaveTextContent('2.2'); // still the second pin
    expect(getByTestId('place-card')).toHaveTextContent('Second');
  });
});

// ── 6. mapFocus (#163) — locate parity: opens the SAME card a direct pin click opens ──
describe('Map — mapFocus opens the place card', () => {
  it('a full focus (name+city+category) pans AND opens the place card, then one-shot consumes', async () => {
    placeCardMock.mockResolvedValue({ status: 'ok', rating: 4.6 });
    const { findByTestId, getByTestId } = mount([]);
    await tick();

    mapFocus.set({ lat: 34.97, lng: 135.77, label: 'Fushimi Inari', name: 'Fushimi Inari', city: 'Kyoto', category: 'attraction' });
    await tick();

    await findByTestId('place-card');
    expect(getByTestId('place-card')).toHaveTextContent('Fushimi Inari');
    expect(placeCardMock).toHaveBeenCalledWith('Fushimi Inari', 'Kyoto');
    expect(get(mapFocus)).toBeNull();                 // one-shot consumed
    expect(store.flyToCalls.length).toBe(1);           // panned too
  });

  it('honest degradation: no name/city pans only — no card, no query', async () => {
    const { queryByTestId } = mount([]);
    await tick();

    mapFocus.set({ lat: 1, lng: 1, label: 'somewhere' });
    await tick();

    expect(queryByTestId('place-card')).toBeNull();
    expect(queryByTestId('place-card-approx')).toBeNull(); // openCard never called
    expect(placeCardMock).not.toHaveBeenCalled();
    expect(get(mapFocus)).toBeNull();                       // still one-shot consumed
    expect(store.flyToCalls.length).toBe(1);
  });

  it('category maps to a real query (not the lodging "approximate" trap)', async () => {
    placeCardMock.mockResolvedValue({ status: 'ok', rating: 4.1 });
    const { findByTestId, queryByTestId } = mount([]);
    await tick();

    mapFocus.set({ lat: 1, lng: 1, name: 'Some Bistro', city: 'Paris', category: 'restaurant' });
    await tick();

    await findByTestId('place-card');
    expect(queryByTestId('place-card-approx')).toBeNull();
    expect(placeCardMock).toHaveBeenCalledWith('Some Bistro', 'Paris');
  });

  it('clears a stale card when a pan-only focus follows an opened card', async () => {
    placeCardMock.mockResolvedValue({ status: 'ok', rating: 4.0 });
    const { findByTestId, queryByTestId } = mount([]);
    await tick();

    mapFocus.set({ lat: 1, lng: 1, name: 'A', city: 'C', category: 'attraction' });
    await tick();
    await findByTestId('place-card');

    mapFocus.set({ lat: 2, lng: 2, label: 'no place here' });
    await tick();
    expect(queryByTestId('place-card')).toBeNull();
  });
});
