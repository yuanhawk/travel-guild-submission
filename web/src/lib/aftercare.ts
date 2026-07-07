// aftercare.ts — #100 AFTERCARE: types + API function for proactive risk-monitoring.
//
// CHANNEL-SECURITY BOUNDARY (non-negotiable):
//   - NO token/chat_id field exists on AftercareResult — the server never echoes them.
//   - aftercareCheck() is READ-ONLY: it never calls confirmPlan/cancel/replanTrip
//     on its own. The user triggers those from the consent flow on the webpage.
//   - All action_target paths are WEBPAGE routes (no transactional endpoint).
//
// var-0 SACRED: aftercare is off the _request_digest; it runs post-booking only.
// All monetary state is unchanged by this path.

import { API_BASE, replanTrip } from './api';
import type { NegotiateResult, ReplanOp, ReplanRejection } from './api';
import { buildIcs } from './ics';
import { authFields } from './session';

// ─── types ──────────────────────────────────────────────────────────────────

export type AftercareSuggestedAction =
  | 'switch_wet_weather_variant'
  | 'reconsider_leg'
  | 'resuggest_area_lodging'
  | 'monitor';

export type AftercareActionKind =
  | 'replan_switch_variant'
  | 'webpage_review'
  | 'webpage_rebook';

export interface AftercareActionTarget {
  kind: AftercareActionKind;
  webpage_path?: string;
  replan_op?: Record<string, unknown>;
}

export interface AftercareAlert {
  leg_id?: string;
  city?: string;
  iso2?: string;
  date_window?: { checkin?: string; checkout?: string };
  risk_type?: string;
  severity_tier: 'high' | 'medium' | 'monitoring';
  summary: string;
  summary_localized?: string;
  lang?: string;
  translated?: boolean;
  translation_note?: string;
  advice?: string;
  suggested_action: AftercareSuggestedAction;
  action_target?: AftercareActionTarget;
  source?: string;
  as_of?: string;
  beta?: boolean;
}

/** NOTE: this interface deliberately has NO token, chat_id, or telegram_token field.
 *  The server never echoes them. If a field named 'token' or 'chat_id' appears in
 *  a response, it is a server bug and MUST NOT be surfaced to the user. */
export interface AftercareTelegramStatus {
  attempted: boolean;
  sent: boolean;
  note?: string;
  // token and chat_id are intentionally absent — never echoed by the server
}

export interface AftercareMonitoring {
  status: 'ok' | 'unavailable';
  as_of?: string;
  source?: string;
  checked_legs?: number;
}

export interface AftercareResult {
  outcome: string;                         // ok | not_booked | unknown_trip | error
  idempotency_key?: string;
  monitoring?: AftercareMonitoring;
  alerts: AftercareAlert[];
  beta_note?: string | null;
  telegram?: AftercareTelegramStatus;      // NO token/chat_id field
}

// ─── API function ────────────────────────────────────────────────────────────

async function _aftercarePostJson<T>(path: string, body: unknown): Promise<T> {
  const r = await fetch(`${API_BASE}${path}`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
  if (!r.ok && r.status !== 402 && r.status !== 403) {
    const body = await r.text();
    console.debug(`${path} error body:`, body.slice(0, 500));
    throw new Error(`${path} -> HTTP ${r.status}`);
  }
  return (await r.json()) as T;
}

/**
 * Check the live risk/emergency feeds against a booked trip's legs+dates.
 * Returns alerts (suggest-only) + monitoring status + honest beta note.
 *
 * PURE READ: does not book, debit, or cancel anything. All rebooking routes
 * through the secure webpage consent flow (/confirm, /replan, /cancel).
 *
 * IDOR fix: threads session_token/owner_token via authFields() — the SAME
 * two-tier ownership proof sent on /confirm /cancel /replan /refine (see
 * session.ts authFields()) — so the backend's _authorize_trip_action gate
 * (added for VULN-AUTH-001) can actually authorize a real owner's request
 * instead of denying every Tier-1 (logged-in) and Tier-2 (anon-owner) trip.
 *
 * No token or chat_id is ever present in the response.
 */
export function aftercareCheck(
  idempotency_key: string,
  user_id?: string,
): Promise<AftercareResult> {
  return _aftercarePostJson<AftercareResult>('/aftercare/check', {
    idempotency_key, user_id, ...authFields(),
  });
}

// ─── replan helper (suggest-only trigger — page then re-downloads ICS) ──────

// Re-exported for callers that only import from aftercare.ts:
export type { ReplanRejection } from './api';

/**
 * Apply a switch_variant op (wet/fair weather switch) via /replan.
 * Returns the updated plan envelope so the caller can rebuild the ICS.
 * This is the ONLY transactional call the aftercare panel makes — it hits
 * /replan (plan-only, no money), not /confirm or /cancel.
 * `rejected` is {op,reason}[] (not string[]) — handled by the caller.
 * Accepts Record<string, unknown> (as served in AftercareActionTarget.replan_op)
 * and casts to ReplanOp for the typed API call.
 */
export async function applyVariantSwitch(
  idempotency_key: string,
  replan_op: Record<string, unknown>,
): Promise<import('./api').ReplanResponse> {
  return replanTrip(idempotency_key, [replan_op as unknown as ReplanOp]);
}

// ─── ICS helper (pure, no server) ────────────────────────────────────────────

/** Build a fresh ICS string from the updated plan envelope. Pure, deterministic. */
export function buildUpdatedIcs(plan: NegotiateResult): string {
  return buildIcs(plan);
}
