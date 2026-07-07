// Client-side ICS (RFC 5545) export of a BOOKED itinerary. PURE + deterministic:
// derives ONLY from server-supplied legs/day_plans/booking_ref and invents NO clock
// times — untimed activities become all-day VALUE=DATE events; lodging uses the real
// leg check-in/out dates. Universal (Google / Apple / Outlook). No network, no recompute.

import type { NegotiateResult, DayPlan, Leg, DayDetail } from './api';
import { assumptionNotes } from './api';
import { mealAnchoredTimeline, displayName } from './itinerary';

const CRLF = '\r\n';

/** Escape ICS TEXT per RFC 5545 §3.3.11 (backslash, semicolon, comma, newline). */
export function escIcs(s: string): string {
  return String(s)
    .replace(/\\/g, '\\\\')
    .replace(/;/g, '\\;')
    .replace(/,/g, '\\,')
    .replace(/\r/g, '')
    .replace(/\n/g, '\\n');
}

/** UTF-8 byte length of a string (no allocation of the encoded buffer). */
function utf8Len(s: string): number {
  let n = 0;
  for (const ch of s) {                          // iterate by code POINTS
    const cp = ch.codePointAt(0) as number;
    n += cp < 0x80 ? 1 : cp < 0x800 ? 2 : cp < 0x10000 ? 3 : 4;
  }
  return n;
}

/** Fold a content line at 75 OCTETS per RFC 5545 §3.1 (continuation lines start with a space).
 *  Byte-aware + iterates by code point, so multibyte names never exceed 75 octets and a
 *  surrogate pair / multibyte codepoint is never split. Deterministic. */
function fold(line: string): string {
  if (utf8Len(line) <= 75) return line;
  const out: string[] = [];
  let cur = '';
  let curBytes = 0;
  let first = true;
  for (const ch of line) {                        // code points, not UTF-16 code units
    const cp = ch.codePointAt(0) as number;
    const b = cp < 0x80 ? 1 : cp < 0x800 ? 2 : cp < 0x10000 ? 3 : 4;
    const budget = first ? 75 : 74;               // continuation reserves 1 octet for the leading space
    if (curBytes + b > budget && cur !== '') {
      out.push(cur);
      cur = ch; curBytes = b; first = false;
    } else {
      cur += ch; curBytes += b;
    }
  }
  if (cur !== '') out.push(cur);
  return out.map((p, idx) => (idx === 0 ? p : ' ' + p)).join(CRLF);
}

/** "YYYY-MM-DD" (+addDays) → "YYYYMMDD" in UTC. Deterministic (explicit UTC, no wall-clock). */
export function dateOnly(iso: string, addDays = 0): string | null {
  const m = /^(\d{4})-(\d{2})-(\d{2})/.exec(iso || '');
  if (!m) return null;
  const d = new Date(Date.UTC(+m[1], +m[2] - 1, +m[3]));
  d.setUTCDate(d.getUTCDate() + addDays);
  const y = d.getUTCFullYear();
  const mo = String(d.getUTCMonth() + 1).padStart(2, '0');
  const da = String(d.getUTCDate()).padStart(2, '0');
  return `${y}${mo}${da}`;
}

interface VEvent {
  uid: string; stamp: string; startDate: string; endDate?: string;
  summary: string; location?: string; description?: string;
}
function vevent(e: VEvent): string[] {
  const lines = ['BEGIN:VEVENT', `UID:${e.uid}`, `DTSTAMP:${e.stamp}`, `DTSTART;VALUE=DATE:${e.startDate}`];
  if (e.endDate) lines.push(`DTEND;VALUE=DATE:${e.endDate}`);
  lines.push(`SUMMARY:${escIcs(e.summary)}`);
  if (e.location) lines.push(`LOCATION:${escIcs(e.location)}`);
  if (e.description) lines.push(`DESCRIPTION:${escIcs(e.description)}`);
  lines.push('END:VEVENT');
  return lines;
}

/** Build a complete VCALENDAR string for the (booked) trip. */
export function buildIcs(result: NegotiateResult): string {
  const ref = (result.booking_ref || 'trip').replace(/[^A-Za-z0-9_-]/g, '');
  const legs: Leg[] = result.legs ?? [];
  const legById = new Map<string, Leg>();
  legs.forEach((l, i) => legById.set(l.leg_id || `leg-${i}`, l));
  const plans: DayPlan[] = result.day_plans ?? [];

  // Deterministic DTSTAMP from the earliest real check-in (NOT wall-clock → stable output).
  const firstCheckin =
    legs.map((l) => l.checkin).find(Boolean) || plans.map((p) => p.checkin).find(Boolean) || '';
  const stamp = `${dateOnly(String(firstCheckin)) || '19700101'}T000000Z`;

  const body: string[] = [];
  plans.forEach((dp, pi) => {
    const leg = (dp.leg_id && legById.get(dp.leg_id)) || legs[pi];
    const city = dp.city || leg?.city || 'Destination';
    const checkin = String(dp.checkin || leg?.checkin || '');
    const checkout = String(dp.checkout || leg?.checkout || '');
    const ciDate = dateOnly(checkin);
    if (!ciDate) return; // no real date → emit nothing for this leg (never fabricate a date)

    // Lodging stay — all-day span over the real check-in → check-out.
    const coDate = dateOnly(checkout) || dateOnly(checkin, Math.max(1, dp.num_days || 1));
    body.push(...vevent({
      uid: `lodging-${ref}-${pi}@travelguild`, stamp,
      startDate: ciDate, endDate: coDate || undefined,
      summary: `Stay: ${city}`, location: city,
      description: leg?.unverified_lodging
        ? 'Lodging not yet verified — confirm/rebook on the webpage for security.' : '',
    }));

    // One all-day event per day, with the meal-anchored timeline as the description.
    (dp.days ?? []).forEach((day: DayDetail, di) => {
      const idx = day.day_index ?? di;
      const dayDate = dateOnly(checkin, idx);
      if (!dayDate) return;
      const items = mealAnchoredTimeline(day);
      const titles = items.filter((t) => t.kind === 'activity').map((t) => t.name).slice(0, 3);
      const summary = `${city} · Day ${idx + 1}${titles.length ? ': ' + titles.join(', ') : ''}`;
      const desc = items.map((t) => `${t.slot}: ${t.name}`).join('\n');
      body.push(...vevent({
        uid: `day-${ref}-${pi}-${di}@travelguild`, stamp,
        startDate: dayDate, summary, location: city, description: desc,
      }));
    });
  });

  // Calendar-level description carries the assistant's narrative overview (if any),
  // so a saved/exported .ics still has it days later — honestly caveated when the
  // backend has flagged it stale (written before a later structural /replan edit
  // that may have removed/added/reordered stops). Never fabricated: absent entirely
  // when there's no overview. Same honesty precedent extends to the parser's
  // assumption notes (#188) — the take-home artifact shouldn't hide guessed
  // date/party/currency/children any more than the live UI does.
  const overview = assistantSummary(result);
  const assum = assumptionNotes(result);
  const header = ['BEGIN:VCALENDAR', 'VERSION:2.0', 'PRODID:-//Travel Guild//Itinerary//EN',
    'CALSCALE:GREGORIAN', 'METHOD:PUBLISH'];
  const descParts: string[] = [];
  if (assum.length) descParts.push(`Note: ${assum.join(' ')}`);
  if (overview) {
    const stale = result.itinerary_narrative?.stale === true;
    const staleReason = result.itinerary_narrative?.stale_reason
      ?? 'This summary may be outdated after your latest edit.';
    descParts.push(stale ? `${staleReason}\n\n${overview}` : overview);
  }
  if (descParts.length) header.push(`X-WR-CALDESC:${escIcs(descParts.join('\n\n'))}`);

  const cal = [...header, ...body, 'END:VCALENDAR'];
  return cal.map(fold).join(CRLF) + CRLF;
}

/** Trigger a browser download of the .ics (safe no-op in non-DOM/test environments). */
export function downloadIcs(result: NegotiateResult): void {
  const ics = buildIcs(result);
  if (typeof document === 'undefined' || typeof URL === 'undefined' || !URL.createObjectURL) return;
  const blob = new Blob([ics], { type: 'text/calendar;charset=utf-8' });
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = `travel-guild-${(result.booking_ref || 'trip').replace(/[^A-Za-z0-9_-]/g, '')}.ics`;
  document.body.appendChild(a);
  a.click();
  a.remove();
  URL.revokeObjectURL(url);
}

/** The assistant's LLM overview IF present (LLM-on) — else null (LLM-off → no fabrication).
 *  itinerary_narrative is an OBJECT {overview, legs[]}; we surface the top-level overview string.
 *  Never fabricates a fallback: returns null when absent (deterministic LLM-off path). */
export function assistantSummary(result: NegotiateResult | null): string | null {
  const overview = result?.itinerary_narrative?.overview;
  if (typeof overview !== 'string') return null;
  const trimmed = overview.trim();
  return trimmed.length ? trimmed : null;
}
