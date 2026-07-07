// #99 — place card API contract tests
// Pure fetch-mock + api.ts functions. No server, no chromium, no Svelte mounting.
// Asserts: request shape, response passthrough, unavailable honesty, cache semantics,
// error-retry, placePhotoUrl encoding, and key-leak guard.

import { describe, it, expect, vi, afterEach, beforeEach } from 'vitest';
import { placeCard, placePhotoUrl, __clearPlaceCardCache, API_BASE } from './api';
import type { PlaceCard } from './api';

// ── helpers ───────────────────────────────────────────────────────────────
function mockFetch(status: number, body: unknown) {
  return vi.fn(async () => ({
    ok: status >= 200 && status < 300,
    status,
    json: async () => body,
    text: async () => JSON.stringify(body),
  })) as unknown as typeof fetch;
}
type Mocked = { mock: { calls: [string, RequestInit][] } };

beforeEach(() => __clearPlaceCardCache());
afterEach(() => vi.restoreAllMocks());

// ── 1. request shape ──────────────────────────────────────────────────────
describe('placeCard — POST /place_card request shape', () => {
  it('POSTs to /place_card with method POST and JSON body {place, city}', async () => {
    const f = mockFetch(200, { status: 'ok', name: 'Senso-ji', rating: 4.7 });
    vi.stubGlobal('fetch', f);
    await placeCard('Senso-ji', 'Tokyo');
    const [url, init] = (f as unknown as Mocked).mock.calls[0];
    expect(url).toMatch(/\/place_card$/);
    expect(init.method).toBe('POST');
    const body = JSON.parse(String(init.body));
    expect(body.place).toBe('Senso-ji');
    expect(body.city).toBe('Tokyo');
  });
});

// ── 2. ok response: nested wire envelope → flat PlaceCard ─────────────────
// REGRESSION for the map-pin-empty-popup bug: society/utils/places_card.py
// actually serves the useful fields NESTED under `place`, using different names
// (display_name, photos as full "/place_photo?ref=..." paths, reviews[].author)
// than PlaceCard's flat contract. A prior version of this test mocked the FLAT
// shape directly — i.e. it asserted passthrough of a shape the server never
// sends — so it kept passing while every field silently rendered undefined in
// prod. This mocks the REAL wire envelope and asserts the normalized result.
describe('placeCard — ok response: nested wire envelope normalized to flat PlaceCard', () => {
  it('flattens place.{display_name,rating,user_rating_count,open_now}', async () => {
    vi.stubGlobal('fetch', mockFetch(200, {
      status: 'ok',
      source: 'live:google_places',
      as_of: '2026-07-04T00:00:00Z',
      place: {
        display_name: 'Senso-ji',
        formatted_address: '2 Chome-3-1 Asakusa, Taito City, Tokyo',
        rating: 4.6,
        user_rating_count: 1200,
        open_now: true,
        photos: [],
        reviews: [],
      },
    }));
    const c = await placeCard('Senso-ji', 'Tokyo');
    expect(c.status).toBe('ok');
    expect(c.name).toBe('Senso-ji');
    expect(c.rating).toBe(4.6);
    expect(c.user_rating_count).toBe(1200);
    expect(c.open_now).toBe(true);
  });

  it('extracts the opaque ref out of served "/place_photo?ref=<opaque>" photo paths', async () => {
    vi.stubGlobal('fetch', mockFetch(200, {
      status: 'ok',
      place: { display_name: 'Senso-ji', photos: ['/place_photo?ref=OPAQUE_REF_1', '/place_photo?ref=OPAQUE_REF_2'] },
    }));
    const c = await placeCard('Senso-ji', 'Tokyo');
    expect(c.photo_refs).toEqual(['OPAQUE_REF_1', 'OPAQUE_REF_2']);
  });

  it('maps place.reviews[].author → PlaceReview.author_name (server field is `author`, not `author_name`)', async () => {
    vi.stubGlobal('fetch', mockFetch(200, {
      status: 'ok',
      place: { display_name: 'Senso-ji', reviews: [{ author: 'Alice', rating: 5, text: 'Amazing!' }] },
    }));
    const c = await placeCard('Senso-ji', 'Tokyo');
    expect(c.reviews?.[0].author_name).toBe('Alice');
    expect(c.reviews?.[0].rating).toBe(5);
    expect(c.reviews?.[0].text).toBe('Amazing!');
  });

  it('a flat (non-nested) body — the OLD assumed shape — normalizes to unavailable, not silent-empty-ok', async () => {
    const flatOldShape: PlaceCard = {
      status: 'ok', name: 'Senso-ji', rating: 4.6, user_rating_count: 1200, open_now: true,
    };
    vi.stubGlobal('fetch', mockFetch(200, flatOldShape));
    const c = await placeCard('Senso-ji', 'Tokyo');
    // No `place` key on this body → treated as no matching result, same as the
    // server's own `_unavailable()` path. Pins should show the honest
    // "unavailable" state rather than a card with every field blank.
    expect(c.status).toBe('unavailable');
  });
});

// ── 3. unavailable passthrough ────────────────────────────────────────────
describe('placeCard — unavailable passthrough (honesty)', () => {
  it('returns {status:unavailable} and no rating/reviews/photo_refs', async () => {
    vi.stubGlobal('fetch', mockFetch(200, { status: 'unavailable' }));
    const c = await placeCard('Nowhere', 'Ghost City');
    expect(c.status).toBe('unavailable');
    expect(c.rating).toBeUndefined();
    expect(c.reviews).toBeUndefined();
    expect(c.photo_refs).toBeUndefined();
  });
});

// ── 4. malformed body coerced to unavailable ──────────────────────────────
describe('placeCard — malformed body coerced to unavailable', () => {
  it('coerces {} (no status field) to {status:unavailable}', async () => {
    vi.stubGlobal('fetch', mockFetch(200, {}));
    const c = await placeCard('Place', 'City');
    expect(c.status).toBe('unavailable');
  });

  it('coerces null status field to {status:unavailable}', async () => {
    vi.stubGlobal('fetch', mockFetch(200, { status: null }));
    const c = await placeCard('Place2', 'City2');
    expect(c.status).toBe('unavailable');
  });
});

// ── 5. cache: same (place,city) → fetch once ─────────────────────────────
describe('placeCard — in-memory cache', () => {
  it('same (place,city) fires fetch only once (two awaited calls)', async () => {
    const f = mockFetch(200, { status: 'ok', place: { rating: 4.2 } });
    vi.stubGlobal('fetch', f);
    const [r1, r2] = await Promise.all([placeCard('A', 'City'), placeCard('A', 'City')]);
    expect((f as unknown as Mocked).mock.calls.length).toBe(1);
    expect(r1).toBe(r2); // same promise resolved
  });

  it('cache key is case/space-insensitive', async () => {
    const f = mockFetch(200, { status: 'ok', place: { rating: 3.9 } });
    vi.stubGlobal('fetch', f);
    await placeCard('A', 'City');
    await placeCard(' a ', 'CITY'); // should hit cache
    expect((f as unknown as Mocked).mock.calls.length).toBe(1);
  });
});

// ── 6. cache: different city → fetch twice ────────────────────────────────
describe('placeCard — different city bypasses cache', () => {
  it('two different cities produce two fetch calls', async () => {
    const f = mockFetch(200, { status: 'ok', place: {} });
    vi.stubGlobal('fetch', f);
    await placeCard('A', 'Tokyo');
    await placeCard('A', 'Osaka');
    expect((f as unknown as Mocked).mock.calls.length).toBe(2);
  });
});

// ── 7. in-flight dedupe ───────────────────────────────────────────────────
describe('placeCard — in-flight deduplication', () => {
  it('two concurrent calls share one pending fetch', async () => {
    const f = mockFetch(200, { status: 'ok', place: { rating: 4.0 } });
    vi.stubGlobal('fetch', f);
    // Fire both WITHOUT awaiting between
    const p1 = placeCard('B', 'C');
    const p2 = placeCard('B', 'C');
    await Promise.all([p1, p2]);
    expect((f as unknown as Mocked).mock.calls.length).toBe(1);
  });
});

// ── 8. errors NOT cached (retry possible) ────────────────────────────────
describe('placeCard — network errors are not cached', () => {
  it('rejects on network error, then allows a successful retry', async () => {
    // First call: throw
    const failFetch = vi.fn(async () => { throw new Error('network down'); }) as unknown as typeof fetch;
    vi.stubGlobal('fetch', failFetch);
    await expect(placeCard('A', 'C')).rejects.toThrow('network down');

    // Second call: swap to success mock — should trigger a real fetch (not cached)
    const okFetch = mockFetch(200, { status: 'ok', place: { rating: 4.5 } });
    vi.stubGlobal('fetch', okFetch);
    const c = await placeCard('A', 'C');
    expect(c.status).toBe('ok');
    expect((okFetch as unknown as Mocked).mock.calls.length).toBe(1);
  });
});

// ── 9. placePhotoUrl encoding ─────────────────────────────────────────────
describe('placePhotoUrl — URL encoding + key-leak guard', () => {
  it('percent-encodes spaces, slashes, and query chars in the ref', () => {
    const url = placePhotoUrl('a b/c?d');
    expect(url).toBe(`${API_BASE}/place_photo?ref=a%20b%2Fc%3Fd`);
  });

  it('never contains googleapis or key= (no key leak)', () => {
    const url = placePhotoUrl('SOME_OPAQUE_REF');
    expect(url).not.toContain('googleapis');
    expect(url).not.toContain('key=');
  });
});

// ── 10. placePhotoUrl base ────────────────────────────────────────────────
describe('placePhotoUrl — base URL', () => {
  it('starts with API_BASE + /place_photo?ref=', () => {
    const url = placePhotoUrl('REF');
    expect(url.startsWith(`${API_BASE}/place_photo?ref=`)).toBe(true);
  });
});
