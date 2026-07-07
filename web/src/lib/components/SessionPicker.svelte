<script lang="ts">
  import { createEventDispatcher } from 'svelte';
  import { centsToUsd } from '../api';
  import type { DemoUser } from '../api';

  export let users: DemoUser[] = [];
  export let error: string | null = null;

  const dispatch = createEventDispatcher<{ select: DemoUser; guest: void }>();

  const PERSONA_ICON: Record<string, string> = {
    foodie: '🍜', hiker: '🥾', adventurer: '🥾', culture: '🏛️', family: '👨‍👩‍👧', budget: '🎒',
  };
  const icon = (p: string) => PERSONA_ICON[(p || '').toLowerCase()] ?? '🧳';
</script>

<div class="picker" data-testid="session-picker">
  <h1>Log in to Travel Guild</h1>
  <p class="lead">Pick a demo account to log in — your saved preferences, passport and display currency apply automatically. No password.</p>

  {#if error}
    <div class="err" data-testid="session-error">Couldn't load accounts ({error}). <button on:click={() => dispatch('guest')}>Continue as guest →</button></div>
  {/if}

  <div class="cards">
    {#each users as u (u.user_id)}
      <button class="card" data-testid="user-{u.user_id}" on:click={() => dispatch('select', u)}>
        <div class="ic">{icon(u.persona)}</div>
        <div class="nm">{u.display_name}</div>
        <div class="meta">{u.persona} · {u.nationality} · {u.home_currency}</div>
        <div class="wallet">wallet {centsToUsd(u.wallet_balance_cents)}</div>
        <span class="login-cta">Log in →</span>
      </button>
    {/each}
  </div>

  <button class="guest" data-testid="continue-guest" on:click={() => dispatch('guest')}>Browse as guest (no account) →</button>
</div>

<style>
  .picker { max-width: 760px; margin: 8vh auto; padding: 0 18px; text-align: center; }
  h1 { font-size: 24px; margin: 0 0 6px; }
  .lead { color: #7c7468; font-size: 14px; margin: 0 0 22px; }
  .err { background: #f8e4de; color: #c0563f; border-radius: 9px; padding: 9px 12px; margin-bottom: 16px; font-size: 13px; }
  .err button { background: none; border: 0; color: #c0563f; font-weight: 700; cursor: pointer; }
  .cards { display: grid; grid-template-columns: repeat(auto-fit, minmax(160px, 1fr)); gap: 12px; }
  .card { background: #fff; border: 1px solid #ece2d5; border-radius: 14px; padding: 16px 12px; cursor: pointer;
    text-align: center; transition: border-color .12s, transform .12s; }
  .card:hover { border-color: #d9774a; transform: translateY(-2px); }
  .ic { font-size: 30px; }
  .nm { font-weight: 700; margin-top: 6px; }
  .meta { color: #7c7468; font-size: 12.5px; margin-top: 3px; text-transform: capitalize; }
  .wallet { color: #4f8a63; font-size: 12px; margin-top: 5px; font-variant-numeric: tabular-nums; }
  .login-cta { display: block; margin-top: 8px; font-size: 12px; font-weight: 700; color: #d9774a;
    opacity: 0; transition: opacity .15s; }
  .card:hover .login-cta, .card:focus .login-cta { opacity: 1; }
  .guest { margin-top: 22px; background: none; border: 1px solid #ece2d5; border-radius: 9px;
    padding: 9px 16px; color: #2d2a26; font-weight: 600; font-size: 13px; cursor: pointer; }
</style>
