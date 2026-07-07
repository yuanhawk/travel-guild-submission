// @vitest-environment jsdom
//
// Aftercare.svelte — component-render tests for the in-component safePath() guard.
//
// safePath() is defined INSIDE Aftercare.svelte (not exported — mirrors, and must stay
// in lockstep with, api.safeHref()). Task #214 fixed a case-sensitivity bug where
// `s.startsWith('https://')` rejected safe URLs using any non-lowercase scheme casing
// (e.g. `Https://...`, `HTTPS://...`) even though URL schemes are case-INSENSITIVE per
// RFC 3986 §3.1. Since safePath() isn't exported, this file exercises it indirectly by
// rendering the component with alerts whose action_target.webpage_path uses varying
// scheme casing, then asserting on the rendered <a href> (or its safe fallback) —
// following the same render + fireEvent + mocked-fetch convention used by
// PlacePhotos.test.ts / RightRail.booking-links.test.ts.
//
// IMPORTANT: this only re-verifies the *effect* of the fix (which behaviorally matches
// api.safeHref()); direct unit coverage of the exported safeHref() itself lives in
// api.test.ts.

import { describe, it, expect, vi, afterEach } from 'vitest';
import { render, fireEvent } from '@testing-library/svelte';
import Aftercare from './Aftercare.svelte';
import type { NegotiateResult } from '../api';

function mockFetch(status: number, body: unknown) {
  const f = vi.fn(async () => ({
    ok: status >= 200 && status < 300,
    status,
    json: async () => body,
    text: async () => JSON.stringify(body),
  }));
  vi.stubGlobal('fetch', f);
  return f;
}

const BOOKED_RESULT: NegotiateResult = {
  outcome: 'success',
  payment_status: 'charged',
  booking_ref: 'BK-TEST-1',
  idempotency_key: 'trip-safepath-1',
  legs: [{ leg_id: 'leg-0', city: 'Kaohsiung' }],
};

function alertWithPath(webpage_path: string) {
  return {
    leg_id: 'leg-0',
    city: 'Kaohsiung',
    severity_tier: 'high' as const,
    summary: 'Typhoon approaching.',
    suggested_action: 'reconsider_leg' as const,
    action_target: {
      kind: 'webpage_review' as const,
      webpage_path,
    },
  };
}

async function mountAndCheck(alert: ReturnType<typeof alertWithPath>) {
  mockFetch(200, {
    outcome: 'ok',
    monitoring: { status: 'ok' },
    alerts: [alert],
  });
  const c = render(Aftercare, { props: { result: BOOKED_RESULT } });
  await fireEvent.click(c.getByTestId('aftercare-check-btn'));
  await new Promise<void>((r) => setTimeout(r, 0));
  return c;
}

afterEach(() => vi.restoreAllMocks());

describe('Aftercare.svelte — safePath() guard on reconsider_leg action_target.webpage_path', () => {
  it('lowercase https:// → renders the review anchor with the href unchanged', async () => {
    const c = await mountAndCheck(alertWithPath('https://travelguild.example.com/trip/x?focus=leg-0'));
    const btn = c.getByTestId('reconsider-leg-btn') as HTMLAnchorElement;
    expect(btn).toBeTruthy();
    expect(btn.getAttribute('href')).toBe('https://travelguild.example.com/trip/x?focus=leg-0');
  });

  it('mixed-case Https:// → still allowed (the #214 fix) — href returned unmodified, not lowercased', async () => {
    const c = await mountAndCheck(alertWithPath('Https://travelguild.example.com/trip/x?focus=leg-0'));
    const btn = c.getByTestId('reconsider-leg-btn') as HTMLAnchorElement;
    expect(btn).toBeTruthy();
    expect(btn.getAttribute('href')).toBe('Https://travelguild.example.com/trip/x?focus=leg-0');
  });

  it('upper-case HTTPS:// → allowed', async () => {
    const c = await mountAndCheck(alertWithPath('HTTPS://travelguild.example.com/trip/x'));
    expect(c.getByTestId('reconsider-leg-btn')).toBeTruthy();
  });

  it('a same-origin relative path → allowed (unchanged baseline)', async () => {
    const c = await mountAndCheck(alertWithPath('/trip/trip-safepath-1?focus=leg-0'));
    const btn = c.getByTestId('reconsider-leg-btn') as HTMLAnchorElement;
    expect(btn.getAttribute('href')).toBe('/trip/trip-safepath-1?focus=leg-0');
  });

  it('plain http:// (lowercase) → still rejected — no anchor, fallback text shown (deliberate HTTPS-only policy, unchanged)', async () => {
    const c = await mountAndCheck(alertWithPath('http://travelguild.example.com/trip/x'));
    expect(c.queryByTestId('reconsider-leg-btn')).toBeNull();
    expect(c.getByText('Review this leg carefully before travel.')).toBeTruthy();
  });

  it('mixed-case Http:// → also still rejected (insecure scheme blocked regardless of casing)', async () => {
    const c = await mountAndCheck(alertWithPath('Http://travelguild.example.com/trip/x'));
    expect(c.queryByTestId('reconsider-leg-btn')).toBeNull();
  });

  it('javascript: (any case) → rejected', async () => {
    const c = await mountAndCheck(alertWithPath('JavaScript:alert(1)'));
    expect(c.queryByTestId('reconsider-leg-btn')).toBeNull();
  });

  it('protocol-relative //host → rejected', async () => {
    const c = await mountAndCheck(alertWithPath('//evil.example.com/phish'));
    expect(c.queryByTestId('reconsider-leg-btn')).toBeNull();
  });
});
