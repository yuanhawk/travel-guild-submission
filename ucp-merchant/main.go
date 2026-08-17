// UCP Merchant Server (plan §7–8). stdlib-only; mock data.
//
// SECURITY INVARIANT: budget ceilings + HITL/mandate consent are enforced in
// code (checkout.go) — never by the LLM/prompt. The autonomy tier is derived
// SERVER-SIDE from the RFC 9421-authenticated signer's grant; the client's
// requested tier is only an upper bound. The agent can only *request*.
package main

import (
	"crypto/subtle"
	"encoding/json"
	"log"
	"net/http"
	"os"
	"strconv"
)

type config struct {
	ListenAddr         string
	BaseURL            string
	BudgetCeilingCents int64
	BudgetHardMaxCents int64
	DefaultTier        string            // requested-tier default when unspecified
	RequireSignatures  bool              // reject unsigned requests
	TierGrants         map[string]string // verified profileURL -> max granted tier
	UnsignedTier       string            // grant floor for unsigned callers (default L1; L2 = NON-PROD demo)
	AllowDevSSRF       bool              // #56: UCP_ALLOW_DEV_SSRF=1 permits loopback/private profile fetch (dev/CI only)
	// VULN-002 / D1: UCP_PROFILE=prod defaults to RequireSignatures=true.
	// D2: admin token for /admin/sim/* endpoints (constant-time compare).
	// When UCP_PROFILE=prod and AdminToken is empty the server refuses to start.
	AdminToken string
}

func loadConfig() config {
	tier := envOr("UCP_DEFAULT_AUTONOMY", "L2")
	if _, ok := tierCaps[tier]; !ok {
		tier = "L2"
	}
	grants := map[string]string{}
	if raw := os.Getenv("UCP_TIER_GRANTS"); raw != "" {
		_ = json.Unmarshal([]byte(raw), &grants)
	}
	unsigned := envOr("UCP_UNSIGNED_TIER", "L1")
	if _, ok := tierCaps[unsigned]; !ok {
		unsigned = "L1"
	}

	// VULN-002 / D1: UCP_PROFILE=prod sets secure defaults.
	// UCP_REQUIRE_SIGNATURES=1 can always override (explicit beats profile).
	// Do NOT flip the bare RequireSignatures default (breaks CI unsigned sweep).
	isProd := os.Getenv("UCP_PROFILE") == "prod"
	requireSig := os.Getenv("UCP_REQUIRE_SIGNATURES") == "1"
	if isProd && os.Getenv("UCP_REQUIRE_SIGNATURES") == "" {
		// prod default: require signatures unless explicitly opted out with =0.
		requireSig = true
	}
	if isProd && os.Getenv("UCP_REQUIRE_SIGNATURES") == "0" {
		log.Println("WARNING: UCP_PROFILE=prod but UCP_REQUIRE_SIGNATURES=0 — " +
			"signature enforcement is DISABLED; all ownership checks are inoperative. " +
			"This configuration should never be used in production.")
	}

	adminToken := os.Getenv("UCP_ADMIN_TOKEN")
	if isProd && adminToken == "" {
		log.Fatal("UCP_PROFILE=prod requires UCP_ADMIN_TOKEN to be set (fail-closed admin gate)")
	}

	return config{
		ListenAddr:         envOr("UCP_LISTEN_ADDR", "127.0.0.1:8090"),
		BaseURL:            envOr("UCP_MERCHANT_BASE_URL", "http://127.0.0.1:8090"),
		BudgetCeilingCents: usdCents("BUDGET_DEFAULT_CEILING_USD", 800),
		BudgetHardMaxCents: usdCents("BUDGET_HARD_MAX_USD", 2000),
		DefaultTier:        tier,
		RequireSignatures:  requireSig,
		TierGrants:         grants,
		UnsignedTier:       unsigned,
		AllowDevSSRF:       os.Getenv("UCP_ALLOW_DEV_SSRF") == "1",
		AdminToken:         adminToken,
	}
}

// requireAdmin wraps an http.HandlerFunc with a token-auth check for the
// /admin/sim/* endpoints (D2 admin gate). When cfg.AdminToken is set, the
// request must supply "X-Admin-Token: <token>" (constant-time compare) or it
// receives 401. When AdminToken is empty the handler is open (CI / demo).
// Under UCP_PROFILE=prod AdminToken is required at startup (see loadConfig).
func requireAdmin(cfg config, h http.HandlerFunc) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		if cfg.AdminToken != "" {
			tok := r.Header.Get("X-Admin-Token")
			if subtle.ConstantTimeCompare([]byte(tok), []byte(cfg.AdminToken)) != 1 {
				writeJSON(w, http.StatusUnauthorized,
					map[string]string{"error": "admin_token_required"})
				return
			}
		}
		h(w, r)
	}
}

func main() {
	// Resolved once at startup (not per-request) — see resolveGitSHA in
	// gitsha.go and /health below, which must never shell out per-request
	// just to report build provenance (mirrors server.py's _GIT_SHA).
	gitSHA = resolveGitSHA(os.Getenv)
	cfg := loadConfig()
	circleCfg := loadCircleConfig()
	if err := checkCircleStartupSafety(cfg, circleCfg); err != nil {
		log.Fatal(err)
	}
	if circleCfg.configured() {
		if circleCfg.AggregateCapCents == 0 {
			log.Printf("WARNING: Circle rail is credentialed but CIRCLE_AGGREGATE_CAP_USD resolves to 0¢ — every settlement will be refused")
		} else {
			log.Printf("circle_usdc rail ENABLED (network %s, aggregate cap %d¢ for this process lifetime)", circleNetwork, circleCfg.AggregateCapCents)
		}
	}
	st := newStore()
	mk, loaded := loadOrCreateKey()
	if loaded {
		log.Printf("signing key loaded (kid=%s)", mk.kid)
	} else {
		log.Printf("WARNING: ephemeral signing key (kid=%s) — set UCP_SIGNING_KEY_PATH for stability", mk.kid)
	}
	seedDeviceKeys(st) // UCP_DEVICE_KEYS: user_id -> JWK (L3 enrollment, demo seed)
	if cfg.AllowDevSSRF {
		log.Printf("WARNING: UCP_ALLOW_DEV_SSRF=1 — loopback/private agent-profile fetch PERMITTED (dev/CI only; NEVER enable in production)")
	}

	verifier := newSigVerifier(httpKeyResolver(cfg.AllowDevSSRF))
	mux := http.NewServeMux()
	mux.HandleFunc("/.well-known/ucp", ucpProfileHandler(cfg, mk))
	mux.HandleFunc("/api/ucp/mcp", mcpHandler(cfg, st, verifier))
	mux.HandleFunc("/health", func(w http.ResponseWriter, _ *http.Request) {
		writeJSON(w, http.StatusOK, map[string]string{"status": "ok", "git_sha": gitSHA})
	})
	// L3 World Simulator admin endpoints (demo/test; loopback-only in Docker mesh).
	// All endpoints are token-gated when UCP_ADMIN_TOKEN is set (D2 admin gate).
	// POST /admin/sim/availability  {"hotel_id":"...", "available":false}
	// GET  /admin/sim/availability  → list unavailable hotels
	mux.HandleFunc("/admin/sim/availability", requireAdmin(cfg, func(w http.ResponseWriter, r *http.Request) {
		switch r.Method {
		case http.MethodPost:
			simAdminHandler(st)(w, r)
		case http.MethodGet:
			simStatusHandler(st)(w, r)
		default:
			writeJSON(w, http.StatusMethodNotAllowed, map[string]string{"error": "GET or POST required"})
		}
	}))
	// Cascade fault: cancelled flight/transfer edges (read by the Transport agent).
	// POST /admin/sim/transfer  {"from_city":"...","to_city":"...","feasible":false}
	// GET  /admin/sim/transfer  → list cancelled transfers
	mux.HandleFunc("/admin/sim/transfer", requireAdmin(cfg, simTransferAdminHandler(st)))

	// POST /admin/sim/counterparty  {"counterparty_id":"...","solvent":false}
	// GET  /admin/sim/counterparty  → list insolvent counterparties (N1 fault)
	mux.HandleFunc("/admin/sim/counterparty", requireAdmin(cfg, simCounterpartyAdminHandler(st)))

	// SIMULATED prepaid wallet (honestly labeled; settlement-layer state, KIV real rail):
	// POST /admin/sim/wallet {"wallet_session_id":"...","wallet_balance_cents":N} → fund/RESET
	// GET  /admin/sim/wallet → all wallet summaries
	mux.HandleFunc("/admin/sim/wallet", requireAdmin(cfg, walletSimAdminHandler(st)))

	// #57 AP2 SIMULATED settlement (honestly labeled; real Alipay = business account, KIV):
	// POST /admin/sim/alipay/settle {"booking_ref":"...","total_cents":N} → {transaction_id,status:"paid",simulated:true}
	// GET  /admin/sim/alipay/settle → settlement summary
	mux.HandleFunc("/admin/sim/alipay/settle", requireAdmin(cfg, alipaySimAdminHandler(st)))

	// Circle Agentic Economy Prize: REAL testnet settlement rail (honestly labeled,
	// not simulated — genuine HTTP calls to api.circle.com; see circle_usdc.go):
	// POST /admin/circle/settle {"booking_ref":"...","total_cents":N} → real transfer, or
	//      501 CIRCLE_NOT_CONFIGURED if CIRCLE_API_KEY/CIRCLE_ENTITY_SECRET unset
	// GET  /admin/circle/settle → settlement summary
	mux.HandleFunc("/admin/circle/settle", requireAdmin(cfg, circleAdminHandler(st, cfg.BudgetHardMaxCents)))

	log.Printf("ucp-merchant on %s (ceiling %d¢, hard-max %d¢, default-tier %s, require-sig %v, grants %d)",
		cfg.ListenAddr, cfg.BudgetCeilingCents, cfg.BudgetHardMaxCents, cfg.DefaultTier, cfg.RequireSignatures, len(cfg.TierGrants))
	log.Fatal(http.ListenAndServe(cfg.ListenAddr, mux))
}

// seedDeviceKeys registers L3 device public keys from UCP_DEVICE_KEYS (JSON:
// {"user_id":{"x":"...","y":"..."}}). A real flow registers via the user's L3
// opt-in; this is the demo enrollment path.
func seedDeviceKeys(st *store) {
	raw := os.Getenv("UCP_DEVICE_KEYS")
	if raw == "" {
		return
	}
	var m map[string]struct{ X, Y string }
	if err := json.Unmarshal([]byte(raw), &m); err != nil {
		log.Printf("UCP_DEVICE_KEYS parse error: %v", err)
		return
	}
	for uid, jwk := range m {
		if pub, err := pubFromJWK(jwk.X, jwk.Y); err == nil {
			st.registerUserKey(uid, pub)
			log.Printf("registered L3 device key for user %s", uid)
		}
	}
}

func writeJSON(w http.ResponseWriter, code int, v any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(code)
	_ = json.NewEncoder(w).Encode(v)
}

func envOr(k, d string) string {
	if v := os.Getenv(k); v != "" {
		return v
	}
	return d
}

func usdCents(k string, defDollars float64) int64 {
	d := defDollars
	if v := os.Getenv(k); v != "" {
		if f, err := strconv.ParseFloat(v, 64); err == nil {
			d = f
		}
	}
	return int64(d*100 + 0.5)
}
