<script lang="ts">
  /**
   * Aftercare.svelte — #100 AFTERCARE: proactive risk-monitoring panel.
   *
   * CHANNEL-SECURITY BOUNDARY (non-negotiable):
   *   - This panel is SUGGEST-ONLY. It NEVER calls confirmPlan or cancel.
   *   - switch_wet_weather_variant calls /replan (plan-only, no money) via
   *     applyVariantSwitch, then regenerates the ICS client-side.
   *   - reconsider_leg and resuggest_area_lodging open the WEBPAGE route only
   *     (no transaction from the panel).
   *   - monitor tier shows awareness text only (no button).
   *   - Telegram status is surfaced for transparency but NEVER contains a token.
   *
   * Pure presentation: all monetary state lives on the server.
   * No token/secret in the bundle.
   */
  import type { NegotiateResult } from '../api';
  import type { AftercareResult, AftercareAlert } from '../aftercare';
  import { aftercareCheck, applyVariantSwitch, buildUpdatedIcs } from '../aftercare';
  import { downloadIcs } from '../ics';

  /** Allow only same-origin relative paths and https:// URLs (scheme match is
   *  case-insensitive per RFC 3986 §3.1, so Https://, HTTPS://, etc. are also
   *  allowed) — blocks javascript:, data:, http:, and protocol-relative //host
   *  (external nav) in ANY casing. Mirrors api.safeHref(). */
  function safePath(raw: string | null | undefined): string {
    if (!raw) return '';
    const s = raw.trim();
    if (s.startsWith('//')) return '';                 // protocol-relative -> external, reject
    if (s.startsWith('/') || s.toLowerCase().startsWith('https://')) return s;
    return '';
  }

  export let result: NegotiateResult;
  export let user_id: string | undefined = undefined;

  // Internal state
  let checking = false;
  let aftercareResult: AftercareResult | null = null;
  let error: string | null = null;

  // ICS update tracking (per-alert)
  let icsUpdatedAlerts = new Set<string>();
  let icsUpdateLoading = new Set<string>();

  // Derived
  $: idk = result?.idempotency_key;
  $: isBooked = result?.payment_status === 'charged' || !!result?.booking_ref;
  $: alerts = aftercareResult?.alerts ?? [];
  $: monitoring = aftercareResult?.monitoring;
  $: betaNote = aftercareResult?.beta_note;
  $: tgStatus = aftercareResult?.telegram;

  async function check() {
    if (!idk) return;
    checking = true;
    error = null;
    try {
      aftercareResult = await aftercareCheck(idk, user_id);
    } catch (e: unknown) {
      error = e instanceof Error ? e.message : String(e);
    } finally {
      checking = false;
    }
  }

  async function doVariantSwitch(alert: AftercareAlert) {
    const at = alert.action_target;
    if (!at?.replan_op || !idk) return;
    const key = alert.leg_id ?? alert.city ?? 'switch';
    icsUpdateLoading = new Set([...icsUpdateLoading, key]);
    try {
      const replanResult = await applyVariantSwitch(idk, at.replan_op);
      if (replanResult.outcome === 'plan_ready' && replanResult.plan) {
        // Rebuild ICS from the updated plan and trigger download
        const updatedResult: NegotiateResult = {
          ...result,
          ...replanResult.plan,
        };
        downloadIcs(updatedResult);
        icsUpdatedAlerts = new Set([...icsUpdatedAlerts, key]);
      } else {
        // Never silently swallow a locked/failed switch — surface it honestly.
        error = `Couldn't apply the wet-weather switch (${replanResult.outcome ?? 'unavailable'}). You can manage this change on the trip webpage.`;
      }
    } catch (e: unknown) {
      error = e instanceof Error ? e.message : String(e);
    } finally {
      const next = new Set(icsUpdateLoading);
      next.delete(key);
      icsUpdateLoading = next;
    }
  }

  function alertKey(alert: AftercareAlert): string {
    return alert.leg_id ?? alert.city ?? alert.risk_type ?? 'alert';
  }

  function severityClass(tier: string): string {
    if (tier === 'high') return 'sev-high';
    if (tier === 'medium') return 'sev-med';
    return 'sev-mon';
  }

  function severityLabel(tier: string): string {
    if (tier === 'high') return 'HIGH';
    if (tier === 'medium') return 'MED';
    return 'MONITORING';
  }
</script>

<div class="aftercare" data-testid="aftercare-panel">
  {#if !isBooked}
    <p class="hint muted" data-testid="aftercare-not-booked">
      Risk monitoring is available after booking is confirmed.
    </p>
  {:else}
    <div class="header">
      <div class="status-row">
        {#if monitoring}
          <span class="status-dot {monitoring.status === 'ok' ? 'ok' : 'unavail'}"></span>
          <span class="status-label">
            {#if monitoring.status === 'ok'}
              Monitoring active
              {#if monitoring.as_of} · as of {monitoring.as_of}{/if}
            {:else}
              Feed unavailable — could not check
            {/if}
          </span>
          {#if monitoring.source}
            <span class="source-tag">{monitoring.source}</span>
          {/if}
        {:else}
          <span class="hint muted">Not checked yet.</span>
        {/if}
      </div>
      <button
        class="check-btn"
        on:click={check}
        disabled={checking}
        data-testid="aftercare-check-btn"
      >
        {checking ? 'Checking...' : 'Check for new risks'}
      </button>
    </div>

    {#if error}
      <div class="err" data-testid="aftercare-error">{error}</div>
    {/if}

    {#if monitoring?.status === 'unavailable'}
      <div class="unavail-banner" data-testid="aftercare-unavailable">
        Feed unavailable — could not check live risk status. Always check local advisories.
      </div>
    {/if}

    {#if betaNote}
      <div class="beta-banner" data-testid="aftercare-beta-note">
        {betaNote}
      </div>
    {/if}

    {#if alerts.length === 0 && aftercareResult?.outcome === 'ok' && monitoring?.status === 'ok'}
      <div class="all-clear" data-testid="aftercare-all-clear">
        No active alerts for your booked trip at this time.
      </div>
    {/if}

    {#each alerts as alert (alertKey(alert))}
      {@const key = alertKey(alert)}
      {@const isLoading = icsUpdateLoading.has(key)}
      {@const icsUpdated = icsUpdatedAlerts.has(key)}
      <div class="alert-card {severityClass(alert.severity_tier)}" data-testid="aftercare-alert">

        <div class="alert-head">
          <span class="sev-chip {severityClass(alert.severity_tier)}" data-testid="severity-chip">
            {severityLabel(alert.severity_tier)}
          </span>
          <span class="alert-city">{alert.city ?? alert.iso2 ?? 'Unknown location'}</span>
          {#if alert.date_window?.checkin}
            <span class="date-range">
              {alert.date_window.checkin}{#if alert.date_window?.checkout} – {alert.date_window.checkout}{/if}
            </span>
          {/if}
        </div>

        <div class="alert-summary" data-testid="alert-summary">
          {alert.summary_localized || alert.summary}
          {#if alert.translated && alert.lang && alert.lang !== 'en'}
            <span class="auto-translated-tag" data-testid="auto-translated-tag">auto-translated</span>
          {:else if alert.lang && alert.lang !== 'en'}
            <span class="translation-failed-tag">(translation unavailable — showing English)</span>
          {/if}
        </div>

        {#if alert.translation_note && alert.translated && alert.lang !== 'en'}
          <div class="translation-note">{alert.translation_note}</div>
        {/if}

        {#if alert.advice}
          <div class="alert-advice">{alert.advice}</div>
        {/if}

        <div class="alert-meta">
          {#if alert.as_of}
            <span class="meta-item">As of: {alert.as_of}</span>
          {/if}
          {#if alert.source}
            <span class="meta-item source">{alert.source}</span>
          {/if}
          {#if alert.beta}
            <span class="meta-item beta">beta coverage</span>
          {/if}
        </div>

        <!-- Action buttons — SUGGEST ONLY, all route through the webpage -->
        {#if alert.suggested_action === 'switch_wet_weather_variant' && alert.action_target?.replan_op}
          <div class="actions">
            {#if icsUpdated}
              <div class="ics-updated" data-testid="ics-updated">
                Calendar updated — .ics re-downloaded
              </div>
            {:else}
              <button
                class="action-btn primary"
                on:click={() => doVariantSwitch(alert)}
                disabled={isLoading}
                data-testid="switch-wet-weather-btn"
              >
                {isLoading ? 'Switching...' : 'Switch to wet-weather plan'}
              </button>
              <p class="action-note">
                Switches the affected day to indoor alternatives. The change will be
                saved to your plan and your calendar will update.
              </p>
            {/if}
          </div>

        {:else if alert.suggested_action === 'reconsider_leg'}
          {@const safeHref = safePath(alert.action_target?.webpage_path)}
          <div class="actions">
            {#if safeHref}
              <a
                class="action-btn review"
                href={safeHref}
                target="_blank"
                rel="noopener noreferrer"
                data-testid="reconsider-leg-btn"
              >
                Review this leg on the webpage
              </a>
            {:else}
              <span class="action-note">Review this leg carefully before travel.</span>
            {/if}
            <p class="action-note">All changes require confirmation on the Travel Guild webpage.</p>
          </div>

        {:else if alert.suggested_action === 'resuggest_area_lodging'}
          {@const safeHref = safePath(alert.action_target?.webpage_path)}
          <div class="actions">
            {#if safeHref}
              <a
                class="action-btn review"
                href={safeHref}
                target="_blank"
                rel="noopener noreferrer"
                data-testid="rebook-lodging-btn"
              >
                Rebook on webpage
              </a>
            {:else}
              <span class="action-note">Rebook lodging on the Travel Guild webpage.</span>
            {/if}
            <p class="action-note">All rebooking happens on the secure webpage consent flow.</p>
          </div>

        {:else if alert.suggested_action === 'monitor'}
          <div class="actions monitor-only" data-testid="monitor-only">
            Monitor official advisories. No action required at this time.
          </div>
        {/if}

      </div>
    {/each}

    <!-- Telegram status (transparency, no secrets) -->
    {#if tgStatus}
      <div class="tg-status" data-testid="telegram-status">
        {#if tgStatus.sent}
          Alert sent to your phone (suggestion only — all changes happen here).
        {:else if tgStatus.attempted}
          Phone alert attempted but not delivered: {tgStatus.note ?? ''}
        {:else}
          {tgStatus.note ?? 'Phone alerts not configured.'}
          <!-- No functional click-through: Aftercare is nested App → RightRail →
               Aftercare, and there is no existing event-bubbling path up to
               App.svelte's openPrefs() (RightRail only dispatches 'replanned').
               A plain instruction is the honest, minimal-scope choice here rather
               than inventing new prop-drilling/event infrastructure for this. -->
          <span class="tg-cta" data-testid="telegram-setup-cta">Set up Telegram alerts in your Preferences.</span>
        {/if}
      </div>
    {/if}
  {/if}
</div>

<style>
  .aftercare { display: flex; flex-direction: column; gap: 10px; padding: 13px; }
  .header { display: flex; flex-direction: column; gap: 8px; }
  .status-row { display: flex; align-items: center; gap: 7px; flex-wrap: wrap; }
  .status-dot { width: 8px; height: 8px; border-radius: 50%; flex-shrink: 0; }
  .status-dot.ok { background: #4f8a63; }
  .status-dot.unavail { background: #aaa; }
  .status-label { font-size: 13px; color: #2d2a26; }
  .source-tag { font-size: 11px; color: #998a78; border: 1px solid #ece2d5;
    border-radius: 5px; padding: 1px 5px; }
  .check-btn { padding: 7px 14px; border-radius: 9px; border: 1px solid #d4c4b0;
    background: #fff; font-weight: 600; font-size: 13px; cursor: pointer; color: #4a4036; }
  .check-btn:hover:not(:disabled) { background: #f5ede4; }
  .check-btn:disabled { opacity: 0.55; cursor: default; }
  .hint.muted { font-size: 13px; color: #998a78; }
  .err { color: #c0563f; font-size: 13px; background: #f8e4de;
    border-radius: 8px; padding: 8px 11px; }
  .unavail-banner, .beta-banner { font-size: 12.5px; background: #fdf6e9;
    border: 1px solid #e8d6a0; border-radius: 8px; padding: 8px 11px; color: #7a6a30; }
  .all-clear { font-size: 13px; color: #4f8a63; background: #e9f5ed;
    border-radius: 8px; padding: 9px 12px; }
  .alert-card { border-radius: 10px; padding: 12px; display: flex; flex-direction: column; gap: 7px; }
  .alert-card.sev-high { background: #fef2f0; border: 1px solid #f0c8c0; }
  .alert-card.sev-med { background: #fffbf0; border: 1px solid #f0e0a8; }
  .alert-card.sev-mon { background: #f0f4ff; border: 1px solid #c0ccee; }
  .alert-head { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
  .sev-chip { font-size: 11px; font-weight: 700; border-radius: 6px; padding: 2px 8px; }
  .sev-chip.sev-high { background: #f8e4de; color: #c0563f; }
  .sev-chip.sev-med { background: #fbf0db; color: #c98a2b; }
  .sev-chip.sev-mon { background: #e0e8fb; color: #4060c8; }
  .alert-city { font-weight: 600; font-size: 14px; }
  .date-range { font-size: 12px; color: #998a78; }
  .alert-summary { font-size: 13.5px; color: #2d2a26; line-height: 1.5; }
  .auto-translated-tag { font-size: 11px; background: #e6f1fb; color: #3070b8;
    border-radius: 5px; padding: 1px 5px; margin-left: 5px; }
  .translation-failed-tag { font-size: 11px; color: #998a78; margin-left: 4px; }
  .translation-note { font-size: 11px; color: #998a78; font-style: italic; }
  .alert-advice { font-size: 13px; color: #7a3030; background: rgba(192,86,63,0.07);
    border-radius: 7px; padding: 6px 9px; }
  .alert-meta { display: flex; gap: 10px; flex-wrap: wrap; }
  .meta-item { font-size: 11px; color: #998a78; }
  .meta-item.source { border: 1px solid #ece2d5; border-radius: 5px; padding: 1px 5px; }
  .meta-item.beta { color: #c98a2b; }
  .actions { display: flex; flex-direction: column; gap: 6px; }
  .action-btn { display: inline-block; padding: 8px 14px; border-radius: 9px;
    font-weight: 600; font-size: 13px; cursor: pointer; text-decoration: none;
    border: 1px solid #d4c4b0; background: #fff; color: #4a4036; text-align: center; }
  .action-btn:hover:not(:disabled) { background: #f5ede4; }
  .action-btn:disabled { opacity: 0.55; cursor: default; }
  .action-btn.primary { background: #4f8a63; color: #fff; border-color: #3d7050; }
  .action-btn.primary:hover:not(:disabled) { background: #3d7050; }
  .action-btn.review { border-color: #d4c4b0; }
  .action-note { font-size: 11.5px; color: #998a78; margin: 0; }
  .monitor-only { font-size: 13px; color: #4060c8; background: #eef1fb;
    border-radius: 7px; padding: 7px 10px; }
  .ics-updated { font-size: 13px; color: #4f8a63; background: #e9f5ed;
    border-radius: 7px; padding: 7px 10px; }
  .tg-status { font-size: 12px; color: #998a78; border-top: 1px solid #f0e8db;
    padding-top: 8px; margin-top: 4px; }
  .tg-cta { display: block; margin-top: 4px; color: #4a4036; font-weight: 600; }
</style>
