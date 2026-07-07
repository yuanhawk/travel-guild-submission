<script lang="ts">
  import { createEventDispatcher } from 'svelte';

  export let start: string = '';
  export let end: string = '';

  let localStart = start;
  let localEnd = end;

  const dispatch = createEventDispatcher<{ confirm: { start: string; end: string } }>();

  $: valid = !!localStart && !!localEnd && localStart <= localEnd;

  function confirm() {
    if (!valid) return;
    dispatch('confirm', { start: localStart, end: localEnd });
  }
</script>

<div class="date-picker-card" data-testid="date-picker-section">
  <div class="date-picker-label">📅 When are you going?</div>
  <div class="date-inputs-row">
    <div class="date-input-group">
      <label for="start-date-input">Start</label>
      <input
        id="start-date-input"
        type="date"
        data-testid="start-date"
        bind:value={localStart}
      />
    </div>
    <div class="date-sep" aria-hidden="true">→</div>
    <div class="date-input-group">
      <label for="end-date-input">End</label>
      <input
        id="end-date-input"
        type="date"
        data-testid="end-date"
        bind:value={localEnd}
      />
    </div>
    <button
      class="date-confirm-btn"
      data-testid="date-confirm"
      disabled={!valid}
      on:click={confirm}
    >
      Confirm →
    </button>
  </div>
</div>

<style>
  .date-picker-card {
    margin-top: 14px;
    padding: 14px 16px;
    background: #fdf6e9;
    border: 1px solid #e8d6a0;
    border-radius: 10px;
    animation: fadeIn 0.2s ease;
  }

  @keyframes fadeIn {
    from { opacity: 0; transform: translateY(-6px); }
    to   { opacity: 1; transform: none; }
  }

  .date-picker-label {
    font-size: 11px;
    font-weight: 700;
    color: #7a6a30;
    text-transform: uppercase;
    letter-spacing: 0.6px;
    margin-bottom: 10px;
  }

  .date-inputs-row {
    display: flex;
    align-items: flex-end;
    gap: 10px;
    flex-wrap: wrap;
  }

  .date-input-group {
    display: flex;
    flex-direction: column;
    gap: 4px;
  }

  .date-input-group label {
    font-size: 10px;
    font-weight: 600;
    color: #7c7468;
    text-transform: uppercase;
    letter-spacing: 0.5px;
  }

  .date-input-group input[type="date"] {
    padding: 8px 10px;
    border-radius: 8px;
    border: 1px solid #e8d6a0;
    font-size: 13px;
    outline: none;
    background: white;
    color: #2d2a26;
    min-height: 36px;
    min-width: 130px;
    transition: border-color 0.15s;
  }

  .date-input-group input[type="date"]:focus {
    border-color: #7a6a30;
    box-shadow: 0 0 0 3px rgba(122, 106, 48, 0.15);
  }

  .date-sep {
    color: #7c7468;
    font-size: 16px;
    padding-bottom: 6px;
    flex-shrink: 0;
  }

  .date-confirm-btn {
    padding: 9px 18px;
    border-radius: 8px;
    background: #d9774a;
    color: white;
    border: none;
    font-size: 13px;
    font-weight: 700;
    cursor: pointer;
    min-height: 36px;
    font-family: inherit;
    transition: background 0.15s;
    flex-shrink: 0;
  }

  .date-confirm-btn:hover:not(:disabled) {
    background: #c4693e;
  }

  .date-confirm-btn:disabled {
    opacity: 0.5;
    cursor: default;
  }

  /* Mobile: stack vertically below ~480px */
  @media (max-width: 480px) {
    .date-inputs-row {
      flex-direction: column;
      align-items: stretch;
    }
    .date-sep {
      display: none;
    }
    .date-input-group input[type="date"],
    .date-confirm-btn {
      width: 100%;
      box-sizing: border-box;
    }
  }
</style>
