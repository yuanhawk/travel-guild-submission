<script lang="ts">
  import { createEventDispatcher } from 'svelte';
  import { putPreferences, getPreferences, getTelegramLinkToken } from '../api';
  import type { Preferences, TelegramLinkResponse } from '../api';
  import { COUNTRIES } from '../countries';
  import { loadSession } from '../session';

  export let user: Preferences;

  const dispatch = createEventDispatcher<{ saved: Preferences; close: void }>();

  // Editable copy (update-only — these fields exist on the seeded profile).
  let home_currency = user.home_currency ?? 'USD';
  let nationality = user.nationality ?? '';
  let persona = user.persona ?? '';
  const p = (user.prefs ?? {}) as Record<string, unknown>;
  let pace = (p.pace as string) ?? '';

  let saving = false;
  let err: string | null = null;

  // Telegram "Connect" flow — a genuinely new interaction pattern (external
  // redirect + manual refresh-confirm), kept as its own small sub-section
  // rather than force-fit into the select/chip pattern the fields above use.
  // account-level (not per-trip); no manual chat_id text-entry fallback (deep-link only).
  let telegramChatId = (p.telegram_chat_id as string) ?? '';
  let tgConnecting = false;
  let tgRefreshing = false;
  let tgDeepLinkOpened = false;
  let tgError: string | null = null;

  async function connectTelegram(): Promise<void> {
    tgConnecting = true; tgError = null; tgDeepLinkOpened = false;
    // SECURITY (user_id-trust gap fix): GET /telegram/link now requires proof
    // of session possession (session_token, minted at login) — without it the
    // server returns 401 rather than trusting the bare user_id.
    const sessionToken = loadSession()?.session_token;
    if (!sessionToken) {
      tgConnecting = false;
      tgError = 'Your session has expired — please log out and log in again to connect Telegram.';
      return;
    }
    try {
      const res: TelegramLinkResponse = await getTelegramLinkToken(user.user_id, sessionToken);
      if (!res.available) {
        tgError = res.reason ?? 'Telegram connect is not available on this deployment.';
        return;
      }
      if (res.deep_link) {
        window.open(res.deep_link, '_blank');
        tgDeepLinkOpened = true;
      }
    } catch (e) {
      tgError = String(e);
    } finally {
      tgConnecting = false;
    }
  }

  async function refreshTelegramStatus(): Promise<void> {
    tgRefreshing = true; tgError = null;
    try {
      const fresh = await getPreferences(user.user_id, loadSession()?.session_token);
      const freshChatId = (fresh.prefs?.telegram_chat_id as string) ?? '';
      if (freshChatId) {
        telegramChatId = freshChatId;
        tgDeepLinkOpened = false;
      } else {
        tgError = "Not connected yet — tap Start in the Telegram chat, then try again.";
      }
    } catch (e) {
      tgError = String(e);
    } finally {
      tgRefreshing = false;
    }
  }

  const CURRENCIES = ['USD', 'EUR', 'GBP', 'SGD', 'AUD', 'JPY', 'INR'];
  const PERSONAS = ['foodie', 'hiker', 'culture', 'family', 'budget'];
  const PACES = ['relaxed', 'moderate', 'active'];

  // Dietary: closed-set chip tags (consistent with every other field being a select) +
  // a small free-text "Other" overflow — never silently drop real user input (this app's
  // honest-degradation convention). The WIRE FORMAT stays a single comma-joined string
  // (day_planner_agent's dietary filter reads it that way) — only the input widget
  // changes here, not the save payload's shape.
  const DIET_TAGS = [
    { value: 'vegetarian', label: 'Vegetarian' },
    { value: 'vegan', label: 'Vegan' },
    { value: 'halal', label: 'Halal' },
    { value: 'kosher', label: 'Kosher' },
    { value: 'gluten-free', label: 'Gluten-free' },
    { value: 'pescatarian', label: 'Pescatarian' },
  ];
  function parseDietary(raw: string): { tags: Set<string>; other: string } {
    const known = new Set(DIET_TAGS.map((t) => t.value));
    const tags = new Set<string>();
    const rest: string[] = [];
    for (const tok of raw.split(',').map((s) => s.trim()).filter(Boolean)) {
      const lower = tok.toLowerCase();
      if (known.has(lower)) tags.add(lower); else rest.push(tok);
    }
    return { tags, other: rest.join(', ') };
  }
  const parsedDietary = parseDietary((p.dietary as string) ?? '');
  let dietTags = parsedDietary.tags;
  let dietOther = parsedDietary.other;
  function toggleDiet(value: string): void {
    if (dietTags.has(value)) dietTags.delete(value); else dietTags.add(value);
    dietTags = dietTags; // re-trigger Svelte reactivity on the Set
  }
  $: dietary = [
    ...DIET_TAGS.filter((t) => dietTags.has(t.value)).map((t) => t.value),
    ...dietOther.split(',').map((s) => s.trim()).filter(Boolean),
  ].join(', ');

  async function save(): Promise<void> {
    saving = true; err = null;
    // SECURITY (user_id-trust gap fix): PUT /preferences now requires proof of
    // session possession (session_token, minted at login) — without it the
    // server returns 401 rather than trusting the bare user_id.
    const sessionToken = loadSession()?.session_token;
    if (!sessionToken) {
      saving = false;
      err = 'Your session has expired — please log out and log in again to save preferences.';
      return;
    }
    try {
      const updated = await putPreferences({
        user_id: user.user_id, home_currency, nationality, persona,
        prefs: { ...p, dietary: dietary || undefined, pace: pace || undefined },
      }, sessionToken);
      dispatch('saved', updated);
    } catch (e) {
      err = String(e);
    } finally {
      saving = false;
    }
  }
</script>

<div class="prefs" data-testid="preferences">
  <div class="head">
    <h2>👤 Preferences — {user.display_name}</h2>
    <button class="x" on:click={() => dispatch('close')} aria-label="Close">←</button>
  </div>
  <p class="lead">These auto-apply to every new trip. You can still override per request in the trip text. Currency is for <b>display only</b> — bookings settle in the wallet currency (USD).</p>

  <label>💱 Display currency
    <select bind:value={home_currency} data-testid="pref-currency">
      {#each CURRENCIES as c}<option value={c}>{c}</option>{/each}
    </select>
  </label>

  <label>🛂 Nationality (passport, ISO-2)
    <select bind:value={nationality} data-testid="pref-nationality">
      {#each COUNTRIES as c}<option value={c.code}>{c.code} — {c.name}</option>{/each}
    </select>
  </label>

  <label>🎭 Persona
    <select bind:value={persona} data-testid="pref-persona">
      {#each PERSONAS as x}<option value={x}>{x}</option>{/each}
    </select>
  </label>

  <div class="field">
    <span class="flabel">🍽️ Dietary (optional)</span>
    <div class="chips" data-testid="pref-dietary">
      {#each DIET_TAGS as t}
        <button type="button" class="chip" class:active={dietTags.has(t.value)}
          aria-pressed={dietTags.has(t.value)} data-testid={`pref-dietary-${t.value}`}
          on:click={() => toggleDiet(t.value)}>{t.label}</button>
      {/each}
    </div>
    <input class="other" bind:value={dietOther} placeholder="Other (e.g. no shellfish)" data-testid="pref-dietary-other" />
  </div>

  <label>🚶 Pace (optional)
    <select bind:value={pace} data-testid="pref-pace">
      <option value="">—</option>
      {#each PACES as x}<option value={x}>{x}</option>{/each}
    </select>
  </label>

  <div class="field tg-connect">
    <span class="flabel">📱 Telegram alerts (optional)</span>
    {#if telegramChatId}
      <div class="tg-connected" data-testid="telegram-connected">
        <span>✅ Telegram connected</span>
        <button type="button" class="tg-btn" on:click={connectTelegram}
          disabled={tgConnecting} data-testid="telegram-reconnect-btn">
          {tgConnecting ? 'Opening…' : 'Reconnect'}
        </button>
      </div>
    {:else if tgDeepLinkOpened}
      <p class="tg-hint">Opening Telegram… tap Start there, then come back and click below.</p>
      <button type="button" class="tg-btn" on:click={refreshTelegramStatus}
        disabled={tgRefreshing} data-testid="telegram-refresh-btn">
        {tgRefreshing ? 'Checking…' : "I've connected — refresh"}
      </button>
    {:else}
      <p class="tg-hint">Get trip updates and safety alerts on Telegram.</p>
      <button type="button" class="tg-btn" on:click={connectTelegram}
        disabled={tgConnecting} data-testid="telegram-connect-btn">
        {tgConnecting ? 'Opening…' : 'Connect Telegram'}
      </button>
    {/if}
    {#if tgError}<div class="tg-err" data-testid="telegram-error">{tgError}</div>{/if}
  </div>

  {#if err}<div class="err">Couldn’t save ({err}).</div>{/if}

  <div class="actions">
    <button class="save" on:click={save} disabled={saving} data-testid="pref-save">{saving ? 'Saving…' : '✓ Save preferences'}</button>
    <button class="cancel" on:click={() => dispatch('close')}>Cancel</button>
  </div>
</div>

<style>
  .prefs { max-width: 520px; margin: 6vh auto; background: #fff; border: 1px solid #ece2d5; border-radius: 14px; padding: 20px 22px; }
  .head { display: flex; align-items: center; gap: 10px; }
  h2 { font-size: 19px; margin: 0; flex: 1; }
  .x { background: none; border: 1px solid #ece2d5; border-radius: 8px; padding: 4px 10px; cursor: pointer; font-size: 15px; }
  .lead { color: #7c7468; font-size: 13px; margin: 6px 0 16px; }
  label { display: block; font-size: 13px; color: #7c7468; margin-bottom: 12px; font-weight: 600; }
  select, input { display: block; width: 100%; margin-top: 4px; padding: 8px 10px; border: 1px solid #ece2d5;
    border-radius: 8px; font: inherit; color: #2d2a26; box-sizing: border-box; }
  .field { margin-bottom: 12px; }
  .flabel { display: block; font-size: 13px; color: #7c7468; margin-bottom: 4px; font-weight: 600; }
  .chips { display: flex; flex-wrap: wrap; gap: 6px; margin-top: 4px; }
  .chip { background: #fff; border: 1px solid #ece2d5; border-radius: 999px; padding: 6px 13px;
    font: inherit; font-size: 13px; color: #2d2a26; cursor: pointer; }
  .chip.active { background: #d9774a; border-color: #d9774a; color: #fff; font-weight: 600; }
  .other { margin-top: 8px; }
  .tg-connect { background: #faf6f0; border: 1px solid #ece2d5; border-radius: 10px; padding: 11px 13px; }
  .tg-hint { font-size: 12.5px; color: #7c7468; margin: 2px 0 8px; font-weight: 400; }
  .tg-connected { display: flex; align-items: center; justify-content: space-between; gap: 10px;
    font-size: 13px; color: #3d7050; font-weight: 600; }
  .tg-btn { background: #fff; border: 1px solid #d4c4b0; border-radius: 8px; padding: 7px 13px;
    font: inherit; font-size: 13px; font-weight: 600; color: #4a4036; cursor: pointer; }
  .tg-btn:hover:not(:disabled) { background: #f5ede4; }
  .tg-btn:disabled { opacity: .6; cursor: default; }
  .tg-err { color: #c0563f; font-size: 12.5px; margin-top: 8px; }
  .err { background: #f8e4de; color: #c0563f; border-radius: 8px; padding: 8px 11px; font-size: 13px; margin-bottom: 12px; }
  .actions { display: flex; gap: 10px; margin-top: 6px; }
  .save { background: #d9774a; color: #fff; border: 0; border-radius: 9px; padding: 9px 18px; font-weight: 700; cursor: pointer; }
  .save:disabled { opacity: .6; }
  .cancel { background: #fff; border: 1px solid #ece2d5; border-radius: 9px; padding: 9px 16px; cursor: pointer; }
  @media (max-width: 640px) {
    .prefs { margin: 0; padding: 16px 16px 24px; border-radius: 0; max-width: 100%; min-height: 100vh; box-sizing: border-box; }
    .actions { flex-direction: column; }
    .save, .cancel { width: 100%; }
  }
</style>
