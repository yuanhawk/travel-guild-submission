// @vitest-environment jsdom
//
// COMPONENT test — money-itemization honesty fix (held-vs-charged mismatch).
//
// Root cause (adversarial investigation, 2026-07-06): fee estimates (insurance/visa/
// vaccine) are folded into the plan_ready "held" total for DISPLAY only — the merchant
// only ever debits the lodging line (package_total_cents). The old UI showed one
// unqualified figure at "held" time and a smaller, unexplained figure at "charged" time
// (e.g. $214.60 held -> $160.00 charged, with no on-screen reconciliation). This test
// mounts the real <App>, drives it through plan_ready -> Confirm & Book -> booked with a
// fixture that has BOTH a lodging figure and a nonzero fee estimate, and asserts:
//   1. the held banner itemizes lodging vs. fee estimate and labels the fee estimate as
//      never charged by us;
//   2. lodging + fee estimate reconciles EXACTLY to the held/trip-total figure (no gap);
//   3. the Confirm & Book button shows the LODGING figure (the actual charge), not the
//      with-fees total;
//   4. the post-booking banner's charged figure equals the lodging figure, and the fee
//      estimate is named and labeled not-charged (no silent, unexplained shortfall).

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, fireEvent, waitFor } from '@testing-library/svelte';

const LODGING_CENTS = 16000;                    // $160.00 — the actual merchant/wallet debit
const FEE_CENTS = 5460;                         // $54.60 — insurance + visa estimate, display-only
const HELD_CENTS = LODGING_CENTS + FEE_CENTS;   // $214.60 — trip total shown pre-booking

const PLAN_READY_ENVELOPE = {
  outcome: 'plan_ready',
  payment_status: 'held',
  idempotency_key: 'trip-money-1',
  package_total_cents: LODGING_CENTS,
  package_total_with_fees_cents: HELD_CENTS,
  fee_total_cents: FEE_CENTS,
  fee_line_items: [
    { description: 'Travel insurance premium', kind: 'premium', usd_cents: 4200 },
    { description: 'Visa fee', kind: 'visa', usd_cents: 1260 },
  ],
  total_budget_cents: 400000,
  wallet: { balance_cents: 500000, held_cents: HELD_CENTS, debited: false },
  legs: [{ leg_id: 'leg-0', city: 'Tokyo', country: 'Japan', checkin: '2026-10-01', checkout: '2026-10-04' }],
  day_plans: [{ leg_id: 'leg-0', city: 'Tokyo', country: 'Japan', num_days: 3,
    days: [{ day_index: 0, meals: {}, attractions: [] }] }],
};

const BOOKED_ENVELOPE = {
  ...PLAN_READY_ENVELOPE,
  outcome: 'success',
  payment_status: 'charged',
  booking_ref: 'BK-MONEY-1',
  wallet: { balance_cents: 500000 - LODGING_CENTS, debited: true, debit_cents: LODGING_CENTS },
};

vi.mock('./planStream', () => ({
  planStreaming: vi.fn(() => ({ done: Promise.resolve(PLAN_READY_ENVELOPE), close: vi.fn() })),
}));
vi.mock('./safety', async (orig) => {
  const actual = await (orig() as Promise<Record<string, unknown>>);
  return { ...actual, safeGetEmergencies: vi.fn(async () => ({ as_of: null, items: [] })) };
});
// <Map> pulls in maplibre-gl (WebGL), which doesn't load under jsdom.
vi.mock('./Map.svelte', () => ({ default: class { $on() {} $set() {} $destroy() {} } }));

import App from '../App.svelte';
import { planStreaming } from './planStream';
const planStreamingMock = planStreaming as unknown as ReturnType<typeof vi.fn>;

beforeEach(() => {
  localStorage.clear();
  localStorage.setItem('tg_session', JSON.stringify({ user: null, guest: true, savedAt: Date.now() }));
  planStreamingMock.mockClear();
});
afterEach(() => { localStorage.clear(); vi.restoreAllMocks(); });

/** All "$X,XXX.XX" USD figures in a string, parsed to integer cents. */
function allUsdCents(text: string): number[] {
  return [...text.matchAll(/\$[\d,]+\.\d{2}/g)].map((m) => Math.round(parseFloat(m[0].replace(/[$,]/g, '')) * 100));
}

// Drives the chat input to trigger the (mocked) plan_ready response. Takes the bound
// query functions directly (not the whole RenderResult) to sidestep a
// @testing-library/svelte generic-variance quirk across call sites.
async function planTrip(
  findByTestId: (id: string) => Promise<HTMLElement>,
  getAllByText: (text: string, opts?: { selector?: string }) => HTMLElement[],
) {
  const input = await findByTestId('chat-input');
  await fireEvent.input(input, { target: { value: 'plan a week in Tokyo' } });
  await fireEvent.click(getAllByText('Send', { selector: 'button' })[0]);
  return findByTestId('status-banner');
}

describe('App - money-itemization honesty (held vs. charged)', () => {
  it('held banner itemizes lodging vs. fee estimate, labeled never-charged, reconciling to the held total', async () => {
    const { findByTestId, getAllByText } = render(App);
    const banner = await planTrip(findByTestId, getAllByText);
    expect(banner).toHaveTextContent('Plan ready');

    const breakdown = await findByTestId('held-breakdown');
    const text = breakdown.textContent ?? '';
    expect(text).toMatch(/never charged by us/i);
    expect(text).toMatch(/insurance/i);

    // Exactly the held total, the lodging figure, and the fee figure appear — and
    // lodging + fee reconciles EXACTLY to the held total (no unexplained gap).
    const figs = allUsdCents(text);
    expect(figs).toEqual(expect.arrayContaining([HELD_CENTS, LODGING_CENTS, FEE_CENTS]));
    expect(LODGING_CENTS + FEE_CENTS).toBe(HELD_CENTS);
  });

  it('Confirm & Book shows the LODGING figure (the actual charge), not the with-fees total', async () => {
    const { findByTestId, getAllByText } = render(App);
    await planTrip(findByTestId, getAllByText);
    const button = await findByTestId('confirm-book');
    expect(button).toHaveTextContent('$160.00');
    expect(button).not.toHaveTextContent('$214.60');
  });

  it('post-booking: charged figure has no unexplained shortfall vs. the held figure — the fee estimate is named and marked not-charged', async () => {
    // /confirm resolves to the booked envelope.
    vi.stubGlobal('fetch', vi.fn(async (url: string) => {
      if (String(url).endsWith('/confirm')) {
        return { ok: true, status: 200, json: async () => BOOKED_ENVELOPE };
      }
      return { ok: true, status: 200, json: async () => ({}) };
    }) as unknown as typeof fetch);

    const { findByTestId, getAllByText, getByTestId } = render(App);
    await planTrip(findByTestId, getAllByText);
    const confirmBtn = await findByTestId('confirm-book');
    await fireEvent.click(confirmBtn);

    await waitFor(() => expect(getByTestId('status-banner')).toHaveTextContent('Booked'));
    const chargedLine = await findByTestId('charged-breakdown');
    const text = chargedLine.textContent ?? '';

    // The charged figure is the lodging amount, not the with-fees total.
    expect(text).toContain('$160.00');
    expect(text).not.toContain('$214.60');
    // The gap ($54.60) is named and explicitly marked as not charged by us — no silent
    // shortfall between what was held and what was charged.
    expect(text).toMatch(/\$54\.60/);
    expect(text).toMatch(/not charged by Travel Guild/i);
    // Regression guard: the two sentences must not run together (a template
    // whitespace-collapse bug once rendered "...prepaid).$54.60..." with no space).
    expect(text).not.toMatch(/prepaid\)\.\$/);
    expect(text).toMatch(/prepaid\)\.\s+\$54\.60/);
  });
});
