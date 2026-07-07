// @vitest-environment jsdom
//
// <ChatPane> STATE-LOGIC + render-branch contract (component-mounted, jsdom).
//
// Scope (deliberately NON-overlapping with e2e/assistant-flyout.spec.ts, which owns
// the hover open/close DOM choreography):
//   1. render branches — embedded (no plan) vs floating (plan active) modes.
//   2. flyout open via the click trigger (openFlyout) + aria-hidden / .open contract.
//   3. pin toggle (togglePin) — class, label, and the unpin→scheduleClose timer.
//   4. the auto-surface reactive — the subtle one:
//        • snapshots the message count on the FIRST hasPlan tick,
//        • does NOT surface the plan-summary already present at that tick,
//        • opens on a LATER non-user message,
//        • does NOT open on a later USER message,
//        • resets the snapshot when hasPlan drops back to false.
//
// The functions (openFlyout/scheduleClose/cancelClose/togglePin/_msgSeen reactive) are
// component-internal, so we exercise them through the public surface: the click handlers
// and the reactive `hasPlan`/`messages` props via rerender(). Hover (mouseenter/leave →
// scheduleClose/cancelClose) is e2e's; we only assert the unpin→scheduleClose branch here.

import { describe, it, expect, vi, afterEach } from 'vitest';
import { render, fireEvent } from '@testing-library/svelte';
import { tick } from 'svelte';
import ChatPane from './components/ChatPane.svelte';

type Msg = { role: 'user' | 'bot' | 'note'; text: string };
const PLAN_SUMMARY: Msg = { role: 'bot', text: 'Here is your 5-day Tokyo plan.' };
const REMEDIATION: Msg = { role: 'note', text: "Tap Confirm again — you weren't charged." };
const USER_MSG: Msg = { role: 'user', text: 'add a day in Kyoto' };

const isOpen = (el: Element) =>
  el.classList.contains('open') && el.getAttribute('aria-hidden') === 'false';

afterEach(() => {
  vi.useRealTimers();
});

// ── 1. render branches ───────────────────────────────────────────────────────
describe('<ChatPane> — render mode branches', () => {
  it('embedded mode (no plan): renders chat-pane + actionable composer, NO floating trigger', () => {
    const { getByTestId, queryByTestId } = render(ChatPane, {
      props: { hasPlan: false, messages: [{ role: 'bot', text: 'hi' }] as Msg[] },
    });
    expect(getByTestId('chat-pane')).toBeTruthy();
    expect(getByTestId('chat-input')).toBeTruthy();
    expect(queryByTestId('assistant-trigger')).toBeNull();
    expect(queryByTestId('assistant-flyout')).toBeNull();
  });

  // Finding #9 (map/mobile UX sweep): the mobile slide-up sheet's own composer
  // (.mob-sheet-box, CSS-hidden on desktop but always mounted, same pattern as
  // .mob-bubble/.mob-sheet above) had NO data-testid at all — mobile e2e specs
  // could only reach it via the `.mob-sheet-box textarea`/`.mob-sheet-box .send`
  // CSS-selector workaround, since the only "chat-input" testid belonged to the
  // desktop box. Distinct mob-* testids (not a second "chat-input") so both boxes
  // being simultaneously mounted never makes getByTestId('chat-input') ambiguous.
  it('the mobile sheet composer (.mob-sheet-box) is independently reachable via data-testid', () => {
    const { getByTestId } = render(ChatPane, {
      props: { hasPlan: false, messages: [{ role: 'bot', text: 'hi' }] as Msg[] },
    });
    expect(getByTestId('mob-chat-input')).toBeTruthy();
    expect(getByTestId('mob-chat-send')).toBeTruthy();
    // The two composers are genuinely distinct DOM nodes, not the same node
    // queried twice under different names.
    expect(getByTestId('mob-chat-input')).not.toBe(getByTestId('chat-input'));
  });

  it('floating mode (plan active): renders the trigger + flyout, NO embedded pane', () => {
    const { getByTestId, queryByTestId } = render(ChatPane, {
      props: { hasPlan: true, messages: [PLAN_SUMMARY] },
    });
    expect(getByTestId('assistant-trigger')).toBeTruthy();
    expect(getByTestId('assistant-flyout')).toBeTruthy();
    expect(queryByTestId('chat-pane')).toBeNull();
  });

  it('collapsed embedded mode hides the message list / composer', () => {
    const { queryByTestId } = render(ChatPane, {
      props: { hasPlan: false, collapsed: true, messages: [{ role: 'bot', text: 'hi' }] as Msg[] },
    });
    expect(queryByTestId('chat-msgs')).toBeNull();
    expect(queryByTestId('chat-input')).toBeNull();
  });

  it('renders the "Planning…" note while loading (both modes keep chat-msgs in DOM)', () => {
    const { getByTestId } = render(ChatPane, {
      props: { hasPlan: true, loading: true, messages: [PLAN_SUMMARY] },
    });
    expect(getByTestId('chat-msgs').textContent).toContain('Planning…');
  });

  it('floating-mode chat-msgs is ALWAYS in the DOM even while the flyout is closed', () => {
    // contract noted in the component: Playwright toContainText must work when closed.
    const { getByTestId } = render(ChatPane, {
      props: { hasPlan: true, messages: [PLAN_SUMMARY, REMEDIATION] },
    });
    const flyout = getByTestId('assistant-flyout');
    expect(isOpen(flyout)).toBe(false); // closed on mount
    expect(getByTestId('chat-msgs').textContent).toContain(REMEDIATION.text);
  });
});

// ── 2. openFlyout via the click trigger ──────────────────────────────────────
describe('<ChatPane> — openFlyout (click trigger)', () => {
  it('flyout starts closed and opens on a trigger click', async () => {
    const { getByTestId } = render(ChatPane, {
      props: { hasPlan: true, messages: [PLAN_SUMMARY] },
    });
    const flyout = getByTestId('assistant-flyout');
    expect(isOpen(flyout)).toBe(false);
    expect(flyout.getAttribute('aria-hidden')).toBe('true');

    await fireEvent.click(getByTestId('assistant-trigger'));
    expect(isOpen(flyout)).toBe(true);
    expect(flyout.getAttribute('aria-hidden')).toBe('false');
  });

  it('trigger gets mob-open-hide while the flyout is open, closeFlyout clears it', async () => {
    // The actual hiding is a mobile-only (max-width:768px) CSS rule -- jsdom doesn't
    // evaluate media queries, so this locks the class-toggle contract the CSS depends
    // on, not the visual outcome. Desktop is unaffected because there's no matching
    // rule for this class outside that media query (see the component's <style> block).
    const { getByTestId } = render(ChatPane, {
      props: { hasPlan: true, messages: [PLAN_SUMMARY] },
    });
    const trigger = getByTestId('assistant-trigger');
    expect(trigger.classList.contains('mob-open-hide')).toBe(false);

    await fireEvent.click(trigger);
    expect(trigger.classList.contains('mob-open-hide')).toBe(true);

    await fireEvent.click(getByTestId('assistant-close'));
    expect(trigger.classList.contains('mob-open-hide')).toBe(false);
  });
});

// ── 3. togglePin (togglePin + unpin→scheduleClose) ───────────────────────────
describe('<ChatPane> — togglePin', () => {
  it('toggles the pinned class + label on each pin click', async () => {
    const { getByTestId } = render(ChatPane, {
      props: { hasPlan: true, messages: [PLAN_SUMMARY] },
    });
    const pin = getByTestId('assistant-pin');
    expect(pin.classList.contains('pinned')).toBe(false);
    expect(pin.textContent).toContain('Pin');
    expect(pin.textContent).not.toContain('Pinned');

    await fireEvent.click(pin);
    expect(pin.classList.contains('pinned')).toBe(true);
    expect(pin.textContent).toContain('Pinned');

    await fireEvent.click(pin);
    expect(pin.classList.contains('pinned')).toBe(false);
    expect(pin.textContent).not.toContain('Pinned');
  });

  it('unpinning an open flyout schedules a deferred close (scheduleClose, 350ms)', async () => {
    vi.useFakeTimers();
    const { getByTestId } = render(ChatPane, {
      props: { hasPlan: true, messages: [PLAN_SUMMARY] },
    });
    const flyout = getByTestId('assistant-flyout');
    const pin = getByTestId('assistant-pin');

    await fireEvent.click(getByTestId('assistant-trigger')); // open
    await fireEvent.click(pin);                              // pin → no auto-close
    expect(isOpen(flyout)).toBe(true);

    await fireEvent.click(pin);                              // unpin → scheduleClose()
    // still open immediately after unpin (timer pending, not yet fired)
    expect(isOpen(flyout)).toBe(true);

    vi.advanceTimersByTime(350);
    await tick();
    expect(isOpen(flyout)).toBe(false);
  });

  it('while pinned, scheduleClose is a no-op — clicking the (open) trigger keeps it open', async () => {
    vi.useFakeTimers();
    const { getByTestId } = render(ChatPane, {
      props: { hasPlan: true, messages: [PLAN_SUMMARY] },
    });
    const flyout = getByTestId('assistant-flyout');
    await fireEvent.click(getByTestId('assistant-trigger')); // open
    await fireEvent.click(getByTestId('assistant-pin'));     // pin

    // pinned: even after the close window elapses it stays open
    vi.advanceTimersByTime(1000);
    await tick();
    expect(isOpen(flyout)).toBe(true);
  });
});

// ── 4. auto-surface reactive (the crux) ──────────────────────────────────────
describe('<ChatPane> — auto-surface reactive', () => {
  it('does NOT surface the plan-summary present on the first hasPlan tick', async () => {
    const { getByTestId } = render(ChatPane, {
      props: { hasPlan: true, messages: [PLAN_SUMMARY] },
    });
    await tick();
    expect(isOpen(getByTestId('assistant-flyout'))).toBe(false);
  });

  it('opens on a LATER non-user message (remediation note) appended after the plan', async () => {
    const { getByTestId, rerender } = render(ChatPane, {
      props: { hasPlan: true, messages: [PLAN_SUMMARY] },
    });
    const flyout = getByTestId('assistant-flyout');
    expect(isOpen(flyout)).toBe(false);

    await rerender({ hasPlan: true, messages: [PLAN_SUMMARY, REMEDIATION] });
    await tick();
    expect(isOpen(flyout)).toBe(true);
  });

  it('does NOT open on a later USER message, but DOES on the next assistant message', async () => {
    const { getByTestId, rerender } = render(ChatPane, {
      props: { hasPlan: true, messages: [PLAN_SUMMARY] },
    });
    const flyout = getByTestId('assistant-flyout');

    await rerender({ hasPlan: true, messages: [PLAN_SUMMARY, USER_MSG] });
    await tick();
    expect(isOpen(flyout)).toBe(false); // user's own echo must not pop the flyout

    await rerender({ hasPlan: true, messages: [PLAN_SUMMARY, USER_MSG, REMEDIATION] });
    await tick();
    expect(isOpen(flyout)).toBe(true);  // the assistant reply does
  });

  it('snapshots on the false→true transition: the summary already present is NOT surfaced', async () => {
    // Embedded first (no plan), then the plan renders WITH the summary already in the list.
    const { getByTestId, queryByTestId, rerender } = render(ChatPane, {
      props: { hasPlan: false, messages: [PLAN_SUMMARY] },
    });
    expect(queryByTestId('assistant-flyout')).toBeNull();

    await rerender({ hasPlan: true, messages: [PLAN_SUMMARY] });
    await tick();
    expect(isOpen(getByTestId('assistant-flyout'))).toBe(false);

    // a genuinely new assistant message after the plan still surfaces
    await rerender({ hasPlan: true, messages: [PLAN_SUMMARY, REMEDIATION] });
    await tick();
    expect(isOpen(getByTestId('assistant-flyout'))).toBe(true);
  });

  it('resets the snapshot when hasPlan drops to false, then re-snapshots on the next plan', async () => {
    const { getByTestId, rerender } = render(ChatPane, {
      props: { hasPlan: true, messages: [PLAN_SUMMARY, REMEDIATION] },
    });
    // surface it, then tear the plan down
    await rerender({ hasPlan: false, messages: [PLAN_SUMMARY, REMEDIATION] });
    await tick();

    // new plan with the SAME (already-seen) messages must re-snapshot, not re-open
    await rerender({ hasPlan: true, messages: [PLAN_SUMMARY, REMEDIATION] });
    await tick();
    expect(isOpen(getByTestId('assistant-flyout'))).toBe(false);
  });
});
