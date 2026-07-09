// Client-side session persistence (localStorage — origin-scoped, survives mobile
// browsers reclaiming a backgrounded tab's memory, which sessionStorage does not:
// a prior hardening pass moved this TO sessionStorage to shrink an XSS-theft
// window, but on real mobile devices "leave the app for a few minutes" silently
// evicted the tab's sessionStorage, dumping the user back to the login picker
// with their held trip lost (reported 2026-07-03; root cause confirmed — iOS
// Safari / Android Chrome discard backgrounded tabs' storage under memory
// pressure; the server's session_token TTL is 8h, ruling out a server-side
// cause). Reverted to localStorage — reviewed and judged
// justified: the assets held here are LOW-sensitivity in this deployment (the
// 5 demo accounts are passwordless / openly click-to-login already — see
// server.py's "shared judge fixtures" comment; owner_token below only gates a
// SIMULATED-wallet demo trip, no real money or PII). Exposure is still
// BOUNDED, not indefinite: entries carry a savedAt timestamp and are swept on
// load past SESSION_TTL_MS (8h, mirrors society/utils/session_token.py's
// server-side TTL so client/server lifetimes stay aligned). SECURITY FIX: the
// compensating control is a real Content-Security-Policy meta tag in
// index.html (script-src 'self' blocks the inline/injected <script>
// execution an XSS payload would need to read localStorage) — a prior
// version of this comment pointed at a backend Caddy config + Pages
// `_headers` file that never existed in this repo (this app ships to AliCloud
// OSS + CDN static hosting, not Cloudflare/Netlify Pages or a Caddy-fronted
// origin — see vite.config.ts). Also: session_token/owner_token are now sent
// as request headers, never URL query params, so they can no longer land in
// server/proxy access logs, browser history, or a Referer header regardless
// of the CSP. NO real auth/secrets are stored here — the demo user_id is
// replayed on every plan/confirm so the backend auto-applies that profile's
// prefs; guest = no user_id → system defaults (byte-identical to anonymous).
import type { DemoUser } from './api';

const KEY = 'tg_session';
const SESSION_TTL_MS = 8 * 60 * 60 * 1000; // 8h — mirrors society/utils/session_token.py's _SESSION_TTL_SECONDS

/** The active trip handle — updated after each successful /refine.
 *  `session_id` is the stable thread id; `idempotency_key` is the CURRENT held plan.
 *  /confirm must always use the LATEST idempotency_key. */
export interface ActivePlan {
  session_id: string;       // stable across re-plans
  idempotency_key: string;  // points at the CURRENT held plan (swapped on each refine)
}

export interface Session {
  user: DemoUser | null;        // null = guest
  guest: boolean;               // true once the user has explicitly chosen (user or guest)
  activePlan?: ActivePlan | null; // the current held trip (null = no active plan)
  // SECURITY (user_id-trust gap fix): server-verified session-possession proof,
  // issued via POST /session/login right after a demo user is picked (never set
  // for guests — they never reach the two endpoints that require it: PUT
  // /preferences and GET /telegram/link, both gated behind {#if activeUser}).
  // Threaded into those two calls; absent/invalid → the server returns 401.
  session_token?: string;
}

// On-disk envelope — savedAt is stripped before a Session reaches callers, so
// the public shape of loadSession()'s return value is unchanged by this TTL.
type StoredSession = Session & { savedAt: number };

export function loadSession(): Session | null {
  try {
    const raw = localStorage.getItem(KEY);
    if (!raw) return null;
    const stored = JSON.parse(raw) as StoredSession;
    if (typeof stored.savedAt !== 'number' || Date.now() - stored.savedAt > SESSION_TTL_MS) {
      localStorage.removeItem(KEY); // expired (or pre-TTL legacy entry) — sweep it
      return null;
    }
    const { savedAt: _savedAt, ...session } = stored;
    return session;
  } catch {
    return null;
  }
}

export function saveSession(s: Session): void {
  try {
    const stored: StoredSession = { ...s, savedAt: Date.now() };
    localStorage.setItem(KEY, JSON.stringify(stored));
  } catch { /* ignore */ }
}

export function clearSession(): void {
  try { localStorage.removeItem(KEY); } catch { /* ignore */ }
}

/** Persist the active plan handle (after a successful plan or refine). */
export function setActivePlan(s: Session, plan: ActivePlan | null): Session {
  return { ...s, activePlan: plan };
}

/** Clear the active plan (new trip / user switch). */
export function clearActivePlan(s: Session): Session {
  return { ...s, activePlan: null };
}

// ─── IDOR fix — trip-action ownership signals (Tier 2: anonymous trips) ────
// See the IDOR fix design spec (STORE-002 / VULN-AUTH-002 / VULN-AUTH-003):
// idempotency_key is a DETERMINISTIC digest (not a secret — it's echoed in
// GET /trips/{key} URLs), so it can never be the anonymous-trip ownership
// boundary. owner_token is a client-generated uuid4 secret, created once per
// browser session and bound to a trip write-once at creation (server-side,
// write-once via COALESCE(NULLIF(...))). Held in localStorage (see the
// top-of-file note) with the SAME SESSION_TTL_MS sweep as tg_session — this
// doesn't reduce a guest's access below the original design ("once per
// browser session"): it just makes "session" mean an 8h clock window instead
// of an OS-dependent, sometimes-5-minutes tab lifetime, so a guest is never
// worse off than before and is usually much better off. Never sent anywhere
// except our own backend.
const ANON_OWNER_KEY = 'tg_anon_owner';
let _anonOwnerTokenMem: string | null = null; // fallback if localStorage is unavailable

interface StoredAnonOwner { token: string; savedAt: number }

/** Lazily create (once per SESSION_TTL_MS window) and return this browser
 *  session's anonymous-trip ownership secret. Falls back to an in-memory
 *  token (not persisted) if localStorage throws/is unavailable, so callers
 *  always get a usable value. */
export function getAnonOwnerToken(): string {
  try {
    const raw = localStorage.getItem(ANON_OWNER_KEY);
    if (raw) {
      const stored = JSON.parse(raw) as StoredAnonOwner;
      if (stored.token && typeof stored.savedAt === 'number'
          && Date.now() - stored.savedAt <= SESSION_TTL_MS) {
        return stored.token;
      }
    }
    const fresh = crypto.randomUUID();
    localStorage.setItem(ANON_OWNER_KEY, JSON.stringify({ token: fresh, savedAt: Date.now() }));
    return fresh;
  } catch {
    if (!_anonOwnerTokenMem) _anonOwnerTokenMem = crypto.randomUUID();
    return _anonOwnerTokenMem;
  }
}

/** The two ownership signals threaded onto every trip-scoped mutation
 *  (/confirm, /cancel, /refine, /replan) AND (additively, since the M1 follow-up)
 *  the initial /negotiate_text plan-creation call. session_token proves possession
 *  of a logged-in demo session (Tier 1, verified server-side against the trip's
 *  row.user_id via verify_session); owner_token is this session's anonymous-trip
 *  secret (Tier 2). Both are always sent — the server picks whichever tier
 *  applies to the trip's ACTUAL owner (row.user_id), never trusting either value
 *  as a claim of WHICH trip is being acted on (that's still idempotency_key).
 *
 *  M1 follow-up: /negotiate_text has no trip row yet to verify
 *  session_token against (that's still deferred to /confirm — see server.py
 *  _authorize_trip_action), so sending session_token here does NOT change trip
 *  ownership semantics. It is consumed for exactly one narrower purpose: gating
 *  the travel_memory personalization write/read (server.py
 *  _memory_verified_user_id) so an unverified user_id claim can't steer another
 *  user's preference history. Absent/invalid → memory is simply skipped, never
 *  an error — additive and var-0-safe like owner_token. */
export function authFields(): { session_token?: string; owner_token: string } {
  return { session_token: loadSession()?.session_token, owner_token: getAnonOwnerToken() };
}
