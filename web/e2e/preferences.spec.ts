import { test, expect } from '@playwright/test';

// Hermetic Playwright spec for the Preferences panel UI (#40/#101).
// All network calls are mocked via page.route — no live backend required.
// The /preferences endpoint is GET /preferences?user_id=... (load) and
// PUT /preferences (save), as defined in src/lib/api.ts.

const USERS = {
  demo_users: [
    {
      user_id: 'demo-mei', display_name: 'Mei', persona: 'foodie',
      nationality: 'SG', home_currency: 'SGD', wallet_balance_cents: 500000,
    },
  ],
};

const MEI_PREFS = {
  user_id: 'demo-mei', display_name: 'Mei', persona: 'foodie',
  nationality: 'SG', home_currency: 'SGD',
  prefs: { dietary: 'vegetarian', pace: 'relaxed' },
};

// What the server returns after a successful PUT /preferences.
const SAVE_PREFS = {
  user_id: 'demo-mei', display_name: 'Mei', persona: 'hiker',
  nationality: 'SG', home_currency: 'EUR',
  prefs: { dietary: 'vegetarian', pace: 'relaxed' },
};

/** Wire all standard mock routes for preference-panel tests. */
async function setupRoutes(page: import('@playwright/test').Page): Promise<void> {
  // Fresh picker: no stored session → onMount calls listDemoUsers() → shows picker.
  // In Playwright each test gets an isolated context so localStorage is empty by
  // default; the addInitScript below is defensive for any prior-test leftovers.
  await page.addInitScript(() => {
    localStorage.clear();
  });

  // /session (POST) — list demo users (no user_id body → returns the demo_users array)
  await page.route('**/session', (route) =>
    route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify(USERS),
    }));

  // /session/login (POST) — issued right after a demo user is picked (SECURITY:
  // user_id-trust gap fix). The mocked session_token is threaded by the FE into
  // PUT /preferences and GET /telegram/link below.
  await page.route('**/session/login', (route) =>
    route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ session_token: 'fake-session-token-mei' }),
    }));

  // /preferences — GET returns MEI_PREFS; PUT (save) returns SAVE_PREFS
  await page.route('**/preferences*', (route) => {
    if (route.request().method() === 'GET') {
      return route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify(MEI_PREFS),
      });
    }
    // PUT /preferences
    return route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify(SAVE_PREFS),
    });
  });

  // /emergencies — suppress live overlay
  await page.route('**/emergencies', (route) =>
    route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ status: 'ok', countries: [] }),
    }));

  // Tile requests — abort to stay offline
  await page.route(/tile|openstreetmap|basemaps|demotiles/, (route) => route.abort());
}

/** Log in as Mei via the session picker and wait for prefs-link to appear. */
async function loginAsMei(page: import('@playwright/test').Page): Promise<void> {
  await setupRoutes(page);
  await page.goto('/');
  await expect(page.getByTestId('session-picker')).toBeVisible({ timeout: 8000 });
  await page.getByTestId('user-demo-mei').click();
  await expect(page.getByTestId('prefs-link')).toBeVisible({ timeout: 5000 });
}

/** Log in as Mei and open the preferences panel. */
async function openPrefsPanel(page: import('@playwright/test').Page): Promise<void> {
  await loginAsMei(page);
  await page.getByTestId('prefs-link').click();
  await expect(page.getByTestId('preferences')).toBeVisible({ timeout: 5000 });
}

// ── Test 1 ─────────────────────────────────────────────────────────────────
test('prefs-link shows active user name/persona/currency when logged in', async ({ page }) => {
  await loginAsMei(page);
  const link = page.getByTestId('prefs-link');
  await expect(link).toContainText('Mei');
  await expect(link).toContainText('foodie');
  await expect(link).toContainText('SGD');
});

// ── Test 2 ─────────────────────────────────────────────────────────────────
test('clicking prefs-link opens the preferences panel', async ({ page }) => {
  await loginAsMei(page);
  await page.getByTestId('prefs-link').click();
  await expect(page.getByTestId('preferences')).toBeVisible({ timeout: 5000 });
});

// ── Test 3 ─────────────────────────────────────────────────────────────────
test('preferences panel renders all editable fields with current values', async ({ page }) => {
  await openPrefsPanel(page);
  const panel = page.getByTestId('preferences');

  // All editable fields must be present
  await expect(panel.getByTestId('pref-currency')).toBeVisible();
  await expect(panel.getByTestId('pref-nationality')).toBeVisible();
  await expect(panel.getByTestId('pref-persona')).toBeVisible();
  await expect(panel.getByTestId('pref-dietary')).toBeVisible();
  await expect(panel.getByTestId('pref-pace')).toBeVisible();
  await expect(panel.getByTestId('pref-save')).toBeVisible();

  // Currency select should reflect Mei's SGD from MEI_PREFS
  await expect(panel.getByTestId('pref-currency')).toHaveValue('SGD');

  // Nationality select (ISO-2 dropdown, #40/#101) should pre-select Mei's
  // stored SG code — the exact scenario this fix guarantees against a
  // silently-blank or first-option select.
  await expect(panel.getByTestId('pref-nationality')).toHaveValue('SG');

  // Dietary chip widget (#40/#101 icon+chips pass) should pre-select the 'vegetarian'
  // chip parsed out of MEI_PREFS' stored comma-string, and leave the "Other" overflow
  // field empty — the same pre-fill-correctness guarantee the nationality assertion
  // above covers, applied to the new widget.
  await expect(panel.getByTestId('pref-dietary-vegetarian')).toHaveAttribute('aria-pressed', 'true');
  await expect(panel.getByTestId('pref-dietary-vegan')).toHaveAttribute('aria-pressed', 'false');
  await expect(panel.getByTestId('pref-dietary-other')).toHaveValue('');
});

// ── Test 4 ─────────────────────────────────────────────────────────────────
test('save preferences updates the prefs-link display', async ({ page }) => {
  await openPrefsPanel(page);
  const panel = page.getByTestId('preferences');

  // Capture the PUT /preferences payload (round-trip save correctness for the new
  // dietary chip widget) while still fulfilling with SAVE_PREFS like setupRoutes does.
  let putBody: Record<string, unknown> | null = null;
  await page.route('**/preferences*', async (route) => {
    if (route.request().method() !== 'PUT') return route.fallback();
    putBody = route.request().postDataJSON();
    return route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify(SAVE_PREFS),
    });
  });

  // Change currency to EUR and persona to hiker (matching SAVE_PREFS)
  await panel.getByTestId('pref-currency').selectOption('EUR');
  await panel.getByTestId('pref-persona').selectOption('hiker');

  // Dietary: pre-filled 'vegetarian' chip stays on, add 'gluten-free' (chip toggle) plus
  // a free-text "Other" entry — exercises the new widget's interaction mechanic while
  // preserving the original test's intent (an edit-then-save round trip).
  await panel.getByTestId('pref-dietary-gluten-free').click();
  await panel.getByTestId('pref-dietary-other').fill('no shellfish');

  // Submit — triggers PUT /preferences which returns SAVE_PREFS
  await panel.getByTestId('pref-save').click();

  // Panel closes (view switches back to 'app')
  await expect(page.getByTestId('preferences')).toHaveCount(0, { timeout: 5000 });

  // prefs-link must now reflect the server-returned home_currency (EUR)
  await expect(page.getByTestId('prefs-link')).toContainText('EUR');

  // The saved payload must carry the chip selections + Other overflow, comma-joined
  // into the same single-string wire format the backend's dietary filter expects
  // (day_planner_agent.py) — the widget only changes how the string is composed.
  const sent = putBody as unknown as { prefs?: { dietary?: string }; session_token?: string } | null;
  expect(sent?.prefs?.dietary).toBe('vegetarian, gluten-free, no shellfish');
  // SECURITY (user_id-trust gap fix): the session_token minted at login (via
  // POST /session/login) must be threaded into the PUT /preferences body.
  expect(sent?.session_token).toBe('fake-session-token-mei');
});

// ── Test 6 ─────────────────────────────────────────────────────────────────
// Connect Telegram (deep-link "Connect" flow) — light coverage per the spec:
// button renders, click calls GET /telegram/link, and both available:true /
// available:false responses are handled sanely. The actual external Telegram
// hand-off (tapping Start in the Telegram app) can't be meaningfully tested here.

test('connect-telegram button calls /telegram/link and opens the deep link', async ({ page }) => {
  await openPrefsPanel(page);
  const panel = page.getByTestId('preferences');

  let requestedUserId: string | null = null;
  let requestedSessionToken: string | null = null;
  await page.route('**/telegram/link*', (route) => {
    const url = new URL(route.request().url());
    requestedUserId = url.searchParams.get('user_id');
    requestedSessionToken = url.searchParams.get('session_token');
    return route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        available: true,
        deep_link: 'https://t.me/TravelGuildBot?start=fake-token-abc',
        expires_in_seconds: 900,
      }),
    });
  });

  // Not connected yet → the Connect button is shown (Mei's MEI_PREFS has no
  // telegram_chat_id).
  await expect(panel.getByTestId('telegram-connect-btn')).toBeVisible();

  // Intercept the new-tab open so the test doesn't actually navigate away.
  const [popup] = await Promise.all([
    page.waitForEvent('popup').catch(() => null),
    panel.getByTestId('telegram-connect-btn').click(),
  ]);
  if (popup) await popup.close();

  expect(requestedUserId).toBe('demo-mei');
  // SECURITY (user_id-trust gap fix): the session_token minted at login (via
  // POST /session/login) must be threaded into the GET /telegram/link query.
  expect(requestedSessionToken).toBe('fake-session-token-mei');
  // Follow-up "I've connected — refresh" prompt should now be visible.
  await expect(panel.getByTestId('telegram-refresh-btn')).toBeVisible({ timeout: 5000 });
});

test('connect-telegram shows the honest reason when the bot is not configured', async ({ page }) => {
  await openPrefsPanel(page);
  const panel = page.getByTestId('preferences');

  await page.route('**/telegram/link*', (route) =>
    route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        available: false,
        reason: 'Telegram bot not configured on this deployment.',
      }),
    }));

  await panel.getByTestId('telegram-connect-btn').click();

  await expect(panel.getByTestId('telegram-error')).toBeVisible({ timeout: 5000 });
  await expect(panel.getByTestId('telegram-error')).toContainText('not configured');
  // No broken/fake follow-up step should appear.
  await expect(panel.getByTestId('telegram-refresh-btn')).toHaveCount(0);
});

// ── Test 7 ─────────────────────────────────────────────────────────────────
test('prefs-link not shown for guest users', async ({ page }) => {
  // Pre-seed a guest session in localStorage so the picker is bypassed and
  // activeUser is null → the prefs-link button is not rendered (only a Guest label).
  await page.addInitScript(() => {
    localStorage.clear();
    localStorage.setItem('tg_session', JSON.stringify({ user: null, guest: true, savedAt: Date.now() }));
  });

  await page.route('**/emergencies', (route) =>
    route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ status: 'ok', countries: [] }),
    }));
  await page.route(/tile|openstreetmap|basemaps|demotiles/, (route) => route.abort());

  await page.goto('/');
  // Chat input visible = we skipped the picker and reached the app view
  await expect(page.getByTestId('chat-input')).toBeVisible({ timeout: 8000 });

  // prefs-link must be absent for guest users
  await expect(page.getByTestId('prefs-link')).toHaveCount(0);
});

// ── Test 8 ─────────────────────────────────────────────────────────────────
// SECURITY (user_id-trust gap fix): if the session_token never got minted (e.g.
// POST /session/login failed at login time), Preferences must surface an honest
// error rather than silently sending an unauthenticated PUT/GET.

test('save preferences shows an honest error when no session_token is available', async ({ page }) => {
  // Pre-seed a logged-in session with NO session_token (simulates a login-time
  // failure/expiry) so the picker/login flow is bypassed entirely.
  // setupRoutes() registers its OWN addInitScript (a defensive localStorage.clear()) —
  // it must run BEFORE this seed, not after, or its clear() wins (init scripts run
  // in registration order) and wipes the seed right before goto('/').
  await setupRoutes(page);
  await page.addInitScript(() => {
    localStorage.setItem('tg_session', JSON.stringify({
      user: {
        user_id: 'demo-mei', display_name: 'Mei', persona: 'foodie',
        nationality: 'SG', home_currency: 'SGD', wallet_balance_cents: 500000,
      },
      guest: true,
      savedAt: Date.now(),
      // session_token deliberately omitted
    }));
  });

  let putCalled = false;
  await page.route('**/preferences*', (route) => {
    if (route.request().method() === 'PUT') { putCalled = true; }
    return route.fallback();
  });

  await page.goto('/');
  await expect(page.getByTestId('prefs-link')).toBeVisible({ timeout: 8000 });
  await page.getByTestId('prefs-link').click();
  const panel = page.getByTestId('preferences');
  await expect(panel).toBeVisible({ timeout: 5000 });

  await panel.getByTestId('pref-save').click();

  await expect(panel.locator('.err')).toBeVisible({ timeout: 5000 });
  await expect(panel.locator('.err')).toContainText('session has expired');
  expect(putCalled).toBe(false);   // no unauthenticated PUT was ever sent
});

test('connect-telegram shows an honest error when no session_token is available', async ({ page }) => {
  // setupRoutes() registers its OWN addInitScript (a defensive localStorage.clear()) —
  // it must run BEFORE this seed, not after, or its clear() wins (init scripts run
  // in registration order) and wipes the seed right before goto('/').
  await setupRoutes(page);
  await page.addInitScript(() => {
    localStorage.setItem('tg_session', JSON.stringify({
      user: {
        user_id: 'demo-mei', display_name: 'Mei', persona: 'foodie',
        nationality: 'SG', home_currency: 'SGD', wallet_balance_cents: 500000,
      },
      guest: true,
      savedAt: Date.now(),
      // session_token deliberately omitted
    }));
  });

  let linkCalled = false;
  await page.route('**/telegram/link*', (route) => { linkCalled = true; return route.fallback(); });

  await page.goto('/');
  await expect(page.getByTestId('prefs-link')).toBeVisible({ timeout: 8000 });
  await page.getByTestId('prefs-link').click();
  const panel = page.getByTestId('preferences');
  await expect(panel).toBeVisible({ timeout: 5000 });

  await panel.getByTestId('telegram-connect-btn').click();

  await expect(panel.getByTestId('telegram-error')).toBeVisible({ timeout: 5000 });
  await expect(panel.getByTestId('telegram-error')).toContainText('session has expired');
  expect(linkCalled).toBe(false);   // no unauthenticated link request was ever sent
});
