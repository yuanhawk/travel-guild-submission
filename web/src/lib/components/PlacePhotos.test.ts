// @vitest-environment jsdom
/// <reference types="@testing-library/jest-dom" />
//
// PlacePhotos.svelte — render-contract tests.
//
// Covers:
//   1. Initial render: only the 📷 Photo button, no panel
//   2. Click 📷: POSTs to API_BASE/place_card with correct body
//   3. Single-photo ok response: 1 img, correct src, rating, open-now, no "Show more"
//   4. Three-photo ok response: 1 img visible, "Show more 2 photos" button present
//   5. Click "Show more": all 3 imgs visible, button gone
//   6. Click ▲ hide: panel closes; re-open shows 1 photo (showAll reset)
//   7. Unavailable response: "No photos available" text, no img
//   8. Click img-btn → lightbox overlay appears; click ✕ → lightbox closes

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, fireEvent } from '@testing-library/svelte';
import PlacePhotos from './PlacePhotos.svelte';

// ── api module mock ───────────────────────────────────────────────────────────
vi.mock('../api', () => ({ API_BASE: 'http://test-api' }));

// ── fetch mock helpers ────────────────────────────────────────────────────────
function mockFetch(response: object, ok = true) {
  const fetchMock = vi.fn().mockResolvedValue({
    ok,
    json: () => Promise.resolve(response),
  });
  vi.stubGlobal('fetch', fetchMock);
  return fetchMock;
}

const OK_1_PHOTO = {
  status: 'ok',
  place: {
    photos: ['/place_photo?ref=abc'],
    rating: 4.5,
    user_rating_count: 100,
    open_now: true,
  },
};

const OK_3_PHOTOS = {
  status: 'ok',
  place: {
    photos: ['/place_photo?ref=p1', '/place_photo?ref=p2', '/place_photo?ref=p3'],
    rating: 4.2,
    user_rating_count: 50,
    open_now: false,
  },
};

const UNAVAILABLE = { status: 'unavailable' };

beforeEach(() => { vi.restoreAllMocks(); });
afterEach(() => { vi.unstubAllGlobals(); });

// ── helper: mount + open panel (fetch+click) ──────────────────────────────────
async function mountAndOpen(fetchResponse: object) {
  mockFetch(fetchResponse);
  const c = render(PlacePhotos, { props: { name: 'Senso-ji', city: 'Tokyo', country: 'Japan' } });
  const btn = c.getByRole('button', { name: /View photos for Senso-ji/ });
  await fireEvent.click(btn);
  // Wait for async fetch to resolve
  await new Promise<void>((r) => setTimeout(r, 0));
  return c;
}

// ── 1. Initial render ─────────────────────────────────────────────────────────
describe('PlacePhotos — initial state', () => {
  it('renders 📷 Photo button only; no panel, no img', () => {
    const c = render(PlacePhotos, { props: { name: 'Test Place', city: 'Tokyo', country: 'Japan' } });
    expect(c.getByRole('button', { name: /View photos/ })).toBeInTheDocument();
    expect(c.container.querySelector('.ph-panel')).toBeNull();
    expect(c.container.querySelector('.ph-img')).toBeNull();
  });
});

// ── 2. Fetch call correctness ─────────────────────────────────────────────────
describe('PlacePhotos — fetch request', () => {
  it('POSTs to API_BASE/place_card with mode:detail + name, city, country', async () => {
    const fetchMock = mockFetch(UNAVAILABLE);
    const c = render(PlacePhotos, { props: { name: 'Senso-ji', city: 'Tokyo', country: 'Japan' } });
    await fireEvent.click(c.getByRole('button', { name: /View photos/ }));
    await new Promise<void>((r) => setTimeout(r, 0));

    expect(fetchMock).toHaveBeenCalledOnce();
    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toBe('http://test-api/place_card');
    expect(init.method).toBe('POST');
    const body = JSON.parse(init.body as string);
    expect(body).toEqual({ mode: 'detail', name: 'Senso-ji', city: 'Tokyo', country: 'Japan' });
  });

  it('does NOT re-fetch on subsequent opens (cached)', async () => {
    const fetchMock = mockFetch(UNAVAILABLE);
    const c = render(PlacePhotos, { props: { name: 'Senso-ji', city: 'Tokyo', country: 'Japan' } });
    const btn = c.getByRole('button', { name: /View photos/ });
    await fireEvent.click(btn);  // open
    await new Promise<void>((r) => setTimeout(r, 0));
    await fireEvent.click(btn);  // close
    await fireEvent.click(c.getByRole('button', { name: /View photos/ }));  // re-open
    await new Promise<void>((r) => setTimeout(r, 0));
    expect(fetchMock).toHaveBeenCalledOnce();  // only 1 fetch total
  });
});

// ── 3. Single-photo ok response ───────────────────────────────────────────────
describe('PlacePhotos — single photo ok response', () => {
  it('shows 1 img with correct API_BASE-prefixed src', async () => {
    const c = await mountAndOpen(OK_1_PHOTO);
    const imgs = c.container.querySelectorAll('.ph-img');
    expect(imgs).toHaveLength(1);
    expect((imgs[0] as HTMLImageElement).src).toBe('http://test-api/place_photo?ref=abc');
  });

  it('renders rating and review count', async () => {
    const c = await mountAndOpen(OK_1_PHOTO);
    expect(c.container.querySelector('.ph-rating')?.textContent).toContain('4.5');
    expect(c.container.querySelector('.ph-count')?.textContent).toContain('100');
  });

  it('renders Open now status', async () => {
    const c = await mountAndOpen(OK_1_PHOTO);
    const status = c.container.querySelector('.ph-status');
    expect(status?.textContent?.trim()).toBe('Open now');
    expect(status?.classList.contains('open-now')).toBe(true);
  });

  it('does NOT show "Show more" button when only 1 photo', async () => {
    const c = await mountAndOpen(OK_1_PHOTO);
    expect(c.container.querySelector('.ph-more')).toBeNull();
  });
});

// ── 4. Three-photo ok response: 1 visible + show-more ────────────────────────
describe('PlacePhotos — three photos', () => {
  it('shows only 1 img initially', async () => {
    const c = await mountAndOpen(OK_3_PHOTOS);
    expect(c.container.querySelectorAll('.ph-img')).toHaveLength(1);
  });

  it('shows "Show more 2 photos" button', async () => {
    const c = await mountAndOpen(OK_3_PHOTOS);
    const moreBtn = c.container.querySelector('.ph-more');
    expect(moreBtn).not.toBeNull();
    expect(moreBtn?.textContent).toContain('Show 2 more photos');
  });

  it('click Show more → all 3 imgs visible, button gone', async () => {
    const c = await mountAndOpen(OK_3_PHOTOS);
    const moreBtn = c.container.querySelector('.ph-more') as HTMLButtonElement;
    await fireEvent.click(moreBtn);
    expect(c.container.querySelectorAll('.ph-img')).toHaveLength(3);
    expect(c.container.querySelector('.ph-more')).toBeNull();
  });
});

// ── 5. Hide resets showAll ────────────────────────────────────────────────────
describe('PlacePhotos — close and reopen', () => {
  it('closing and reopening resets to 1 photo', async () => {
    const c = await mountAndOpen(OK_3_PHOTOS);

    // Expand to all 3
    await fireEvent.click(c.container.querySelector('.ph-more') as HTMLButtonElement);
    expect(c.container.querySelectorAll('.ph-img')).toHaveLength(3);

    // Close
    await fireEvent.click(c.getByRole('button', { name: /Hide photos/ }));
    expect(c.container.querySelector('.ph-panel')).toBeNull();

    // Reopen — should show 1 again (showAll reset)
    await fireEvent.click(c.getByRole('button', { name: /View photos/ }));
    await new Promise<void>((r) => setTimeout(r, 0));
    expect(c.container.querySelectorAll('.ph-img')).toHaveLength(1);
    expect(c.container.querySelector('.ph-more')).not.toBeNull();
  });
});

// ── 6. Unavailable response ───────────────────────────────────────────────────
describe('PlacePhotos — unavailable response', () => {
  it('shows "No photos available" text; no img', async () => {
    const c = await mountAndOpen(UNAVAILABLE);
    expect(c.container.querySelector('.ph-none')?.textContent).toContain('No photos available');
    expect(c.container.querySelector('.ph-img')).toBeNull();
  });
});

// ── 7. Lightbox ──────────────────────────────────────────────────────────────
describe('PlacePhotos — lightbox', () => {
  it('clicking photo opens lightbox with that src', async () => {
    const c = await mountAndOpen(OK_1_PHOTO);
    const imgBtn = c.container.querySelector('.ph-img-btn') as HTMLButtonElement;
    await fireEvent.click(imgBtn);
    const overlay = c.container.querySelector('.lb-overlay');
    expect(overlay).not.toBeNull();
    const lbImg = overlay?.querySelector('.lb-img') as HTMLImageElement;
    expect(lbImg?.src).toBe('http://test-api/place_photo?ref=abc');
  });

  it('clicking ✕ close button dismisses lightbox', async () => {
    const c = await mountAndOpen(OK_1_PHOTO);
    await fireEvent.click(c.container.querySelector('.ph-img-btn') as HTMLButtonElement);
    expect(c.container.querySelector('.lb-overlay')).not.toBeNull();

    await fireEvent.click(c.getByRole('button', { name: 'Close photo' }));
    expect(c.container.querySelector('.lb-overlay')).toBeNull();
  });
});
