<script lang="ts">
  import { createEventDispatcher } from 'svelte';
  import type { EmergenciesResponse } from '../api';
  import { summarizeWatch } from '../safety';
  import { prettyCategory } from '../itinerary';
  export let data: EmergenciesResponse | null = null;   // null = still loading
  export let open = false;
  const dispatch = createEventDispatcher();
  $: s = summarizeWatch(data);
  $: countries = (data && data.status === 'ok' ? data.countries : []) ?? [];
  $: active = countries.filter((c) => c.severity !== 'monitoring');
  $: monitoring = countries.filter((c) => c.severity === 'monitoring');
</script>

<button class="badge {s.tone}" data-testid="safety-badge"
  title="Global Safety Watch — worldwide active hazards (separate from your trip)"
  on:click={() => dispatch('toggle')}>
  🌐 Safety Watch: {data ? s.label : '…'}
</button>

{#if open}
  <div class="overlay" data-testid="safety-overlay" role="presentation"
    on:click|self={() => dispatch('toggle')}
    on:keydown|self={(e) => e.key === 'Escape' && dispatch('toggle')}>
    <div class="panel">
      <header><h3>🌐 Global Safety Watch</h3>
        <span class="asof">{s.asOf ? `as of ${s.asOf} · ${s.source}` : ''}</span>
        <button class="x" on:click={() => dispatch('toggle')}>✕</button></header>

      {#if !data}
        <p class="muted">Loading live feed…</p>
      {:else if s.tone === 'unavailable'}
        <p class="unavail">⚠ Live feed unavailable — could not confirm active emergencies. Not a safe-signal; check official sources.</p>
      {:else if s.tone === 'clear'}
        <p class="clear">✓ No active (Orange/Red) emergencies reported worldwide.</p>
      {:else}
        <p class="summary">
          {#if active.length}<span class="sev-high">{active.length} active declaration(s)</span>{/if}
          {#if monitoring.length}<span class="sev-mon"> · 🌀 {monitoring.length} storm(s) monitored</span>{/if}
        </p>
        {#each [...active, ...monitoring] as c}
          <div class="card {c.severity === 'monitoring' ? 'mon' : ''}">
            <!-- Adversarial review (2026-07-06): c.hazard
                 is a raw served snake_case key (e.g. "tropical_cyclone", from
                 emergency_feed.py's _GDACS_HAZARD), was rendered verbatim — same
                 humanizer gap RightRail.svelte already closed for a.type. -->
            <div class="t">{c.severity === 'monitoring' ? '🌀' : '⚠'} {c.iso2} — {prettyCategory(c.hazard)}
              · <span class="sev-{c.severity}">{c.severity}</span></div>
            {#if c.headline}<div class="h">{c.headline}</div>{/if}
            <div class="m">source: {c.source ?? '—'} · as of {c.as_of ?? '—'}</div>
          </div>
        {/each}
      {/if}
      <p class="note">This is the worldwide watchlist. Hazards affecting <em>your</em> trip appear on the plan itself.</p>
    </div>
  </div>
{/if}

<style>
  .badge { font-size:12.5px; border-radius:999px; padding:5px 11px; cursor:pointer; font-family:inherit;
    border:1px solid #ece2d5; background:#fff; color:#7c7468; }
  .badge.alert { border-color:#c0563f; color:#c0563f; background:#fdf1ee; }
  .badge.monitoring { border-color:#3a6fd0; color:#3a6fd0; background:#eef3fc; }
  .badge.unavailable { border-color:#caa75b; color:#a87d28; background:#fbf4e6; }
  .badge.clear { border-color:#bcdcc7; color:#3f8a5d; }
  @media (max-width: 460px) { .badge { font-size:11.5px; padding:4px 8px; } }
  .overlay { position:fixed; inset:0; background:rgba(20,16,12,.35); display:flex;
    align-items:flex-start; justify-content:center; padding-top:8vh; z-index:50; }
  .panel { background:#fff; border:1px solid #ece2d5; border-radius:14px; width:min(560px,92vw);
    max-height:78vh; overflow:auto; padding:16px 18px; }
  header { display:flex; align-items:center; gap:10px; }
  header h3 { margin:0; font-size:16px; } .asof { font-size:12px; color:#998a78; }
  .x { margin-left:auto; border:0; background:none; font-size:16px; cursor:pointer; color:#998a78; }
  .muted,.note { color:#998a78; font-size:12.5px; } .note { margin-top:12px; }
  .unavail { color:#a87d28; } .clear { color:#3f8a5d; }
  .summary { font-size:14px; margin:8px 0; }
  .card { border:1px solid #f0e1dd; border-left:3px solid #c0563f; border-radius:8px; padding:8px 10px; margin:8px 0; }
  .card.mon { border-left-color:#3a6fd0; }
  .card .t { font-weight:600; } .card .h { font-size:13px; color:#7c7468; margin:3px 0; } .card .m { font-size:11.5px; color:#998a78; }
  .sev-high { color:#c0563f; font-weight:700; } .sev-medium { color:#c07a2a; font-weight:700; } .sev-monitoring,.sev-mon { color:#3a6fd0; font-weight:700; }
</style>
