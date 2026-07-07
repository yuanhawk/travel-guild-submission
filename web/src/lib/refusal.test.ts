// @vitest-environment jsdom
//
// COMPONENT test — the refusal / honest-decline journey (the safety-honesty moat).
// Mounts the real <App>, drives a chat request through the (mocked) planner that comes
// back `cannot_satisfy`, and asserts the honest-decline UI:
//   • the do-not-recommend / advisory reason is surfaced verbatim (no euphemism);
//   • it lands in the dedicated `state-declined` card;
//   • NOTHING is fabricated — no itinerary, no held/booked banner, no Confirm button.
// This is the unit/component counterpart to the e2e advisory-decline coverage.

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, fireEvent } from '@testing-library/svelte';

// Replace the streaming planner with a synchronous resolved result.
const declinedEnvelope = {
  outcome: 'cannot_satisfy',
  reason: "Pakistan is under a Level 4 “Do Not Travel” advisory — I won't book a trip there.",
};
vi.mock('./planStream', () => ({
  planStreaming: vi.fn(() => ({ done: Promise.resolve(declinedEnvelope), close: vi.fn() })),
}));
// No live safety feed in the test.
vi.mock('./safety', async (orig) => {
  const actual = await (orig() as Promise<Record<string, unknown>>);
  return { ...actual, safeGetEmergencies: vi.fn(async () => ({ as_of: null, items: [] })) };
});
// <Map> pulls in maplibre-gl (WebGL) which doesn't load under jsdom. It is NOT rendered
// in the declined state, so a no-op stub keeps the module graph importable.
vi.mock('./Map.svelte', () => ({ default: class { $on() {} $set() {} $destroy() {} } }));

import App from '../App.svelte';
import { planStreaming } from './planStream';
const planStreamingMock = planStreaming as unknown as ReturnType<typeof vi.fn>;

beforeEach(() => {
  // Guest session so the SessionPicker is bypassed and the app view renders directly.
  // (Session persistence moved sessionStorage → localStorage 2026-07-03 — see
  // session.ts's top comment; seed must match and needs a savedAt within the
  // 8h TTL or loadSession() sweeps it as expired.)
  localStorage.clear();
  localStorage.setItem('tg_session', JSON.stringify({ user: null, guest: true, savedAt: Date.now() }));
  planStreamingMock.mockClear();
});
afterEach(() => { localStorage.clear(); });

describe('App — refusal / honest-decline journey', () => {
  it('surfaces the advisory reason in a declined card and fabricates NOTHING', async () => {
    const { findByTestId, getAllByText, queryByTestId } = render(App);

    // Compose + send a request the planner will decline.
    const input = await findByTestId('chat-input');
    await fireEvent.input(input, { target: { value: 'plan a week in Pakistan' } });
    await fireEvent.click(getAllByText('Send', { selector: 'button' })[0]);

    // The planner WAS consulted (we didn't short-circuit before asking).
    expect(planStreamingMock).toHaveBeenCalledTimes(1);

    // Honest decline card, verbatim reason — no fabricated itinerary text.
    const card = await findByTestId('state-card');
    expect(card).toHaveTextContent('Do Not Travel');
    expect(card).toHaveTextContent("won't book");
    expect(card.className).toContain('state-declined');

    // No fabrication: no itinerary, no held/booked banner, no Confirm button.
    expect(queryByTestId('itinerary')).toBeNull();
    expect(queryByTestId('status-banner')).toBeNull();
    expect(queryByTestId('confirm-book')).toBeNull();
  });

  it('echoes the decline as an honest chat note (not a planned-trip message)', async () => {
    const { findByTestId, getAllByText } = render(App);
    const input = await findByTestId('chat-input');
    await fireEvent.input(input, { target: { value: 'book me a tour of an armed-conflict zone' } });
    await fireEvent.click(getAllByText('Send', { selector: 'button' })[0]);

    await findByTestId('state-card');
    const msgs = await findByTestId('chat-msgs');
    // The assistant's reply is the honest reason, NOT a "Planned … held" confirmation.
    expect(msgs).toHaveTextContent('Do Not Travel');
    expect(msgs).not.toHaveTextContent(/Planned .* held/);
  });
});
