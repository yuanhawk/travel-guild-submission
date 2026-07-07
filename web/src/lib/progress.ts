// Pure reducer: SSE StreamEvent[] → a renderable "society of agents" progress board.
// HONESTY: this only REFLECTS the real deterministic stream — it never fabricates a
// status. The final plan envelope (negotiate_finished.result) is the source of truth;
// this board is hidden the moment that envelope renders. Pure + immutable so it is
// unit-testable in node and drives Svelte reactivity via `progress = reduceProgress(...)`.
import type { StreamEvent } from './stream';

export type AgentLayer = 'plan' | 'gate' | 'money';
export type AgentStatus = 'pending' | 'running' | 'done' | 'skipped';

export interface AgentRow {
  name: string;
  layer: AgentLayer;
  status: AgentStatus;
  verdict: string;
  elapsedMs: number | null;   // computed from event ts_ms (deterministic, no wall-clock)
  flagged: boolean;
  startedMs: number | null;   // ts_ms of this row's agent_started
}

export interface ProgressState {
  phase: string;              // live phase line (LLM parse / negotiating / finalizing)
  rows: AgentRow[];
  round: number;
  flags: number;
  startedMs: number | null;   // negotiate_started ts_ms
  finished: boolean;          // negotiate_finished seen (board may unmount)
}

// Canonical roster — mirrors orchestrator.py's emit names.
export const AGENT_ROSTER: { name: string; layer: AgentLayer }[] = [
  { name: 'Destination',   layer: 'plan'  },
  { name: 'Risk',          layer: 'gate'  },
  { name: 'Health',        layer: 'gate'  },
  { name: 'Compliance',    layer: 'gate'  },
  { name: 'Fraud',         layer: 'gate'  },
  { name: 'Planner',       layer: 'plan'  },
  { name: 'Accommodation', layer: 'plan'  },
  { name: 'Budget',        layer: 'money' },
  { name: 'Transport',     layer: 'gate'  },
  { name: 'DayPlanner',    layer: 'plan'  },
  { name: 'Critic',        layer: 'gate'  },
  { name: 'Insurance',     layer: 'money' },
];

const FLAG_TRIGGERS = ['FLAG', 'BLOCK', 'MEDEVAC', 'REJECTED'];
export function hasFlag(text?: string): boolean {
  if (!text) return false;
  const u = text.toUpperCase();
  return FLAG_TRIGGERS.some((t) => u.includes(t));
}

export function initProgress(): ProgressState {
  return {
    phase: 'Waking the Travel Guild agents…',
    rows: AGENT_ROSTER.map((a) => ({
      name: a.name, layer: a.layer, status: 'pending',
      verdict: '', elapsedMs: null, flagged: false, startedMs: null,
    })),
    round: 0, flags: 0, startedMs: null, finished: false,
  };
}

// immutable single-row patch by agent name (unknown agent → state unchanged: forward-compat)
function patchRow(s: ProgressState, name: string, fn: (r: AgentRow) => AgentRow): ProgressState {
  let touched = false;
  const rows = s.rows.map((r) => (r.name === name ? (touched = true, fn(r)) : r));
  return touched ? { ...s, rows } : s;
}

/** Pure, immutable. Always returns a NEW state object on a recognized event so Svelte re-renders. */
export function reduceProgress(state: ProgressState, ev: StreamEvent): ProgressState {
  const ts = typeof ev.ts_ms === 'number' ? ev.ts_ms : null;
  const agent = ev.agent || '';
  const summary = ev.summary || '';

  switch (ev.type) {
    case 'phase':
      return { ...state, phase: agent === 'narrator_done' ? 'Finalizing your plan…' : (summary || 'Working…') };

    case 'negotiate_started':
      return { ...initProgress(), phase: 'Negotiating across agents…', startedMs: ts };

    case 'round_started':
      return typeof ev.round === 'number' ? { ...state, round: ev.round } : state;

    case 'agent_started':
      return patchRow(state, agent, (r) => ({
        ...r, status: 'running', verdict: summary || r.verdict, startedMs: ts, elapsedMs: null,
      }));

    case 'agent_completed': {
      const flagged = hasFlag(summary);
      const next = patchRow(state, agent, (r) => ({
        ...r, status: 'done', verdict: summary || r.verdict, flagged,
        elapsedMs: (r.startedMs != null && ts != null) ? Math.max(0, ts - r.startedMs) : r.elapsedMs,
      }));
      return next === state ? state : { ...next, flags: next.flags + (flagged ? 1 : 0) };
    }

    case 'agent_skipped':
      return patchRow(state, agent, (r) => ({ ...r, status: 'skipped', verdict: summary || r.verdict, elapsedMs: null }));

    case 'budget_veto':
      return patchRow(state, 'Budget', (r) => ({ ...r, status: 'running', verdict: 'VETO — over budget', flagged: true }));

    case 'gate_blocked':
      return patchRow(state, agent, (r) => ({ ...r, status: 'done', verdict: `BLOCK — ${summary}`, flagged: true }));

    case 'critic_rejected':
      return patchRow(state, 'Critic', (r) => ({ ...r, status: 'done', verdict: `REJECTED — ${summary}`, flagged: true }));

    case 'negotiate_finished':
      // settle any row still pending/running → skipped (no perpetual spinner), then mark finished.
      return {
        ...state, finished: true, phase: 'Plan ready.',
        rows: state.rows.map((r) =>
          (r.status === 'pending' || r.status === 'running')
            ? { ...r, status: 'skipped', verdict: r.verdict || 'not reached this run', elapsedMs: null }
            : r),
      };

    default:                       // wallet / booked / consent_requested / timeout / unknown → no board change
      return state;
  }
}
