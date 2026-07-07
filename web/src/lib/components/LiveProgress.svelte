<script lang="ts">
  import type { ProgressState } from '../progress';
  export let progress: ProgressState;
  const glyph = (s: string) => (s === 'done' ? '●' : s === 'skipped' ? '○' : s === 'running' ? '' : '·');
</script>

<section class="live" data-testid="live-progress">
  <header class="lp-head">
    <span class="pulse" aria-hidden="true"></span>
    <h3>Your Travel Guild agents are planning</h3>
    <span class="round">{progress.round ? `Round ${progress.round}` : ''}</span>
  </header>
  <p class="phase" data-testid="live-phase">{progress.phase}</p>

  <div class="rows">
    {#each progress.rows as r (r.name)}
      <div class="row {r.status}" class:flagged={r.flagged} data-testid="agent-row-{r.name}">
        <span class="name">{r.name}</span>
        <span class="layer layer-{r.layer}">{r.layer}</span>
        <span class="status">
          {#if r.status === 'running'}<span class="spin" aria-label="running"></span>
          {:else}<span class="gly {r.status}">{glyph(r.status)}</span>{/if}
          {r.flagged ? '⚑' : ''}
        </span>
        <span class="verdict">{r.verdict || '—'}</span>
        <span class="ms">{r.elapsedMs != null ? Math.round(r.elapsedMs) + ' ms' : ''}</span>
      </div>
    {/each}
  </div>

  <footer class="lp-foot">
    {progress.flags} flag{progress.flags === 1 ? '' : 's'} ·
    <span class="honest">Live progress mirrors the deterministic planner — your final plan is the source of truth.</span>
  </footer>
</section>

<style>
  .live { padding: 6px 4px; }
  .lp-head { display:flex; align-items:center; gap:10px; }
  .lp-head h3 { font-size:15px; margin:0; color:#2d2a26; }
  .round { margin-left:auto; font-size:12px; color:#998a78; }
  .pulse { width:9px; height:9px; border-radius:50%; background:#d9774a; animation:pulse 1.2s infinite; }
  @keyframes pulse { 0%,100%{opacity:1} 50%{opacity:.3} }
  .phase { font-size:13px; color:#7c7468; margin:6px 0 12px; }
  .rows { display:flex; flex-direction:column; }
  .row { display:grid; grid-template-columns:130px 58px 70px 1fr 64px; gap:8px; align-items:center;
    padding:7px 8px; border-bottom:1px solid #f0e8dc; font-size:13px; transition:background .3s, opacity .3s; }
  .row.running { background:#fff7ef; }
  .row.skipped { opacity:.5; }
  .row.flagged { border-left:3px solid #c0563f; background:#fdf1ee; }
  .name { font-weight:600; color:#2d2a26; }
  .layer { font-size:10.5px; text-transform:uppercase; letter-spacing:.04em; border-radius:9px;
    padding:2px 7px; justify-self:start; }
  .layer-plan { background:#e7eefc; color:#3a6fd0; }
  .layer-gate { background:#fcefe0; color:#c07a2a; }
  .layer-money { background:#e6f3ea; color:#3f8a5d; }
  .status { display:flex; align-items:center; gap:4px; }
  .gly.done { color:#3f8a5d; } .gly.skipped { color:#998a78; } .gly.pending { color:#c9bdad; }
  .spin { width:11px; height:11px; border:2px solid #f0d8c4; border-top-color:#d9774a; border-radius:50%;
    animation:spin .7s linear infinite; display:inline-block; }
  @keyframes spin { to { transform:rotate(360deg); } }
  .verdict { color:#7c7468; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
  .ms { text-align:right; color:#998a78; font-variant-numeric:tabular-nums; }
  .lp-foot { margin-top:12px; font-size:11.5px; color:#998a78; }
  .honest { font-style:italic; }
</style>
