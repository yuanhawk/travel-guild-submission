<script lang="ts">
  import { createEventDispatcher, onMount, onDestroy } from 'svelte';

  /** When true the plan has rendered — switch to floating bot-emoji + flyout. */
  export let hasPlan = false;
  /** Collapsed state for EMBEDDED mode (no plan). Persists across hasPlan transitions. */
  export let collapsed = false;
  export let loading = false;
  export let messages: Array<{ role: 'user' | 'bot' | 'note'; text: string }> = [];
  /** Trip-summary/save-to-phone (Preview) owns the mobile bottom edge while open —
   *  hide the floating mobile trigger (+ its flyout) so they don't visually collide
   *  with Preview's sticky bottom bar. Desktop's .bot-trigger/.flyout never conflicts
   *  (Preview has no fixed-bottom element on desktop), so this only applies on mobile
   *  widths via CSS, not an unconditional hide. */
  export let hideMobileBubble = false;
  /** Landing-page-draft2: grammar-teaching input chips — ONLY the true empty
   *  pre-plan state passes this true. The needs_clarification/error state
   *  reuses this same ChatPane instance and leaves this at its default false
   *  (mockup: mockups/landing-page-draft2.html tech-note). */
  export let showGrammarChips = false;

  let text = '';
  let ta: HTMLTextAreaElement | undefined;
  let mobTa: HTMLTextAreaElement | undefined;
  const dispatch = createEventDispatcher<{ plan: string; toggle: boolean; prefill: string }>();

  // Grammar-teaching chips: show the exact typeable FORMAT (destination, duration,
  // budget, style) so a first-time user learns the input grammar — distinct from the
  // guide-panel's destination cards, which teach "where" via curated auto-send prompts.
  const GRAMMAR_CHIPS = [
    { lbl: 'multi-city + budget + style', text: '10 days Japan, $3,000, culture + food' },
    { lbl: 'group size + total budget', text: '7 days Bali, 2 people, $1,500 total' },
    { lbl: 'short trip + solo + focus', text: 'Weekend in Singapore, solo, $800, food-focused' },
  ];
  // Removal condition per mockup: once any exchange beyond the initial greeting has
  // happened (messages.length > 1) OR the composer is non-empty, chips are REMOVED
  // (not hidden-with-reserved-space) so the panel shrinks back to its bounded-content height.
  $: showChips = showGrammarChips && messages.length <= 1 && !text.trim();

  /** Chip click POPULATES the composer but does NOT send — a distinct interaction
   *  from the guide-panel's destination cards (which auto-send via send(d.prompt)). */
  function prefill(t: string): void {
    text = t;
    dispatch('prefill', t);
    // focus whichever textarea is actually the visible surface (mobile sheet vs
    // desktop panel share the same `text` binding, but only one is on-screen —
    // focusing the hidden one wouldn't raise the mobile keyboard).
    (mobTa && mobTa.offsetParent !== null ? mobTa : ta)?.focus();
  }

  // Mobile bubble sheet state (pre-plan mode on mobile). Bindable so a parent
  // can open the sheet from another on-screen trigger (e.g. App.svelte's
  // guide-assistant-header) in addition to the bubble's own click handler.
  export let mobOpen = false;
  let mobSheetBottom = 0;

  let _vvListener: (() => void) | null = null;
  onMount(() => {
    const vv = window.visualViewport;
    if (vv) {
      _vvListener = () => {
        // Shift sheet up by keyboard height when soft keyboard opens.
        mobSheetBottom = Math.max(0, window.innerHeight - vv.height - vv.offsetTop);
      };
      vv.addEventListener('resize', _vvListener);
      vv.addEventListener('scroll', _vvListener);
    }
  });
  onDestroy(() => {
    const vv = window.visualViewport;
    if (_vvListener && vv) {
      vv.removeEventListener('resize', _vvListener);
      vv.removeEventListener('scroll', _vvListener);
    }
  });

  // ── Flyout state (used only in floating/plan mode) ──────────────────────────
  let flyoutOpen = false;
  // Distinct from flyoutOpen: true only when the TRIGGER itself was tapped to open
  // it, not when auto-surface (below) pops it open for a message the user hasn't
  // acted on yet. Mobile hides the trigger while this is true (the user is actively
  // looking at the panel they asked for) but keeps it visible during an auto-surfaced
  // message they haven't dismissed -- see the mob-open-hide class on the trigger.
  let userOpenedFlyout = false;
  let pinned = false;
  let hideTimer: ReturnType<typeof setTimeout> | null = null;
  // Desktop-narrow-mouse-only guard, consumed at most once per activation (see
  // onTriggerMouseLeave below for the full mechanism this exists to break).
  // Deliberately does NOT gate scheduleClose() itself (used by .flyout's own
  // mouseleave and by togglePin's unpin path) — only the trigger's mouseleave,
  // so a legitimate close request (e.g. quickly unpinning right after opening)
  // is never swallowed.
  let suppressNextTriggerLeave = false;

  function openFlyout(): void {
    if (hideTimer) { clearTimeout(hideTimer); hideTimer = null; }
    flyoutOpen = true;
  }
  function onTriggerActivate(): void {
    userOpenedFlyout = true;
    openFlyout();
    // At <=768px width, setting userOpenedFlyout=true synchronously applies
    // .mob-open-hide (display:none) to .bot-trigger — right under a real
    // mouse cursor that just clicked it. The browser then synthesizes a
    // mouseleave for the now-hidden element, which would otherwise call
    // scheduleClose() and, 350ms later, reset userOpenedFlyout=false. The
    // trigger reappears (still under the stationary cursor), the browser
    // synthesizes a fresh mouseenter -> openFlyout() runs again but never
    // restores userOpenedFlyout, leaving the flyout open AND the trigger
    // visible. Touch taps don't hit this (no synthetic hover events), so
    // it's a desktop-narrow-window-with-mouse-only edge case. Fix: swallow
    // the very next mouseleave on the trigger (one-shot) so that synthetic
    // leave can't unwind the state a real click just set; a genuine later
    // mouseleave (the user actually moving the mouse away) is unaffected.
    // Width-gated to <=768 (the only width .mob-open-hide's display:none
    // actually applies at, per the @media rule) — a review caught that
    // an ungated flag also suppressed a real desktop click-then-leave close.
    if (typeof window !== 'undefined' && window.innerWidth <= 768) {
      suppressNextTriggerLeave = true;
    }
  }
  function onTriggerMouseLeave(): void {
    if (suppressNextTriggerLeave) { suppressNextTriggerLeave = false; return; }
    scheduleClose();
  }
  function scheduleClose(): void {
    if (pinned) return;
    hideTimer = setTimeout(() => { flyoutOpen = false; userOpenedFlyout = false; }, 350);
  }
  function cancelClose(): void {
    if (hideTimer) { clearTimeout(hideTimer); hideTimer = null; }
  }
  function togglePin(): void {
    pinned = !pinned;
    if (!pinned) scheduleClose();
  }

  /** Explicit dismiss — the always-available one-tap close. Works on touch, where
   *  there is no mouseleave to trigger scheduleClose. Distinct from Pin: Pin = "keep
   *  open regardless of hover/tap-away"; Close = "dismiss now". Also unpins so a later
   *  re-open reverts to the default hover-follow behavior. Synchronous (clears any
   *  pending scheduleClose timer) so a single click closes immediately — no double-tap. */
  function closeFlyout(): void {
    if (hideTimer) { clearTimeout(hideTimer); hideTimer = null; }
    pinned = false;
    flyoutOpen = false;
    userOpenedFlyout = false;
  }

  // ── Auto-surface: when the assistant pushes a NEW message after the plan has
  // rendered (a remediation note, a /refine reply, a booked confirmation), pop the
  // flyout open so the user actually sees it — collapsing to an emoji must not hide
  // messages they MUST read (e.g. the "tap Confirm again, you weren't charged" note).
  // The plan-render summary is NOT surfaced: we snapshot the message count on the
  // first hasPlan tick (when that summary is already present) and only open on
  // subsequent assistant messages. It stays open until the user hovers out.
  let _msgSeen = -1;
  $: if (hasPlan) {
    if (_msgSeen < 0) {
      _msgSeen = messages.length;
    } else if (messages.length > _msgSeen) {
      if (messages[messages.length - 1]?.role !== 'user') openFlyout();
      _msgSeen = messages.length;
    }
  } else {
    _msgSeen = -1;
  }

  // ── Composer helpers ────────────────────────────────────────────────────────
  function onKey(e: KeyboardEvent): void {
    if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); send(); }
  }
  function send(): void {
    const t = text.trim();
    if (!t || loading) return;
    dispatch('plan', t);
    text = '';
  }
</script>

{#if !hasPlan}
  <!-- ══════════════════════════════════════════════════════════════════════
       EMBEDDED MODE — no plan active; chat lives in a normal aside column.
       Always actionable so specs can fill chat-input on page load.
       ══════════════════════════════════════════════════════════════════════ -->
  <!-- Desktop embedded panel (hidden on mobile, replaced by mob-bubble below) -->
  <aside class="chat" class:collapsed data-testid="chat-pane">
    <header>
      <span class="ttl">Assistant</span>
      <button class="min" title={collapsed ? 'Expand' : 'Minimize'}
        on:click={() => dispatch('toggle', !collapsed)} data-testid="chat-toggle">
        {collapsed ? '▸' : '▾'}
      </button>
    </header>

    {#if !collapsed}
      <div class="msgs" data-testid="chat-msgs">
        {#each messages as m}
          <div class="b {m.role}">{m.text}</div>
        {/each}
        {#if loading}<div class="b note">Planning…</div>{/if}
        {#if showChips}
          <div class="suggest-lbl">Try a format like this</div>
          {#each GRAMMAR_CHIPS as c}
            <button class="suggest-chip" on:click={() => prefill(c.text)} data-testid="grammar-chip">
              <span class="lbl">{c.lbl}</span>{c.text}
            </button>
          {/each}
        {/if}
      </div>
      <div class="box">
        <textarea bind:this={ta} bind:value={text} rows="1"
          placeholder="Plan a trip, or ask to add / swap…  (Shift+Enter = new line)"
          on:keydown={onKey} data-testid="chat-input"></textarea>
        <button class="send" on:click={send} disabled={loading}>Send</button>
      </div>
    {/if}
  </aside>

  <!-- Mobile bubble + slide-up sheet (hidden on desktop via CSS) -->
  <div class="mob-wrap" style="--mob-bottom: {mobSheetBottom}px">
    <!-- Full-width search-bar style trigger, not a small cornered icon: a FAB
         parked in a screen corner is exactly what a hand's palm/thumb covers
         during normal one-handed phone grip — real user report (2026-07-03).
         A wide bar spanning near the full width can't be fully occluded that
         way, and the placeholder text makes its purpose legible at a glance
         instead of relying on a possibly-obscured emoji. -->
    <button class="mob-bubble" class:mob-open-hide={mobOpen} on:click={() => (mobOpen = !mobOpen)}
      aria-label={mobOpen ? 'Close assistant' : 'Open assistant'}>
      <span class="mob-bubble-icon">🤖</span>
      <span class="mob-bubble-txt">Describe your trip…</span>
    </button>
    <div class="mob-sheet" class:mob-open={mobOpen} aria-hidden={!mobOpen}>
      <div class="mob-sheet-hdr">
        <span class="mob-sheet-ttl">Assistant</span>
        <button class="mob-sheet-close" on:click={() => (mobOpen = false)} aria-label="Close">▼</button>
      </div>
      <div class="msgs mob-sheet-msgs">
        {#each messages as m}
          <div class="b {m.role}">{m.text}</div>
        {/each}
        {#if loading}<div class="b note">Planning…</div>{/if}
        {#if showChips}
          <div class="suggest-lbl">Try a format like this</div>
          {#each GRAMMAR_CHIPS as c}
            <button class="suggest-chip" on:click={() => prefill(c.text)} data-testid="grammar-chip">
              <span class="lbl">{c.lbl}</span>{c.text}
            </button>
          {/each}
        {/if}
      </div>
      <div class="box mob-sheet-box">
        <!-- Finding #9 (map/mobile UX sweep): this composer had no data-testid at
             all — specs could only target it via the `.mob-sheet-box textarea`/
             `.mob-sheet-box .send` CSS-selector workaround. Distinct testid
             namespace (mob-*, not the desktop/flyout "chat-input") since this
             sheet can be mounted alongside the flyout's own chat-input — same
             collision-avoidance precedent as PinDetailContent's testidPrefix. -->
        <textarea bind:this={mobTa} bind:value={text} rows="1"
          placeholder="Plan a trip, or ask to add / swap…"
          on:keydown={onKey} data-testid="mob-chat-input"></textarea>
        <button class="send" on:click={send} disabled={loading} data-testid="mob-chat-send">Send</button>
      </div>
    </div>
  </div>

{:else}
  <!-- ══════════════════════════════════════════════════════════════════════
       FLOATING MODE — plan is active; chat collapses to a 🤖 emoji trigger
       fixed at bottom-left. Hover opens the flyout; 📌 pin keeps it open.

       IMPORTANT: the flyout panel is always in the DOM (CSS opacity/transform
       only) so that data-testid="chat-msgs" is always queryable by Playwright.
       Only chat-input inside the OPEN flyout is actionable (.fill-able).
       ══════════════════════════════════════════════════════════════════════ -->

  <!-- Floating trigger -->
  <button class="bot-trigger" class:mob-hide={hideMobileBubble} class:mob-open-hide={userOpenedFlyout}
    aria-label="Open assistant"
    on:mouseenter={openFlyout}
    on:mouseleave={onTriggerMouseLeave}
    on:click={onTriggerActivate}
    data-testid="assistant-trigger">
    <span class="pulse"></span>
    🤖
  </button>

  <!-- Flyout panel — always in DOM; CSS-driven show/hide -->
  <div class="flyout" class:open={flyoutOpen} class:mob-hide={hideMobileBubble}
    style="--mob-bottom: {mobSheetBottom}px"
    on:mouseenter={cancelClose}
    on:mouseleave={scheduleClose}
    role="dialog"
    aria-label="Guild Assistant"
    aria-hidden={!flyoutOpen}
    data-testid="assistant-flyout">
    <div class="fly-header">
      <div class="online-dot"></div>
      <span class="fly-label">Guild Assistant</span>
      <button class="pin-btn" class:pinned on:click={togglePin} data-testid="assistant-pin"
        aria-pressed={pinned}
        title={pinned ? 'Pinned open — click to unpin' : 'Keep open'}>
        📌 {pinned ? 'Pinned' : 'Pin'}
      </button>
      <button class="close-btn" on:click={closeFlyout} data-testid="assistant-close"
        aria-label="Close assistant" title="Close">✕</button>
    </div>

    <!-- chat-msgs: always in DOM so toContainText() works even when flyout is closed -->
    <div class="msgs fly-msgs" data-testid="chat-msgs">
      {#each messages as m}
        <div class="b {m.role}">{m.text}</div>
      {/each}
      {#if loading}<div class="b note">Planning…</div>{/if}
    </div>

    <div class="box fly-box">
      <textarea bind:this={ta} bind:value={text} rows="1"
        placeholder="Ask anything…  (Shift+Enter = new line)"
        on:keydown={onKey} data-testid="chat-input"></textarea>
      <button class="send" on:click={send} disabled={loading}>↑</button>
    </div>
  </div>
{/if}

<style>
  /* ── Embedded mode ──────────────────────────────────────────────────────── */
  /* landing-page-draft2: bounded content-sizing — grows with messages but caps at
     78vh (was a fixed 78vh, flagged by the mockup audit as a real risk in the
     REUSED needs_clarification/error state where messages genuinely accumulate).
     .msgs keeps its own internal scroll (flex:1; overflow:auto below) as the cap. */
  .chat { display: flex; flex-direction: column; height: auto; max-height: 78vh; background: #fff;
    border: 1px solid #ece2d5; border-radius: 14px; overflow: hidden; }
  .chat.collapsed { height: auto; }
  header { display: flex; align-items: center; justify-content: space-between;
    padding: 11px 14px; border-bottom: 1px solid #ece2d5; }
  .ttl { font-size: 12px; letter-spacing: .6px; text-transform: uppercase; color: #7c7468; }
  .min { border: 0; background: #f4ede3; border-radius: 7px; width: 24px; height: 24px;
    cursor: pointer; color: #2d2a26; }

  /* ── Shared message styles ─────────────────────────────────────────────── */
  .msgs { flex: 1; overflow: auto; padding: 12px; display: flex; flex-direction: column; gap: 9px; }
  .b { padding: 8px 11px; border-radius: 12px; font-size: 13.5px; max-width: 92%;
    white-space: pre-wrap; overflow-wrap: break-word; word-break: break-word; }
  .b.user { align-self: flex-end; background: #d9774a; color: #fff; border-bottom-right-radius: 4px; }
  .b.bot { align-self: flex-start; background: #f4ede3; border-bottom-left-radius: 4px; }
  .b.note { align-self: center; background: #fbf0db; color: #c98a2b; font-size: 12px; }

  /* ── Grammar-teaching chips (landing-page-draft2, showGrammarChips) ──────── */
  .suggest-lbl { font-size: 10.5px; color: #a89e94; text-transform: uppercase;
    letter-spacing: .5px; font-weight: 700; margin: 10px 0 8px; }
  .suggest-chip { display: block; width: 100%; text-align: left; background: #fff;
    border: 1px solid #ece2d5; border-radius: 9px; padding: 9px 11px; font-size: 12.5px;
    color: #2d2a26; margin-bottom: 6px; cursor: pointer; font-family: ui-monospace, monospace; }
  .suggest-chip:hover { border-color: #d9774a; background: #fff4ee; }
  .suggest-chip .lbl { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
    color: #a89e94; font-size: 10.5px; display: block; margin-bottom: 2px; }

  /* ── Shared composer styles ────────────────────────────────────────────── */
  .box { display: flex; gap: 8px; padding: 11px; border-top: 1px solid #ece2d5; align-items: flex-end; }
  .box textarea { flex: 1; border: 1px solid #ece2d5; border-radius: 9px; padding: 9px 11px; font: inherit;
    resize: none; overflow-y: auto; height: 40px; max-height: 40px; line-height: 1.4; }
  .send { border: 0; border-radius: 9px; background: #d9774a; color: #fff; font-weight: 600;
    padding: 9px 14px; cursor: pointer; font-family: inherit; }
  .send:disabled { opacity: .6; }

  /* ── Floating trigger ──────────────────────────────────────────────────── */
  .bot-trigger {
    position: fixed;
    left: 18px;
    bottom: 24px;
    z-index: 300;
    width: 52px;
    height: 52px;
    border-radius: 50%;
    background: #d9774a;
    border: 3px solid #fff;
    box-shadow: 0 4px 18px rgba(0,0,0,.18), 0 2px 6px rgba(0,0,0,.1);
    font-size: 22px;
    cursor: pointer;
    display: flex;
    align-items: center;
    justify-content: center;
    transition: transform .2s cubic-bezier(.34,1.56,.64,1), box-shadow .2s;
    overflow: visible;
  }
  .bot-trigger:hover { transform: scale(1.1); box-shadow: 0 8px 28px rgba(0,0,0,.22); }

  .pulse {
    position: absolute;
    inset: -5px;
    border-radius: 50%;
    background: rgba(217,119,74,.18);
    animation: pulse 2.6s ease-out infinite;
    pointer-events: none;
  }
  @keyframes pulse {
    0%   { transform: scale(.82); opacity: .7; }
    60%  { transform: scale(1.3);  opacity: 0; }
    100% { transform: scale(1.3);  opacity: 0; }
  }

  /* ── Flyout panel ──────────────────────────────────────────────────────── */
  .flyout {
    position: fixed;
    left: 82px;
    bottom: 18px;
    width: 300px;
    max-height: 440px;
    background: #fff;
    border: 1px solid #ece2d5;
    border-radius: 14px;
    box-shadow: 0 8px 32px rgba(0,0,0,.16), 0 2px 8px rgba(0,0,0,.08);
    display: flex;
    flex-direction: column;
    overflow: hidden;
    z-index: 299;
    /* Hidden state */
    opacity: 0;
    transform: translateX(-10px) scale(.95);
    pointer-events: none;
    /* visibility:hidden removes the closed flyout from the tab order / a11y tree
       (opacity alone leaves it focusable); delay it until after the fade so the
       close animation still shows. */
    visibility: hidden;
    transition: opacity .18s ease, transform .2s cubic-bezier(.34,1.56,.64,1), visibility 0s linear .2s,
                bottom .15s ease;
    transform-origin: left bottom;
  }
  .flyout.open {
    opacity: 1;
    transform: translateX(0) scale(1);
    pointer-events: all;
    visibility: visible;
    transition: opacity .18s ease, transform .2s cubic-bezier(.34,1.56,.64,1), visibility 0s linear 0s,
                bottom .15s ease;
  }

  .fly-header {
    display: flex;
    align-items: center;
    gap: 8px;
    padding: 10px 14px;
    border-bottom: 1px solid #ece2d5;
    background: #faf6f0;
    flex-shrink: 0;
  }
  .online-dot { width: 7px; height: 7px; background: #4f8a63; border-radius: 50%; flex-shrink: 0; }
  .fly-label { font-size: 12px; font-weight: 700; color: #2d2a26; letter-spacing: .3px; }
  .pin-btn {
    margin-left: auto;
    font-size: 10px;
    padding: 2px 8px;
    border-radius: 6px;
    border: 1px solid #ece2d5;
    background: #fff;
    cursor: pointer;
    color: #7c7468;
    font-family: inherit;
    transition: all .12s;
  }
  .pin-btn:hover { border-color: #d9774a; color: #d9774a; }
  .pin-btn.pinned { background: #d9774a; color: #fff; border-color: #d9774a; }

  .close-btn {
    font-size: 12px;
    line-height: 1;
    width: 22px;
    height: 22px;
    padding: 0;
    display: flex;
    align-items: center;
    justify-content: center;
    border-radius: 6px;
    border: 1px solid #ece2d5;
    background: #fff;
    color: #7c7468;
    cursor: pointer;
    font-family: inherit;
    transition: all .12s;
  }
  .close-btn:hover { border-color: #d9774a; color: #d9774a; background: #fff4ee; }

  .fly-msgs { font-size: 13px; }
  .fly-msgs .b { font-size: 12.5px; }

  .fly-box textarea { font-size: 16px; } /* prevent iOS zoom on focus */
  .fly-box .send { padding: 9px 12px; font-size: 14px; font-weight: 700; }

  /* ── Mobile: flyout goes near-full-width ──────────────────────────────── */
  @media (max-width: 600px) {
    .flyout {
      left: 8px;
      right: 8px;
      width: auto;
      /* Same visualViewport-driven shift the pre-plan .mob-sheet already uses
         (--mob-bottom, wired up by the onMount listener above) — without this
         the flyout's compose box sits under the iOS soft keyboard when
         refining a plan by chat (layout viewport doesn't resize on iOS). */
      bottom: calc(var(--mob-bottom, 0px) + 80px);
      max-height: 70vh;
    }
    .bot-trigger { bottom: 16px; left: 14px; }
    .close-btn { width: 30px; height: 30px; font-size: 14px; }
    .pin-btn { padding: 5px 10px; }
  }

  /* ── Tablet: coordinated with App.svelte's .grid single-column breakpoint
       (max-width:1100px) — between 601-1100px there isn't enough horizontal
       room to place the flyout "beside" the itinerary without overlap, so we
       use the same raised-overlay strategy as phone, but width-capped with a
       real right-margin reservation (via min()/calc()) so content peeks
       through rather than being fully covered, and with a taller max-height
       since tablets are usually taller viewports than phones in portrait. */
  @media (min-width: 601px) and (max-width: 1100px) {
    .flyout {
      left: 18px;
      right: auto;
      width: min(380px, calc(100vw - 96px));
      bottom: calc(var(--mob-bottom, 0px) + 82px);
      max-height: 65vh;
    }
  }

  /* ── hideMobileBubble (Preview/trip-summary open) ───────────────────────
     Preview's sticky bottom bar owns the mobile bottom edge while the trip
     summary is open — hide the floating trigger + flyout there so they don't
     visually collide (see Preview.svelte's tech-note / mockup decision).
     Desktop never conflicts (Preview has no fixed-bottom element there), so
     the hide only applies under the same 768px breakpoint the mob-wrap below
     uses, not unconditionally. */
  @media (max-width: 768px) {
    .bot-trigger.mob-hide, .flyout.mob-hide { display: none !important; }
    /* Trigger stays visible on desktop while the flyout is open (hover-based UX,
       icon + panel side by side is the intended chat-bubble pattern there). On
       mobile the flyout is a near-full-width overlay opened by tapping the same
       icon, and there's already a dedicated close (✕) button inside it -- leaving
       the trigger on screen too was redundant clutter, not a deliberate affordance. */
    .bot-trigger.mob-open-hide { display: none !important; }
  }

  /* ── Mobile bubble + sheet: hidden on desktop ──────────────────────────── */
  .mob-wrap { display: none; }

  /* ── Mobile: replace fixed bar with floating bubble + slide-up sheet ───── */
  @media (max-width: 768px) {
    /* Hide the desktop embedded panel entirely on mobile */
    .chat { display: none !important; }

    /* Show the mobile bubble wrapper */
    .mob-wrap { display: block; }

    /* Full-width search-bar style trigger (was a small circular FAB — a corner
       icon is exactly what a hand's palm/thumb covers during one-handed mobile
       use). Pinned to the bottom edge like the old bubble, but spans the width
       so it can't be fully occluded and its purpose is legible without relying
       on a possibly-obscured emoji alone. */
    .mob-bubble {
      position: fixed;
      left: 14px;
      right: 14px;
      bottom: calc(var(--mob-bottom, 0px) + 16px);
      z-index: 310;
      height: 52px;
      border-radius: 26px;
      background: #fff;
      border: 1px solid #ece2d5;
      box-shadow: 0 4px 18px rgba(0,0,0,.14), 0 2px 6px rgba(0,0,0,.08);
      cursor: pointer;
      display: flex;
      align-items: center;
      gap: 10px;
      padding: 0 8px 0 8px;
      text-align: left;
      transition: transform .15s ease, box-shadow .2s, bottom .15s ease;
      overflow: visible;
    }
    .mob-bubble-icon {
      width: 36px; height: 36px; border-radius: 50%; background: #d9774a;
      display: flex; align-items: center; justify-content: center;
      font-size: 18px; flex-shrink: 0;
    }
    .mob-bubble-txt { font-size: 14.5px; color: #a89e94; font-weight: 500; }
    .mob-bubble:active { transform: scale(.98); }
    /* Same fix as .bot-trigger.mob-open-hide (floating mode): once the sheet is
       open it owns the bottom edge of the screen — the still-fixed bubble sat on
       top of the sheet's compose textarea (z-index 310 over 309), covering its
       first ~58px / the placeholder. The sheet already has its own close (▼)
       affordance in mob-sheet-hdr, so hiding the bubble loses no functionality. */
    .mob-bubble.mob-open-hide { display: none; }

    /* Slide-up sheet */
    .mob-sheet {
      position: fixed;
      left: 0;
      right: 0;
      bottom: var(--mob-bottom, 0px);
      height: 65vh;
      background: #fff;
      border-radius: 16px 16px 0 0;
      box-shadow: 0 -4px 28px rgba(0,0,0,.16);
      z-index: 309;
      display: flex;
      flex-direction: column;
      overflow: hidden;
      transform: translateY(100%);
      transition: transform .28s cubic-bezier(.22,.61,.36,1),
                  bottom .15s ease;
    }
    .mob-sheet.mob-open { transform: translateY(0); }

    .mob-sheet-hdr {
      display: flex;
      align-items: center;
      justify-content: space-between;
      padding: 12px 16px 10px;
      border-bottom: 1px solid #ece2d5;
      background: #faf6f0;
      flex-shrink: 0;
    }
    .mob-sheet-ttl { font-size: 13px; font-weight: 700; color: #2d2a26; letter-spacing: .3px; }
    .mob-sheet-close {
      border: none;
      background: #ede4da;
      border-radius: 8px;
      width: 28px;
      height: 28px;
      cursor: pointer;
      font-size: 12px;
      display: flex;
      align-items: center;
      justify-content: center;
      color: #7c7468;
    }

    .mob-sheet-msgs { flex: 1; overflow-y: auto; -webkit-overflow-scrolling: touch; }

    .mob-sheet-box {
      padding: 10px 14px calc(env(safe-area-inset-bottom, 0px) + 10px);
      border-top: 1px solid #ece2d5;
      flex-shrink: 0;
    }
    .mob-sheet-box textarea { font-size: 16px; } /* prevent iOS zoom on focus */
  }
</style>
