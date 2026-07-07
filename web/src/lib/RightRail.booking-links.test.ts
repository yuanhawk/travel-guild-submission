// @vitest-environment jsdom
//
// COMPONENT test — #211: result.booking_links.{lodging,transport,visa,health,insurance}
// (society/utils/booking_links.py, tasks #37/#42) — the trip/leg-level (LOW-cardinality)
// portion of the CONSTRUCTED reference/handoff-link block. Every link is deterministically
// built (never fetched/live-verified) and must never imply a confirmed booking. This was
// computed but never rendered anywhere (#200 render-coverage sweep). Locks the fix:
//  1. Multiple categories (lodging/transport/visa/health/insurance) all render together,
//     not just one category.
//  2. Absent/null categories (e.g. visa: null, matching the real golden-fixture shape)
//     render nothing for that category — no fabrication, no crash.
//  3. safeHref() is genuinely used — a javascript:/data: booking_url must NOT produce a
//     clickable anchor (security regression guard, mirrors the live-price overlay pattern).
//  4. legs[].booking_link (the duplicated-in-place copy) is NOT a second render source —
//     rendering happens once, from the top-level booking_links block only.

import { describe, it, expect } from 'vitest';
import { render, fireEvent } from '@testing-library/svelte';
import RightRail from './components/RightRail.svelte';
import type { NegotiateResult, BookingLink } from './api';

const LODGING_LINK: BookingLink = {
  booking_url: 'https://www.google.com/maps/search/?api=1&query=Holiday%20Inn%20Singapore',
  kind: 'maps',
  label: 'Find on Google Maps — search Holiday Inn Singapore (not a confirmed booking)',
  providers: null,
};

const TRANSPORT_LINK: BookingLink = {
  booking_url: null,
  kind: 'meta_search',
  label: 'Compare flights SIN → BKK (meta-search — fares/availability not guaranteed)',
  providers: [
    { name: 'Google Flights', url: 'https://www.google.com/travel/flights?q=Flights%20from%20SIN%20to%20BKK' },
    { name: 'Skyscanner', url: 'https://www.skyscanner.net/transport/flights/sin/bkk/261005/' },
  ],
};

const VISA_LINK: BookingLink = {
  booking_url: 'https://www.mfa.gov.sg/visa',
  kind: 'gov_portal',
  label: 'Official entry / visa portal (verify requirements yourself)',
  providers: null,
};

const HEALTH_LINK: BookingLink = {
  booking_url: 'https://wwwnc.cdc.gov/travel/destinations/traveler/none/singapore',
  kind: 'cdc_who_portal',
  label: 'Travel-health guidance (CDC/WHO) — verify vaccinations yourself',
  providers: null,
};

const INSURANCE_LINK: BookingLink = {
  booking_url: null,
  kind: 'compare_note',
  label: 'Compare coverage independently (no vendor plan offered)',
  providers: null,
};

function resultWithBookingLinks(overrides: Record<string, unknown> = {}): NegotiateResult {
  return {
    outcome: 'success',
    legs: [{ leg_id: 'leg-0', city: 'Singapore', booking_link: LODGING_LINK }],
    booking_links: {
      lodging: [LODGING_LINK],
      transport: [TRANSPORT_LINK],
      visa: VISA_LINK,
      health: HEALTH_LINK,
      insurance: INSURANCE_LINK,
      ...overrides,
    },
  } as unknown as NegotiateResult;
}

async function openSafetyTab(getByText: (t: string) => HTMLElement) {
  await fireEvent.click(getByText('Safety'));
}

describe('#211: Safety tab renders trip/leg-level booking_links', () => {
  it('renders all categories (lodging/transport/visa/health/insurance) together, not just one', async () => {
    const result = resultWithBookingLinks();
    const { getByTestId, getAllByTestId, getByText } = render(RightRail, { props: { result } });
    await openSafetyTab(getByText);

    expect(getByTestId('trip-booking-links')).toBeTruthy();
    expect(getByTestId('booking-link-group-lodging')).toBeTruthy();
    expect(getByTestId('booking-link-group-transport')).toBeTruthy();
    expect(getByTestId('booking-link-group-visa')).toBeTruthy();
    expect(getByTestId('booking-link-group-health')).toBeTruthy();
    expect(getByTestId('booking-link-group-insurance')).toBeTruthy();

    expect(getByText(LODGING_LINK.label as string)).toBeTruthy();
    expect(getByText('Google Flights ↗')).toBeTruthy();
    expect(getByText('Skyscanner ↗')).toBeTruthy();
    expect(getByText(VISA_LINK.label as string)).toBeTruthy();
    expect(getByText(HEALTH_LINK.label as string)).toBeTruthy();
    expect(getByText(INSURANCE_LINK.label as string)).toBeTruthy();

    // 5 categories → 5 groups, and every non-provider entry contributes one row.
    expect(getAllByTestId('booking-link-item')).toHaveLength(5);
  });

  it('null/absent categories (matching the real fixture shape, e.g. visa: null) render nothing for them — no fabrication, no crash', async () => {
    const result = resultWithBookingLinks({ visa: null, transport: [] });
    const { queryByTestId, getByTestId, getByText } = render(RightRail, { props: { result } });
    await openSafetyTab(getByText);

    expect(getByTestId('trip-booking-links')).toBeTruthy();
    expect(queryByTestId('booking-link-group-visa')).toBeNull();
    expect(queryByTestId('booking-link-group-transport')).toBeNull();
    // The still-present categories still render.
    expect(getByTestId('booking-link-group-lodging')).toBeTruthy();
    expect(getByTestId('booking-link-group-health')).toBeTruthy();
    expect(getByTestId('booking-link-group-insurance')).toBeTruthy();
  });

  it('an entirely absent booking_links block renders no container at all', async () => {
    const result = { outcome: 'success', legs: [{ leg_id: 'leg-0', city: 'Singapore' }] } as unknown as NegotiateResult;
    const { queryByTestId, getByText } = render(RightRail, { props: { result } });
    await openSafetyTab(getByText);
    expect(queryByTestId('trip-booking-links')).toBeNull();
  });

  it('a compare_note with booking_url: null (insurance, #45 boundary) renders the label but no clickable link', async () => {
    const result = resultWithBookingLinks();
    const { getByTestId, getByText } = render(RightRail, { props: { result } });
    await openSafetyTab(getByText);
    const group = getByTestId('booking-link-group-insurance');
    expect(group.textContent).toContain(INSURANCE_LINK.label);
    expect(group.querySelector('a')).toBeNull();
  });

  it('safeHref() is genuinely used — an unsafe booking_url (javascript:) never becomes a clickable anchor', async () => {
    const result = resultWithBookingLinks({
      visa: { ...VISA_LINK, booking_url: 'javascript:alert(1)' },
    });
    const { getByTestId, getByText } = render(RightRail, { props: { result } });
    await openSafetyTab(getByText);
    const group = getByTestId('booking-link-group-visa');
    expect(group.textContent).toContain(VISA_LINK.label);
    expect(group.querySelector('a')).toBeNull();
  });

  it('a safe https booking_url renders a real target=_blank rel=noopener anchor via safeHref()', async () => {
    const result = resultWithBookingLinks();
    const { getByTestId, getByText } = render(RightRail, { props: { result } });
    await openSafetyTab(getByText);
    const group = getByTestId('booking-link-group-visa');
    const anchor = group.querySelector('a') as HTMLAnchorElement;
    expect(anchor).toBeTruthy();
    expect(anchor.getAttribute('href')).toBe(VISA_LINK.booking_url);
    expect(anchor.getAttribute('target')).toBe('_blank');
    expect(anchor.getAttribute('rel')).toBe('noopener noreferrer');
  });

  it('legs[].booking_link (the duplicated-in-place copy) is not a second render source — lodging renders once, not twice', async () => {
    const result = resultWithBookingLinks();
    const { getByTestId, getByText } = render(RightRail, { props: { result } });
    await openSafetyTab(getByText);
    const group = getByTestId('booking-link-group-lodging');
    // Exactly one lodging row/anchor even though legs[0].booking_link duplicates the
    // same object — proves the render reads booking_links.lodging[] only, once.
    expect(group.querySelectorAll('[data-testid="booking-link-item"]')).toHaveLength(1);
    expect(group.querySelectorAll('a')).toHaveLength(1);
  });
});
