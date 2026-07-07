import { describe, it, expect } from 'vitest';
import {
  initProgress, reduceProgress, AGENT_ROSTER, hasFlag,
} from './progress';
import type { ProgressState } from './progress';

// ─── initProgress ──────────────────────────────────────────────────────────
describe('initProgress', () => {
  it('produces 12 rows matching AGENT_ROSTER order', () => {
    const s = initProgress();
    expect(s.rows).toHaveLength(12);
    expect(s.rows.map((r) => r.name)).toEqual(AGENT_ROSTER.map((a) => a.name));
  });

  it('all rows start pending with no verdict or elapsed time', () => {
    const s = initProgress();
    for (const r of s.rows) {
      expect(r.status).toBe('pending');
      expect(r.verdict).toBe('');
      expect(r.elapsedMs).toBeNull();
      expect(r.flagged).toBe(false);
      expect(r.startedMs).toBeNull();
    }
  });

  it('finished:false, round:0, flags:0', () => {
    const s = initProgress();
    expect(s.finished).toBe(false);
    expect(s.round).toBe(0);
    expect(s.flags).toBe(0);
    expect(s.startedMs).toBeNull();
  });
});

// ─── hasFlag ───────────────────────────────────────────────────────────────
describe('hasFlag', () => {
  it('detects FLAG, BLOCK, MEDEVAC, REJECTED (case-insensitive)', () => {
    expect(hasFlag('FLAG — risk')).toBe(true);
    expect(hasFlag('BLOCK')).toBe(true);
    expect(hasFlag('medevac required')).toBe(true);
    expect(hasFlag('REJECTED score')).toBe(true);
  });
  it('returns false for CLEAR and other non-flag text', () => {
    expect(hasFlag('CLEAR')).toBe(false);
    expect(hasFlag('visa ok')).toBe(false);
    expect(hasFlag(undefined)).toBe(false);
    expect(hasFlag('')).toBe(false);
  });
});

// ─── reduceProgress — phase ────────────────────────────────────────────────
describe('reduceProgress — phase event', () => {
  it('updates phase from summary', () => {
    const s = initProgress();
    const next = reduceProgress(s, { type: 'phase', summary: 'Parsing intent (LLM)…' });
    expect(next.phase).toBe('Parsing intent (LLM)…');
  });

  it('narrator_done → "Finalizing your plan…"', () => {
    const s = initProgress();
    const next = reduceProgress(s, { type: 'phase', agent: 'narrator_done', summary: 'ignored' });
    expect(next.phase).toBe('Finalizing your plan…');
  });
});

// ─── negotiate_started ─────────────────────────────────────────────────────
describe('reduceProgress — negotiate_started', () => {
  it('resets rows + sets startedMs from ts_ms', () => {
    const s = initProgress();
    // first dirty a row
    const dirty = reduceProgress(s, { type: 'agent_started', agent: 'Risk', ts_ms: 500 });
    expect(dirty.rows.find((r) => r.name === 'Risk')?.status).toBe('running');

    const fresh = reduceProgress(dirty, { type: 'negotiate_started', ts_ms: 1000 });
    expect(fresh.startedMs).toBe(1000);
    expect(fresh.rows.every((r) => r.status === 'pending')).toBe(true);
    expect(fresh.phase).toBe('Negotiating across agents…');
  });
});

// ─── round_started ─────────────────────────────────────────────────────────
describe('reduceProgress — round_started', () => {
  it('updates round number', () => {
    const s = initProgress();
    const next = reduceProgress(s, { type: 'round_started', round: 2 });
    expect(next.round).toBe(2);
  });
  it('ignores non-numeric round', () => {
    const s = initProgress();
    const next = reduceProgress(s, { type: 'round_started' });
    expect(next).toBe(s);   // same reference — no recognized change
  });
});

// ─── agent_started ─────────────────────────────────────────────────────────
describe('reduceProgress — agent_started', () => {
  it('sets Risk row to running with startedMs', () => {
    const s = initProgress();
    const next = reduceProgress(s, { type: 'agent_started', agent: 'Risk', ts_ms: 1000 });
    const row = next.rows.find((r) => r.name === 'Risk')!;
    expect(row.status).toBe('running');
    expect(row.startedMs).toBe(1000);
    expect(row.elapsedMs).toBeNull();
  });

  it('unknown agent → state unchanged (forward-compat)', () => {
    const s = initProgress();
    const next = reduceProgress(s, { type: 'agent_started', agent: 'Ghost', ts_ms: 1 });
    expect(next).toBe(s);
  });
});

// ─── agent_completed ───────────────────────────────────────────────────────
describe('reduceProgress — agent_completed', () => {
  it('CLEAR: done, elapsedMs computed, flagged:false', () => {
    const s = initProgress();
    const started = reduceProgress(s, { type: 'agent_started', agent: 'Risk', ts_ms: 1000 });
    const done = reduceProgress(started, { type: 'agent_completed', agent: 'Risk', summary: 'CLEAR', ts_ms: 1300 });
    const row = done.rows.find((r) => r.name === 'Risk')!;
    expect(row.status).toBe('done');
    expect(row.elapsedMs).toBe(300);
    expect(row.flagged).toBe(false);
    expect(done.flags).toBe(0);
  });

  it('FLAG: flagged:true AND flags incremented', () => {
    const s = initProgress();
    const started = reduceProgress(s, { type: 'agent_started', agent: 'Compliance', ts_ms: 100 });
    const done = reduceProgress(started, { type: 'agent_completed', agent: 'Compliance', summary: 'FLAG — visa denied', ts_ms: 400 });
    const row = done.rows.find((r) => r.name === 'Compliance')!;
    expect(row.flagged).toBe(true);
    expect(done.flags).toBe(1);
  });

  it('unknown agent → state unchanged', () => {
    const s = initProgress();
    const next = reduceProgress(s, { type: 'agent_completed', agent: 'Ghost', summary: 'FLAG' });
    expect(next).toBe(s);
  });
});

// ─── agent_skipped ─────────────────────────────────────────────────────────
describe('reduceProgress — agent_skipped', () => {
  it('marks row skipped with verdict', () => {
    const s = initProgress();
    const next = reduceProgress(s, { type: 'agent_skipped', agent: 'Transport', summary: 'single-city trip' });
    const row = next.rows.find((r) => r.name === 'Transport')!;
    expect(row.status).toBe('skipped');
    expect(row.verdict).toBe('single-city trip');
    expect(row.elapsedMs).toBeNull();
  });
});

// ─── gate_blocked ──────────────────────────────────────────────────────────
describe('reduceProgress — gate_blocked', () => {
  it('sets verdict to BLOCK — <summary> and flagged:true', () => {
    const s = initProgress();
    const next = reduceProgress(s, { type: 'gate_blocked', agent: 'Compliance', summary: 'visa denied' });
    const row = next.rows.find((r) => r.name === 'Compliance')!;
    expect(row.status).toBe('done');
    expect(row.verdict).toBe('BLOCK — visa denied');
    expect(row.flagged).toBe(true);
  });
});

// ─── budget_veto ───────────────────────────────────────────────────────────
describe('reduceProgress — budget_veto', () => {
  it('patches Budget row with VETO verdict and flagged', () => {
    const s = initProgress();
    const next = reduceProgress(s, { type: 'budget_veto' });
    const row = next.rows.find((r) => r.name === 'Budget')!;
    expect(row.status).toBe('running');
    expect(row.verdict).toBe('VETO — over budget');
    expect(row.flagged).toBe(true);
  });
});

// ─── critic_rejected ───────────────────────────────────────────────────────
describe('reduceProgress — critic_rejected', () => {
  it('sets Critic verdict to REJECTED — <summary> and flagged', () => {
    const s = initProgress();
    const next = reduceProgress(s, { type: 'critic_rejected', summary: 'low score' });
    const row = next.rows.find((r) => r.name === 'Critic')!;
    expect(row.status).toBe('done');
    expect(row.verdict).toBe('REJECTED — low score');
    expect(row.flagged).toBe(true);
  });
});

// ─── negotiate_finished ────────────────────────────────────────────────────
describe('reduceProgress — negotiate_finished', () => {
  it('finished:true, phase "Plan ready.", pending/running rows → skipped', () => {
    const s = initProgress();
    // Start one agent, leave others pending
    const mid = reduceProgress(s, { type: 'agent_started', agent: 'Risk', ts_ms: 100 });
    const done = reduceProgress(mid, { type: 'negotiate_finished' });
    expect(done.finished).toBe(true);
    expect(done.phase).toBe('Plan ready.');
    for (const r of done.rows) {
      expect(r.status === 'done' || r.status === 'skipped').toBe(true);
    }
  });

  it('already-done rows stay done', () => {
    const s = initProgress();
    const withDone = reduceProgress(
      reduceProgress(s, { type: 'agent_started', agent: 'Risk', ts_ms: 1 }),
      { type: 'agent_completed', agent: 'Risk', summary: 'CLEAR', ts_ms: 2 },
    );
    const fin = reduceProgress(withDone, { type: 'negotiate_finished' });
    expect(fin.rows.find((r) => r.name === 'Risk')!.status).toBe('done');
  });
});

// ─── forward-compat: unknown event type and no-op types ───────────────────
describe('reduceProgress — forward-compat', () => {
  it('unknown event type → same state reference (no change)', () => {
    const s = initProgress();
    const next = reduceProgress(s, { type: 'some_future_event' });
    expect(next).toBe(s);
  });

  it('wallet event → same state reference', () => {
    const s = initProgress();
    const next = reduceProgress(s, { type: 'wallet', data: { op: 'seed' } });
    expect(next).toBe(s);
  });

  it('booked event → same state reference', () => {
    const s = initProgress();
    const next = reduceProgress(s, { type: 'booked' });
    expect(next).toBe(s);
  });
});

// ─── immutability ──────────────────────────────────────────────────────────
describe('reduceProgress — immutability', () => {
  it('does NOT mutate the input state or its rows on agent_started', () => {
    const s = initProgress();
    const prevStatuses = s.rows.map((r) => r.status);
    reduceProgress(s, { type: 'agent_started', agent: 'Risk', ts_ms: 1 });
    expect(s.rows.map((r) => r.status)).toEqual(prevStatuses);   // input unchanged
  });

  it('returns a NEW object on a recognized change', () => {
    const s = initProgress();
    const next = reduceProgress(s, { type: 'agent_started', agent: 'Risk', ts_ms: 1 });
    expect(next).not.toBe(s);
  });
});

// ─── rows assign correct layers ────────────────────────────────────────────
describe('AGENT_ROSTER layers', () => {
  it('gate agents have layer gate', () => {
    const gateAgents = ['Risk', 'Health', 'Compliance', 'Fraud', 'Transport', 'Critic'];
    const roster = AGENT_ROSTER.filter((a) => gateAgents.includes(a.name));
    expect(roster.every((a) => a.layer === 'gate')).toBe(true);
  });
  it('money agents have layer money', () => {
    const moneyAgents = ['Budget', 'Insurance'];
    const roster = AGENT_ROSTER.filter((a) => moneyAgents.includes(a.name));
    expect(roster.every((a) => a.layer === 'money')).toBe(true);
  });
  it('plan agents have layer plan', () => {
    const planAgents = ['Destination', 'Planner', 'Accommodation', 'DayPlanner'];
    const roster = AGENT_ROSTER.filter((a) => planAgents.includes(a.name));
    expect(roster.every((a) => a.layer === 'plan')).toBe(true);
  });
});
