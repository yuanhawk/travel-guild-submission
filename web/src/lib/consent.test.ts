import { describe, it, expect, vi, afterEach } from 'vitest';
import { uiState, confirmPlan } from './api';
import type { NegotiateResult } from './api';

// Slice 2 — the consent split (plan → review → /confirm). These guard the outcome→UI
// mapping + the confirm call shape (pure logic; component render is covered by Playwright).

describe('uiState — consent-split states', () => {
  it("plan_ready outcome → 'plan_ready' (HELD, awaiting the one confirm)", () => {
    expect(uiState({ outcome: 'plan_ready', payment_status: 'held' } as NegotiateResult)).toBe('plan_ready');
  });
  it("booked → 'success'", () => {
    expect(uiState({ outcome: 'success', booking_ref: 'BK-1' } as NegotiateResult)).toBe('success');
  });
  it("confirm ambiguity → 'reconcile' (retry SAME key, never re-book)", () => {
    expect(uiState({ outcome: 'cannot_satisfy', reason: 'commit_errored', needs_reconciliation: true } as NegotiateResult)).toBe('reconcile');
    expect(uiState({ outcome: 'cannot_satisfy', needs_reconciliation: true } as NegotiateResult)).toBe('reconcile');
  });
  it("expired / cancelled held plan → 'plan_expired'", () => {
    expect(uiState({ outcome: 'invalid_request', reason: 'unknown_plan' } as NegotiateResult)).toBe('plan_expired');
    expect(uiState({ outcome: 'invalid_request', reason: 'plan_cancelled' } as NegotiateResult)).toBe('plan_expired');
  });
  it("insufficient_funds still classified; carries total_cents (NOT package_total_cents — the bug)", () => {
    const r = { outcome: 'cannot_satisfy', reason: 'insufficient_funds', total_cents: 324000, wallet_balance_cents: 40000 } as NegotiateResult;
    expect(uiState(r)).toBe('insufficient_funds');
    expect(r.total_cents).toBe(324000);              // the field the UI must read
    expect((r as Record<string, unknown>).package_total_cents).toBeUndefined();
  });
});

describe('confirmPlan — POST /confirm {idempotency_key}', () => {
  afterEach(() => vi.restoreAllMocks());
  it('posts the idempotency_key and returns the booked envelope', async () => {
    const booked: NegotiateResult = {
      outcome: 'success', booking_ref: 'BK-9', payment_status: 'charged',
      wallet: { debited: true, debit_cents: 324000, balance_cents: 176000 },
    };
    const fetchMock = vi.fn(async () => ({ ok: true, status: 200, json: async () => booked }));
    vi.stubGlobal('fetch', fetchMock as unknown as typeof fetch);
    const res = await confirmPlan('trip-abc');
    expect(fetchMock).toHaveBeenCalledTimes(1);
    const [url, init] = (fetchMock.mock.calls[0] as unknown as [string, RequestInit]);
    expect(String(url)).toMatch(/\/confirm$/);
    expect(init.method).toBe('POST');
    expect(JSON.parse(String(init.body))).toMatchObject({ idempotency_key: 'trip-abc' });
    expect(res.booking_ref).toBe('BK-9');
    expect(res.wallet?.debited).toBe(true);
  });
});
