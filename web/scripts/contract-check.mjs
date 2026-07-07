#!/usr/bin/env node
// Contract sync-check — the cross-repo drift gate.
//
// The frontend (web/) and the backend (this repo's root) are two halves of one repo
// coupled ONLY by the HTTP/SSE contract. Both run on the same box, so this script
// curls the LIVE backend and asserts it still serves the exact fields src/lib/api.ts
// (+ stream.ts / planStream.ts) depend on. Run it whenever either side changes:
//   npm run contract-check                 # full gate (LIVE backend + offline fixtures)
//   node scripts/contract-check.mjs --offline   # fixtures/types only, no backend needed
//
// The validators are factored out so the SAME assertions run against the live
// responses (drift gate) AND against golden fixtures (offline, CI-safe, executable
// documentation of the contract). The SSE frame schema (no live curl of an event
// stream) is validated against fixtures derived from src/lib/stream.ts + planStream.ts.
//
// Exit 0 = in sync; exit 1 = drift (prints the missing/renamed field).

const BASE = (process.env.VITE_API_BASE ?? 'http://localhost:8080').replace(/\/+$/, '');
const OFFLINE = process.argv.includes('--offline') || process.argv.includes('--no-live');
const isObj = (v) => v && typeof v === 'object' && !Array.isArray(v);

// ── shared validators (run against live responses AND offline fixtures) ──────
// Each takes the parsed value + an `ok(cond, msg)` collector scoped to a label.

function validateNegotiate(res, ok) {
  ok(isObj(res), 'negotiate result is not an object');
  ok(typeof res.outcome === 'string', 'result.outcome missing/not a string');
  ok(Array.isArray(res.legs), 'result.legs missing/not an array');
  // money is cents (one of these must be a number on a booked/feasible trip)
  ok(['total_booked_cents', 'package_total_cents', 'package_total_with_fees_cents']
      .some((k) => typeof res[k] === 'number'), 'no *_cents money field served');

  // risk_signals: consolidated object with per_leg[] (NOT a leg-keyed dict)
  const rs = res.risk_signals;
  if (rs != null) {
    ok(isObj(rs), 'result.risk_signals present but not an object');
    ok(Array.isArray(rs?.per_leg), 'risk_signals.per_leg missing/not an array (contract drift!)');
    const lr = rs?.per_leg?.[0];
    if (lr) {
      ok(typeof lr.alert_tier === 'string', 'per_leg[0].alert_tier missing');
      ok('decisions' in lr && isObj(lr.decisions),
        "per_leg[0].decisions missing/not an object (it must be `decisions{}`, not `decision`)");
      ok('advisory' in lr && Array.isArray(lr.advisory),
        "per_leg[0].advisory missing/not an array (it must be `advisory[]`, not `advisory_level`)");
      ok(['cyclone_likelihood_pct', 'flood_index_bp', 'wildfire_likelihood_pct', 'drought_index_bp']
          .some((k) => k in lr), 'per_leg[0] carries no hazard field');
    }
  }

  // active_emergencies: App.svelte renders e.status, e.city, e.leg_id, e.headline
  if (Array.isArray(res.active_emergencies) && res.active_emergencies.length) {
    const ae = res.active_emergencies[0];
    ok('status' in ae, `active_emergencies[0] missing 'status'`);
    // city + leg_id are both optional in ActiveEmergency (RightRail renders e.city ?? e.leg_id);
    // require at least one so the chip has a label, not both.
    ok(('city' in ae) || ('leg_id' in ae), `active_emergencies[0] needs at least one of city/leg_id`);
    ok(['active', 'monitoring', 'clear', 'unavailable'].includes(ae.status),
      `active_emergencies[0].status must be active|monitoring|clear|unavailable, got: ${ae?.status}`);
  }
}

function validateEmergencies(e, ok) {
  ok(['ok', 'unavailable'].includes(e?.status), `/emergencies.status invalid: ${e?.status}`);
  ok(Array.isArray(e?.countries), '/emergencies.countries missing/not an array');
  const c = e?.countries?.[0];
  if (c) {
    for (const k of ['iso2', 'hazard', 'severity'])
      ok(k in c, `/emergencies country missing '${k}'`);
  }
}

// /replan (api.ts ReplanResponse). NEVER books/debits; FE renders the `plan` envelope.
const REPLAN_OUTCOMES = ['plan_ready', 'noop', 'unknown_plan', 'plan_locked', 'cannot_satisfy', 'invalid_request'];
function validateReplan(r, ok) {
  ok(isObj(r), '/replan result is not an object');
  ok(typeof r?.outcome === 'string', '/replan.outcome missing/not a string');
  ok(REPLAN_OUTCOMES.includes(r?.outcome), `/replan.outcome unknown: ${r?.outcome} (api.ts ReplanOutcome drift)`);
  for (const k of ['applied', 'rejected', 'notes'])
    ok(Array.isArray(r?.[k]), `/replan.${k} missing/not an array`);
  // The plan envelope is ALWAYS echoed (updated on plan_ready, UNCHANGED on noop/cannot_satisfy)
  // — the FE never fabricates a plan, so this field is load-bearing.
  ok(isObj(r?.plan), '/replan.plan missing/not an object (FE renders the server envelope; never fabricates)');
  // rejections are honest {op, reason} pairs
  const rej = r?.rejected?.[0];
  if (rej) {
    ok(typeof rej.op === 'string' && typeof rej.reason === 'string',
      '/replan.rejected[0] must be {op, reason} (honest stale-board failure)');
  }
}

// /confirm (api.ts NegotiateResult). THE one consent step → booked.
function validateConfirm(b, ok) {
  ok(isObj(b), '/confirm result is not an object');
  ok(typeof b?.outcome === 'string', '/confirm.outcome missing/not a string');
  if (b?.outcome === 'success') {
    ok(typeof b?.booking_ref === 'string' && b.booking_ref.length > 0,
      '/confirm success must carry a booking_ref');
    ok(isObj(b?.wallet), '/confirm success must carry a wallet object (SIMULATED debit)');
  }
}

// SSE frame schema (src/lib/stream.ts StreamEvent + planStream.ts consumption).
const WALLET_OPS = ['seed', 'debit', 'gate_blocked', 'credit'];
function validateStreamFrame(f, ok) {
  ok(isObj(f), 'SSE frame is not an object');
  ok(typeof f?.type === 'string', 'SSE frame.type missing/not a string (the discriminator)');
  if ('seq' in f) ok(typeof f.seq === 'number', 'SSE frame.seq present but not a number');
  if ('ts_ms' in f) ok(typeof f.ts_ms === 'number', 'SSE frame.ts_ms present but not a number');
  if ('data' in f && f.data != null) ok(isObj(f.data), 'SSE frame.data present but not an object');
  // wallet frames carry data.op from the WalletOp union the rail UI keys off.
  if (f?.type === 'wallet') {
    ok(isObj(f?.data) && WALLET_OPS.includes(f.data.op),
      `SSE wallet frame.data.op must be one of ${WALLET_OPS.join('|')} (got ${f?.data?.op})`);
  }
  // planStream.ts resolves the trip ONLY off negotiate_finished.result — if the final
  // frame drops `result`, the FE degrades to the blocking fallback. So the contract is:
  // the terminal frame MUST carry the full plan envelope.
  if (f?.type === 'negotiate_finished') {
    ok(isObj(f?.result), 'SSE negotiate_finished frame.result missing (FE would degrade to fallback)');
    if (isObj(f?.result)) validateNegotiate(f.result, ok);
  }
}

// ── golden fixtures (the shapes the FE is hard-coded to consume) ─────────────
const FIXTURES = {
  replan_plan_ready: {
    outcome: 'plan_ready', idempotency_key: 'trip-1',
    applied: ['remove:leg0.day0.pos0:nishiki-market'], rejected: [], notes: [],
    plan: { outcome: 'plan_ready', legs: [{ leg_id: 'leg-0', city: 'Kyoto' }],
            package_total_with_fees_cents: 180000, day_plans: [] },
  },
  replan_noop: {
    outcome: 'noop', idempotency_key: 'trip-1', applied: [],
    rejected: [{ op: 'remove_place', reason: 'attraction not found at position' }], notes: [],
    plan: { outcome: 'plan_ready', legs: [], package_total_cents: 180000 },
  },
  confirm_success: {
    outcome: 'success', booking_ref: 'BK-1', payment_status: 'charged',
    legs: [], wallet: { balance_cents: 176000, debited: true, debit_cents: 324000 },
  },
  stream_frames: [
    { seq: 0, ts_ms: 1, type: 'phase', summary: 'parsing' },
    { seq: 1, type: 'agent', agent: 'risk', round: 1, summary: 'screening advisories' },
    { seq: 2, type: 'wallet', data: { op: 'seed', balance_cents: 500000 } },
    { seq: 3, type: 'wallet', data: { op: 'debit', amount_cents: 180000 } },
    { seq: 4, type: 'risk', data: { iso2: 'JP', alert_tier: 'normal' } },
    {
      seq: 5, type: 'negotiate_finished',
      result: {
        outcome: 'plan_ready', legs: [{ leg_id: 'leg-0', city: 'Kyoto' }],
        package_total_with_fees_cents: 180000,
        risk_signals: { per_leg: [{ alert_tier: 'normal', decisions: {}, advisory: [], flood_index_bp: 0 }] },
      },
    },
  ],
};

function runOffline() {
  const fails = [];
  const scoped = (label) => (cond, msg) => { if (!cond) fails.push(`[${label}] ${msg}`); };

  validateReplan(FIXTURES.replan_plan_ready, scoped('replan:plan_ready'));
  validateReplan(FIXTURES.replan_noop, scoped('replan:noop'));
  validateConfirm(FIXTURES.confirm_success, scoped('confirm:success'));
  FIXTURES.stream_frames.forEach((f, i) => validateStreamFrame(f, scoped(`sse:frame[${i}](${f.type})`)));
  // The negotiate envelope shape is also exercised via the terminal SSE frame above,
  // plus a direct pass for completeness.
  validateNegotiate(FIXTURES.stream_frames.at(-1).result, scoped('negotiate(fixture)'));
  return fails;
}

async function runLive() {
  const fails = [];
  const ok = (cond, msg) => { if (!cond) fails.push(msg); };

  // ── /negotiate_text ───────────────────────────────────────────────────────
  let res;
  try {
    const r = await fetch(`${BASE}/negotiate_text`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ text: '5 days in Tokyo in October 2026, ~$2500, solo' }),
    });
    res = await r.json();
  } catch (e) {
    console.error(`✗ cannot reach ${BASE}/negotiate_text — is the backend running? (${e})`);
    console.error('  (run with --offline to validate the fixtures/SSE schema without a backend.)');
    process.exit(1);
  }
  validateNegotiate(res, ok);

  // ── /emergencies ──────────────────────────────────────────────────────────
  try {
    const e = await (await fetch(`${BASE}/emergencies`)).json();
    validateEmergencies(e, ok);
  } catch (e) {
    fails.push(`/emergencies unreachable or non-JSON (${e})`);
  }

  return fails;
}

async function main() {
  // Offline fixture + SSE-schema checks ALWAYS run (free, CI-safe).
  const offlineFails = runOffline();
  const liveFails = OFFLINE ? [] : await runLive();
  const fails = [...offlineFails, ...liveFails];

  if (fails.length) {
    console.error('✗ CONTRACT DRIFT — frontend (api.ts / stream.ts) expects fields no longer served:');
    for (const f of fails) console.error('   • ' + f);
    process.exit(1);
  }
  const scope = OFFLINE
    ? 'offline fixtures: negotiate + /replan + /confirm + SSE frame schema'
    : `LIVE ${BASE} (negotiate + risk_signals.per_leg + /emergencies) + offline fixtures (/replan, /confirm, SSE frames)`;
  console.log(`✓ contract in sync — ${scope}`);
}

main();
