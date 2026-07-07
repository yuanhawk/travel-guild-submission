// Global Safety Watch helpers. DISTINCT from a plan's per-trip active_emergencies:
// this is the worldwide GET /emergencies watchlist, visible independent of any trip.
import { getEmergencies } from './api';
import type { EmergenciesResponse, EmergencyCountry } from './api';

export interface WatchSummary {
  tone: 'alert' | 'monitoring' | 'clear' | 'unavailable';
  active: number;       // Orange/Red (severity !== 'monitoring')
  monitoring: number;   // Green storms being tracked
  label: string;        // badge text
  asOf: string;
  source: string;
}

/** Pure: EmergenciesResponse → badge summary. Honest: 'unavailable' is NEVER 'clear'. */
export function summarizeWatch(r: EmergenciesResponse | null): WatchSummary {
  if (!r || r.status === 'unavailable') {
    return { tone: 'unavailable', active: 0, monitoring: 0, label: 'feed unavailable', asOf: r?.as_of ?? '', source: r?.source ?? '' };
  }
  const cs: EmergencyCountry[] = r.countries ?? [];
  const active = cs.filter((c) => c && c.severity !== 'monitoring').length;
  const monitoring = cs.filter((c) => c && c.severity === 'monitoring').length;
  if (active > 0)     return { tone: 'alert',      active, monitoring, label: `${active} active`,        asOf: r.as_of ?? '', source: r.source ?? '' };
  if (monitoring > 0) return { tone: 'monitoring', active, monitoring, label: `${monitoring} monitored`, asOf: r.as_of ?? '', source: r.source ?? '' };
  return { tone: 'clear', active, monitoring, label: 'all clear', asOf: r.as_of ?? '', source: r.source ?? '' };
}

/** Never throws — a network error becomes an honest 'unavailable' (≠ safe). */
export async function safeGetEmergencies(): Promise<EmergenciesResponse> {
  try { return await getEmergencies(); }
  catch { return { status: 'unavailable', source: 'live:gdacs', as_of: '', countries: [] }; }
}
