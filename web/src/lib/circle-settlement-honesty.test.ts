// @vitest-environment jsdom
//
// COMPONENT test — PR #12 adversarial audit, Finding2 (HIGH, honesty).
//
// ucp-merchant/circle_usdc.go's maybeCircleSettle writes into the SAME
// result.circle_settlement key on BOTH outcomes: a success object
// (transaction_id/status/tx_hash/block_explorer_url/rail/network/note) and, on
// every failure path (credentials absent, aggregate cap refused, booking_ref
// amount mismatch, transfer failed), an ERROR object ({error, note}) — while the
// booking itself still commits. <App> gated its "Settled with real USDC (testnet)"
// banner on mere truthiness of that field, so a refusal rendered as a claim that
// real money had moved. This test pins both directions.

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, fireEvent, waitFor } from '@testing-library/svelte';

const LODGING_CENTS = 16000;

const PLAN_READY_ENVELOPE = {
  outcome: 'plan_ready',
  payment_status: 'held',
  idempotency_key: 'trip-circle-1',
  package_total_cents: LODGING_CENTS,
  package_total_with_fees_cents: LODGING_CENTS,
  fee_total_cents: 0,
  fee_line_items: [],
  total_budget_cents: 400000,
  wallet: { balance_cents: 500000, held_cents: LODGING_CENTS, debited: false },
  legs: [{ leg_id: 'leg-0', city: 'Tokyo', country: 'Japan', checkin: '2026-10-01', checkout: '2026-10-04' }],
  day_plans: [{ leg_id: 'leg-0', city: 'Tokyo', country: 'Japan', num_days: 3,
    days: [{ day_index: 0, meals: {}, attractions: [] }] }],
};

const BOOKED_BASE = {
  ...PLAN_READY_ENVELOPE,
  outcome: 'success',
  payment_status: 'charged',
  booking_ref: 'BK-CIRCLE-1',
  wallet: { balance_cents: 500000 - LODGING_CENTS, debited: true, debit_cents: LODGING_CENTS },
};

// The genuine on-chain result (maybeCircleSettle's default branch).
const CIRCLE_SUCCESS = {
  transaction_id: 'ci-tx-0001',
  status: 'COMPLETE',
  tx_hash: '0xabc123',
  block_explorer_url: 'https://sepolia.etherscan.io/tx/0xabc123',
  rail: 'circle_usdc_testnet',
  network: 'ETH-SEPOLIA',
  note: 'REAL testnet settlement via Circle…',
};

// The refusal shape — NO status, NO transaction_id, no funds moved.
const CIRCLE_ERROR = {
  error: 'CIRCLE_NOT_CONFIGURED',
  note: 'CIRCLE_API_KEY and/or CIRCLE_ENTITY_SECRET are not set — this rail refuses to fake a settlement.',
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

/** /confirm resolves to the booked envelope carrying this circle_settlement value. */
function stubConfirmWith(circle_settlement: unknown) {
  vi.stubGlobal('fetch', vi.fn(async (url: string) => {
    if (String(url).endsWith('/confirm')) {
      return { ok: true, status: 200, json: async () => ({ ...BOOKED_BASE, circle_settlement }) };
    }
    return { ok: true, status: 200, json: async () => ({}) };
  }) as unknown as typeof fetch);
}

async function planTrip(
  findByTestId: (id: string) => Promise<HTMLElement>,
  getAllByText: (text: string, opts?: { selector?: string }) => HTMLElement[],
) {
  const input = await findByTestId('chat-input');
  await fireEvent.input(input, { target: { value: 'plan a week in Tokyo' } });
  await fireEvent.click(getAllByText('Send', { selector: 'button' })[0]);
  return findByTestId('status-banner');
}

describe('App — circle_settlement honesty (error object must not read as "settled")', () => {
  it('an ERROR circle_settlement renders NO "Settled with real USDC" copy, and states what went wrong', async () => {
    stubConfirmWith(CIRCLE_ERROR);
    const { findByTestId, getAllByText, getByTestId, queryByTestId } = render(App);
    await planTrip(findByTestId, getAllByText);
    await fireEvent.click(await findByTestId('confirm-book'));
    await waitFor(() => expect(getByTestId('status-banner')).toHaveTextContent('Booked'));

    // 1. The success banner must be absent — no claim that funds moved, anywhere.
    expect(queryByTestId('circle-settlement-result')).toBeNull();
    expect(queryByTestId('circle-explorer-link')).toBeNull();
    expect(document.body.textContent ?? '').not.toMatch(/Settled with real USDC/i);

    // 2. The failure IS disclosed, naming the error code, and says no USDC moved.
    const failed = await findByTestId('circle-settlement-failed');
    const text = failed.textContent ?? '';
    expect(text).toContain('CIRCLE_NOT_CONFIGURED');
    expect(text).toMatch(/no usdc was moved/i);
    // 3. …without contradicting the booking itself, which did commit.
    expect(text).toMatch(/booking .*still stands/i);
    // 4. No `undefined` leaking from the absent success fields.
    expect(text).not.toMatch(/undefined/);
  });

  it('a SUCCESS circle_settlement (status + transaction_id, no error) renders the settled copy and the explorer link', async () => {
    stubConfirmWith(CIRCLE_SUCCESS);
    const { findByTestId, getAllByText, getByTestId, queryByTestId } = render(App);
    await planTrip(findByTestId, getAllByText);
    await fireEvent.click(await findByTestId('confirm-book'));
    await waitFor(() => expect(getByTestId('status-banner')).toHaveTextContent('Booked'));

    const ok = await findByTestId('circle-settlement-result');
    expect(ok.textContent ?? '').toMatch(/Settled with real USDC/i);
    expect(ok.textContent ?? '').toContain('COMPLETE');
    expect(getByTestId('circle-explorer-link').getAttribute('href'))
      .toBe(CIRCLE_SUCCESS.block_explorer_url);
    expect(queryByTestId('circle-settlement-failed')).toBeNull();
  });

  it('no circle_settlement at all (opt-out) renders neither banner', async () => {
    stubConfirmWith(undefined);
    const { findByTestId, getAllByText, getByTestId, queryByTestId } = render(App);
    await planTrip(findByTestId, getAllByText);
    await fireEvent.click(await findByTestId('confirm-book'));
    await waitFor(() => expect(getByTestId('status-banner')).toHaveTextContent('Booked'));
    expect(queryByTestId('circle-settlement-result')).toBeNull();
    expect(queryByTestId('circle-settlement-failed')).toBeNull();
  });
});
