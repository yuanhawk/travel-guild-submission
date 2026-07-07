// @vitest-environment jsdom
//
// Live best-price OVERLAY display contract (PROD/live edition) — additive layer.
// Two halves:
//   1. Pure helpers (livePriceOverlay / safeHref) — extraction + XSS-safe guard.
//   2. <Itinerary> COMPONENT render in both editions:
//        • PROD (status:'ok')  → price + 'live' badge + safe https:// deeplink.
//        • UAT  (absent / status:'unavailable') → seeded view BYTE-IDENTICAL,
//          no overlay node emitted (zero-regression).
//        • unsafe deeplink (javascript:/data:/ //host) → blocked by the href guard
//          (no <a> rendered), price + badge still shown.

import { describe, it, expect } from 'vitest';
import { render } from '@testing-library/svelte';
import { livePriceOverlay, safeHref } from './api';
import type { Leg, DayPlan } from './api';
import Itinerary from './components/Itinerary.svelte';

// ── shared seeded fixture (one Tokyo leg, one day) — the UAT floor ───────────
const DAY = { day_index: 0, bad_weather: false, attractions: [], meals: {} };
const PLANS: DayPlan[] = [{
  leg_id: 'leg-0', city: 'Tokyo', checkin: '2026-11-01', checkout: '2026-11-03', num_days: 2,
  days: [DAY],
}];
const BASE_LEG: Leg = { leg_id: 'leg-0', city: 'Tokyo' };

const OK_OVERLAY = {
  status: 'ok' as const,
  hotel: 'Park Hotel Tokyo',
  lowest_price_cents: 18450,
  currency: 'USD',
  deeplink: 'https://book.example.com/tokyo/park-hotel?aff=guild',
  source: 'ExamplePartner',
  fetched_at: '2026-06-29T08:00:00Z',
};

function mount(legs: Leg[]) {
  return render(Itinerary, { props: { plans: PLANS, legs, idempotencyKey: 'trip-1', editable: false } });
}

// ── 1. pure helper: extraction semantics ────────────────────────────────────
describe('livePriceOverlay — extraction', () => {
  it('returns the overlay when present and status:ok (price_overlay key)', () => {
    expect(livePriceOverlay({ price_overlay: OK_OVERLAY })).toEqual(OK_OVERLAY);
  });
  it('also reads the live_price key', () => {
    expect(livePriceOverlay({ live_price: OK_OVERLAY })).toEqual(OK_OVERLAY);
  });
  it('returns null in UAT — overlay absent', () => {
    expect(livePriceOverlay({})).toBeNull();
    expect(livePriceOverlay(undefined)).toBeNull();
    expect(livePriceOverlay(BASE_LEG)).toBeNull();
  });
  it('returns null on status:unavailable', () => {
    expect(livePriceOverlay({ price_overlay: { status: 'unavailable', reason: 'no_match' } })).toBeNull();
  });
  it('returns null when required fields are missing', () => {
    expect(livePriceOverlay({ price_overlay: { status: 'ok', deeplink: 'https://x' } })).toBeNull();
    expect(livePriceOverlay({ price_overlay: { status: 'ok', lowest_price_cents: 100, currency: 'USD' } })).toBeNull();
  });
});

// ── 2. pure helper: XSS-safe href guard ─────────────────────────────────────
describe('safeHref — XSS-safe external guard', () => {
  it('allows https:// and same-origin relative paths', () => {
    expect(safeHref('https://book.example.com/x?aff=1')).toBe('https://book.example.com/x?aff=1');
    expect(safeHref('/trips/abc')).toBe('/trips/abc');
  });
  it('blocks javascript:, data:, http:, and protocol-relative //host', () => {
    expect(safeHref('javascript:alert(1)')).toBe('');
    expect(safeHref('data:text/html,<script>1</script>')).toBe('');
    expect(safeHref('http://insecure.example.com')).toBe('');
    expect(safeHref('//evil.example.com/phish')).toBe('');
    expect(safeHref(null)).toBe('');
    expect(safeHref(undefined)).toBe('');
  });
});

// ── 3. component: PROD overlay present → price + badge + safe deeplink ───────
describe('<Itinerary> — overlay present (PROD)', () => {
  it('renders price, live badge, source, and the booking deeplink as a safe link', () => {
    const { getByTestId } = mount([{ ...BASE_LEG, price_overlay: OK_OVERLAY }]);

    expect(getByTestId('live-price-0')).toBeTruthy();
    expect(getByTestId('live-price-amount').textContent).toBe('$184.50');
    expect(getByTestId('live-price-badge').textContent).toBe('live');
    expect(getByTestId('live-price-source').textContent).toContain('ExamplePartner');

    const link = getByTestId('live-price-deeplink') as HTMLAnchorElement;
    expect(link.getAttribute('href')).toBe(OK_OVERLAY.deeplink);
    expect(link.getAttribute('target')).toBe('_blank');
    expect(link.getAttribute('rel')).toBe('noopener noreferrer');
  });

  it('prefixes a non-USD currency code on the rendered price', () => {
    const { getByTestId } = mount([
      { ...BASE_LEG, price_overlay: { ...OK_OVERLAY, currency: 'EUR', lowest_price_cents: 16000 } },
    ]);
    expect(getByTestId('live-price-amount').textContent).toBe('EUR 160.00');
  });
});

// ── 4. component: UAT (absent / unavailable) → seeded view UNCHANGED ─────────
describe('<Itinerary> — overlay absent (UAT) is byte-identical', () => {
  it('renders NO overlay node when the field is absent', () => {
    const { container, queryByTestId } = mount([BASE_LEG]);
    expect(queryByTestId('live-price-0')).toBeNull();
    // seeded leg header still present
    expect(container.querySelector('.city')?.textContent).toBe('Tokyo');
  });

  it('produces identical DOM whether the field is absent or status:unavailable', () => {
    const absent = mount([BASE_LEG]).container.innerHTML;
    const unavailable = mount([
      { ...BASE_LEG, price_overlay: { status: 'unavailable', reason: 'no_match' } },
    ]).container.innerHTML;
    expect(unavailable).toBe(absent);
  });
});

// ── 5. component: unsafe deeplink blocked by the href guard ──────────────────
describe('<Itinerary> — unsafe deeplink is blocked', () => {
  for (const bad of ['javascript:alert(1)', 'data:text/html,x', '//evil.example.com/phish', 'http://insecure']) {
    it(`renders price+badge but NO link for deeplink "${bad}"`, () => {
      const { getByTestId, queryByTestId } = mount([
        { ...BASE_LEG, price_overlay: { ...OK_OVERLAY, deeplink: bad } },
      ]);
      expect(getByTestId('live-price-amount')).toBeTruthy();
      expect(getByTestId('live-price-badge')).toBeTruthy();
      expect(queryByTestId('live-price-deeplink')).toBeNull();
    });
  }
});
