// @vitest-environment jsdom
//
// <LiveProgress> RENDER-CONTRACT test (component-mounted, jsdom).
//
// The pure reducer (initProgress/reduceProgress) is already exhaustively covered by
// progress.test.ts. THIS file owns the missing half: the frame-sequence → rendered-row
// mapping inside LiveProgress.svelte. We drive the component the way the app does — by
// folding a realistic SSE StreamEvent[] through the REAL reduceProgress and handing the
// resulting ProgressState to the component as a prop — then assert the DOM:
//   • one row per roster agent, in roster order, with the right status class;
//   • a running row shows the spinner (aria-label="running"), not a glyph;
//   • done/skipped/pending rows show the right glyph and status class;
//   • a flagged row gets the .flagged class + the ⚑ mark, and the footer flag count;
//   • verdict text (or the — placeholder) and the elapsed "<n> ms" render;
//   • phase line + "Round N" header reflect state;
//   • negotiate_finished settles every un-reached row → skipped (no perpetual spinner).
//
// Non-overlap: progress.test.ts asserts STATE shape; this asserts the rendered VIEW of it.

import { describe, it, expect } from 'vitest';
import { render } from '@testing-library/svelte';
import { initProgress, reduceProgress, AGENT_ROSTER } from './progress';
import type { ProgressState } from './progress';
import type { StreamEvent } from './stream';
import LiveProgress from './components/LiveProgress.svelte';

// Fold a frame sequence through the real reducer — exactly what stream.ts → UI does.
const fold = (frames: StreamEvent[]): ProgressState =>
  frames.reduce((s, ev) => reduceProgress(s, ev), initProgress());

const mount = (progress: ProgressState) => render(LiveProgress, { props: { progress } });
const rowEl = (c: ReturnType<typeof mount>, name: string): HTMLElement =>
  c.getByTestId(`agent-row-${name}`) as HTMLElement;

// ── roster → rows ────────────────────────────────────────────────────────────
describe('<LiveProgress> — roster rendering', () => {
  it('renders exactly one row per roster agent, in roster order', () => {
    const c = mount(initProgress());
    const rows = c.getAllByTestId(/^agent-row-/);
    expect(rows).toHaveLength(AGENT_ROSTER.length);
    expect(rows.map((r) => r.getAttribute('data-testid')))
      .toEqual(AGENT_ROSTER.map((a) => `agent-row-${a.name}`));
  });

  it('every fresh row carries its status class and layer chip', () => {
    const c = mount(initProgress());
    for (const a of AGENT_ROSTER) {
      const row = rowEl(c, a.name);
      expect(row.classList.contains('pending')).toBe(true);
      const layer = row.querySelector('.layer')!;
      expect(layer.textContent?.trim()).toBe(a.layer);
      expect(layer.classList.contains(`layer-${a.layer}`)).toBe(true);
    }
  });

  it('initial phase line renders', () => {
    const c = mount(initProgress());
    expect(c.getByTestId('live-phase').textContent).toContain('Waking the society');
  });
});

// ── per-status render: spinner vs glyph ──────────────────────────────────────
describe('<LiveProgress> — status → glyph/spinner', () => {
  it('a running row shows the spinner (aria-label="running"), NOT a glyph', () => {
    const state = fold([
      { type: 'negotiate_started', ts_ms: 0 },
      { type: 'agent_started', agent: 'Risk', ts_ms: 100 },
    ]);
    const c = mount(state);
    const row = rowEl(c, 'Risk');
    expect(row.classList.contains('running')).toBe(true);
    expect(row.querySelector('.spin[aria-label="running"]')).toBeTruthy();
    expect(row.querySelector('.gly')).toBeNull(); // no static glyph while spinning
  });

  it('a done row shows the done glyph ● and elapsed "<n> ms"', () => {
    const state = fold([
      { type: 'negotiate_started', ts_ms: 0 },
      { type: 'agent_started', agent: 'Risk', ts_ms: 1000 },
      { type: 'agent_completed', agent: 'Risk', summary: 'CLEAR', ts_ms: 1300 },
    ]);
    const c = mount(state);
    const row = rowEl(c, 'Risk');
    expect(row.classList.contains('done')).toBe(true);
    expect(row.querySelector('.gly.done')?.textContent).toBe('●');
    expect(row.querySelector('.ms')?.textContent?.trim()).toBe('300 ms');
    expect(row.querySelector('.verdict')?.textContent?.trim()).toBe('CLEAR');
  });

  it('a skipped row shows the skipped glyph ○ and the skipped class', () => {
    const state = fold([
      { type: 'negotiate_started', ts_ms: 0 },
      { type: 'agent_skipped', agent: 'Transport', summary: 'single-city trip' },
    ]);
    const c = mount(state);
    const row = rowEl(c, 'Transport');
    expect(row.classList.contains('skipped')).toBe(true);
    expect(row.querySelector('.gly.skipped')?.textContent).toBe('○');
    expect(row.querySelector('.verdict')?.textContent?.trim()).toBe('single-city trip');
  });

  it('a pending (never-touched) row shows the pending glyph · and an empty ms cell', () => {
    const c = mount(initProgress());
    const row = rowEl(c, 'Insurance');
    expect(row.querySelector('.gly.pending')?.textContent).toBe('·');
    expect(row.querySelector('.ms')?.textContent?.trim()).toBe('');
    // verdict placeholder when there is no verdict
    expect(row.querySelector('.verdict')?.textContent?.trim()).toBe('—');
  });
});

// ── flag rendering: class + ⚑ mark + footer count ────────────────────────────
describe('<LiveProgress> — flag rendering', () => {
  it('a flagged completion adds .flagged + the ⚑ mark and bumps the footer count', () => {
    const state = fold([
      { type: 'negotiate_started', ts_ms: 0 },
      { type: 'agent_started', agent: 'Compliance', ts_ms: 100 },
      { type: 'agent_completed', agent: 'Compliance', summary: 'FLAG — visa denied', ts_ms: 400 },
    ]);
    const c = mount(state);
    const row = rowEl(c, 'Compliance');
    expect(row.classList.contains('flagged')).toBe(true);
    expect(row.querySelector('.status')?.textContent).toContain('⚑');
    expect(row.querySelector('.verdict')?.textContent?.trim()).toBe('FLAG — visa denied');
    // footer: singular "1 flag"
    const foot = c.getByTestId('live-progress').querySelector('.lp-foot')!;
    expect(foot.textContent).toContain('1 flag');
    expect(foot.textContent).not.toContain('1 flags');
  });

  it('two flags render the plural "2 flags" in the footer', () => {
    const state = fold([
      { type: 'negotiate_started', ts_ms: 0 },
      { type: 'agent_started', agent: 'Compliance', ts_ms: 1 },
      { type: 'agent_completed', agent: 'Compliance', summary: 'FLAG — visa', ts_ms: 2 },
      { type: 'agent_started', agent: 'Risk', ts_ms: 3 },
      { type: 'agent_completed', agent: 'Risk', summary: 'BLOCK — conflict zone', ts_ms: 4 },
    ]);
    const c = mount(state);
    const foot = c.getByTestId('live-progress').querySelector('.lp-foot')!;
    expect(foot.textContent).toContain('2 flags');
  });

  it('zero flags renders the plural "0 flags"', () => {
    const c = mount(initProgress());
    const foot = c.getByTestId('live-progress').querySelector('.lp-foot')!;
    expect(foot.textContent).toContain('0 flags');
  });

  it('a non-flag CLEAR completion does NOT add .flagged or the ⚑ mark', () => {
    const state = fold([
      { type: 'negotiate_started', ts_ms: 0 },
      { type: 'agent_started', agent: 'Risk', ts_ms: 10 },
      { type: 'agent_completed', agent: 'Risk', summary: 'CLEAR', ts_ms: 20 },
    ]);
    const c = mount(state);
    const row = rowEl(c, 'Risk');
    expect(row.classList.contains('flagged')).toBe(false);
    expect(row.querySelector('.status')?.textContent).not.toContain('⚑');
  });
});

// ── header: phase line + round ───────────────────────────────────────────────
describe('<LiveProgress> — header phase + round', () => {
  it('reflects the live phase line driven by a phase frame', () => {
    const state = fold([
      { type: 'phase', summary: 'Parsing intent (LLM)…' },
    ]);
    const c = mount(state);
    expect(c.getByTestId('live-phase').textContent?.trim()).toBe('Parsing intent (LLM)…');
  });

  it('round header is blank before any round_started frame', () => {
    const c = mount(initProgress());
    expect(c.container.querySelector('.round')?.textContent?.trim()).toBe('');
  });

  it('renders "Round N" once a round_started frame arrives', () => {
    const state = fold([
      { type: 'negotiate_started', ts_ms: 0 },
      { type: 'round_started', round: 2 },
    ]);
    const c = mount(state);
    expect(c.container.querySelector('.round')?.textContent?.trim()).toBe('Round 2');
  });
});

// ── terminal: negotiate_finished settles the board ───────────────────────────
describe('<LiveProgress> — negotiate_finished render', () => {
  it('settles un-reached rows to skipped (no perpetual spinner) and keeps done rows done', () => {
    const state = fold([
      { type: 'negotiate_started', ts_ms: 0 },
      { type: 'agent_started', agent: 'Risk', ts_ms: 10 },
      { type: 'agent_completed', agent: 'Risk', summary: 'CLEAR', ts_ms: 20 },
      { type: 'agent_started', agent: 'Planner', ts_ms: 30 }, // left running when stream ends
      { type: 'negotiate_finished' },
    ]);
    const c = mount(state);
    // No spinner anywhere once finished.
    expect(c.getByTestId('live-progress').querySelector('.spin')).toBeNull();
    // The row left running is now skipped; the completed row stays done.
    expect(rowEl(c, 'Planner').classList.contains('skipped')).toBe(true);
    expect(rowEl(c, 'Risk').classList.contains('done')).toBe(true);
    expect(c.getByTestId('live-phase').textContent?.trim()).toBe('Plan ready.');
  });
});
