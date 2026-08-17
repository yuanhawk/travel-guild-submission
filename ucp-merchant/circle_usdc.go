package main

// circle_usdc.go — Circle Agentic Economy Prize: REAL testnet settlement rail via
// Circle's Developer-Controlled Wallets API (api.circle.com/v1/w3s/...).
//
// Deliberately NOT another deterministic simulator like alipay_sim.go — this makes
// genuine HTTP calls to Circle's testnet infrastructure: a real USDC transfer
// between two developer-controlled wallets on a public testnet, not a hash-derived
// fake id.
//
// WIRED INTO THE CHECKOUT FLOW: an agent opts a booking into this rail by setting
// checkout.settlement_rail="circle_usdc" on create_checkout (mirrors the existing
// wallet_session_id opt-in — see checkoutSession.SettlementRail in checkout.go).
// When complete_checkout commits that booking, checkoutTool signals the rail via
// the response's "settlement_rail" field; dispatchTool (mcp.go) then calls
// maybeCircleSettle AFTER checkoutTool has released st.mu. This ordering is not
// optional: complete_checkout holds st.mu for its full duration, and circleSettle
// both re-acquires that lock internally and makes live network calls — running it
// while the store lock is held would either deadlock or stall every other
// checkout/wallet operation on the server for the duration of the Circle round
// trip. The admin endpoint below still exists as a direct/manual entry point
// (useful for testing), but the checkout path is what makes this genuinely
// agent-driven rather than a human manually hitting an admin endpoint.
//
// LIVE-VERIFIED (2026-08-17): a real settlement was run end-to-end against
// api.circle.com on Ethereum Sepolia — entity-secret registration, wallet-set and
// wallet creation, a live transfer call, and on-chain confirmation via a third-party
// RPC call (independent of Circle's own reporting) all completed successfully. The
// request/response field names below are drawn from that live round trip, not
// solely from documentation.
//
// HONESTLY LABELED, same convention as every other settlement rail in this repo:
//   - Every response carries "rail":"circle_usdc_testnet" and "network" so nobody
//     mistakes this for mainnet/real-money settlement.
//   - If CIRCLE_API_KEY / CIRCLE_ENTITY_SECRET aren't configured, this rail refuses
//     to silently no-op or fall back to a fake transaction id — it returns a clear
//     CIRCLE_NOT_CONFIGURED error, mirroring the fail-honest pattern used elsewhere
//     in this codebase (e.g. itinerario-web's narrate.ts NO_GEMINI_CONFIG).
//   - SECURITY: because this rail moves real (testnet) funds with no per-amount
//     ceiling other than the shared budget hard-max, main.go refuses to start if
//     CIRCLE_* credentials are configured but UCP_ADMIN_TOKEN is empty — an
//     unauthenticated real-money endpoint is not an acceptable default.
//
// Endpoints:
//
//	POST /admin/circle/settle  {"booking_ref":"...","total_cents":120000}
//	     -> 200 {"transaction_id","status","booking_ref","total_cents",
//	             "rail":"circle_usdc_testnet","network","note"}
//	     -> 409 if booking_ref was already settled for a DIFFERENT total_cents
//	     -> 501 {"error":"CIRCLE_NOT_CONFIGURED","note"} if credentials are unset
//	     -> 502 {"error":"circle_transfer_failed","note"} on any upstream failure
//	        (upstream response bodies are logged server-side, never echoed to the
//	        client — they can contain wallet IDs and other operational detail)
//	GET  /admin/circle/settle
//	     -> {"settlements":[...],"total_settled_cents","count","rail","network"}

import (
	"bytes"
	"crypto/rand"
	"crypto/rsa"
	"crypto/sha256"
	"crypto/x509"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"encoding/pem"
	"fmt"
	"io"
	"log"
	"net/http"
	"net/url"
	"os"
	"sort"
	"time"
)

const (
	circleAPIBase = "https://api.circle.com/v1/w3s"
	circleNetwork = "ETH-SEPOLIA" // Ethereum Sepolia testnet — widest public faucet availability
	circleNote    = "REAL testnet settlement via Circle's Developer-Controlled Wallets API " +
		"(api.circle.com) — genuine USDC transfer on a public testnet, not mainnet, not a simulation."
	circleNotConfiguredNote = "CIRCLE_API_KEY and/or CIRCLE_ENTITY_SECRET are not set — this rail " +
		"refuses to fake a settlement. Set both (plus CIRCLE_SOURCE_WALLET_ID and " +
		"CIRCLE_MERCHANT_WALLET_ID) to enable real testnet calls."
	// circleEntitySecretBytes is the required raw length of CIRCLE_ENTITY_SECRET
	// per Circle's spec (32-byte entity secret, hex-encoded => 64 hex chars).
	circleEntitySecretBytes = 32
)

type circleSettlement struct {
	BookingRef    string `json:"booking_ref"`
	TotalCents    int    `json:"total_cents"`
	TransactionID string `json:"transaction_id"`
	Status        string `json:"status"`
}

// circleConfig is read once from the environment. All four must be set for the
// rail to attempt a live call — see circleNotConfiguredNote for the fail-honest
// behavior when they aren't.
type circleConfig struct {
	APIKey           string // CIRCLE_API_KEY — format "PREFIX:ID:SECRET" per Circle docs
	EntitySecretHex  string // CIRCLE_ENTITY_SECRET — 32-byte hex, generated+registered once via Circle's dashboard
	SourceWalletID   string // CIRCLE_SOURCE_WALLET_ID — the agent's own developer-controlled wallet
	MerchantWalletID string // CIRCLE_MERCHANT_WALLET_ID — destination wallet (the merchant being paid)
}

func loadCircleConfig() circleConfig {
	return circleConfig{
		APIKey:           os.Getenv("CIRCLE_API_KEY"),
		EntitySecretHex:  os.Getenv("CIRCLE_ENTITY_SECRET"),
		SourceWalletID:   os.Getenv("CIRCLE_SOURCE_WALLET_ID"),
		MerchantWalletID: os.Getenv("CIRCLE_MERCHANT_WALLET_ID"),
	}
}

func (c circleConfig) configured() bool {
	return c.APIKey != "" && c.EntitySecretHex != "" && c.SourceWalletID != "" && c.MerchantWalletID != ""
}

// checkCircleStartupSafety refuses to let the server start with the Circle
// rail enabled in a configuration that lets an UNAUTHENTICATED caller trigger
// a real transfer. Two independent gaps are checked, matching the rail's two
// entry points:
//
//  1. /admin/circle/settle is wrapped in requireAdmin, which is a no-op when
//     UCP_ADMIN_TOKEN is empty.
//  2. complete_checkout (settlement_rail:"circle_usdc", see maybeCircleSettle)
//     is reached via /api/ucp/mcp, which is NOT behind requireAdmin at all —
//     its only gate is the UCP tier/signature system. If signatures aren't
//     required (RequireSignatures==false, the default outside
//     UCP_PROFILE=prod) AND the unsigned-caller tier floor grants checkout
//     capability (UCP_UNSIGNED_TIER=L2, a documented non-prod demo setting —
//     see tierCaps in negotiate.go), any unauthenticated caller can drive a
//     real settlement through the checkout flow, regardless of the admin
//     token on the OTHER endpoint.
func checkCircleStartupSafety(cfg config, circleCfg circleConfig) error {
	if !circleCfg.configured() {
		return nil
	}
	if cfg.AdminToken == "" {
		return fmt.Errorf("CIRCLE_* credentials are set but UCP_ADMIN_TOKEN is empty — refusing to " +
			"start with an unauthenticated /admin/circle/settle endpoint. Set UCP_ADMIN_TOKEN.")
	}
	if !cfg.RequireSignatures {
		for _, c := range tierCaps[cfg.UnsignedTier] {
			if c == "dev.ucp.shopping.checkout" {
				return fmt.Errorf("CIRCLE_* credentials are set, signature verification is disabled "+
					"(UCP_REQUIRE_SIGNATURES=0 or unset outside UCP_PROFILE=prod), and "+
					"UCP_UNSIGNED_TIER=%s grants unauthenticated callers checkout capability — "+
					"refusing to start, since any unsigned caller could drive a real settlement via "+
					"complete_checkout with settlement_rail=\"circle_usdc\". Set "+
					"UCP_REQUIRE_SIGNATURES=1 or UCP_UNSIGNED_TIER=L1.", cfg.UnsignedTier)
			}
		}
	}
	return nil
}

// circleHTTPClient is overridable in tests so circle_usdc_test.go can point it at
// an httptest.Server instead of api.circle.com — see circleBaseURL below.
var circleHTTPClient = &http.Client{Timeout: 15 * time.Second}
var circleBaseURL = circleAPIBase

// entityCiphertext encrypts the entity secret with Circle's published RSA public
// key using RSA-OAEP/SHA-256, base64-encoded — the scheme Circle's own SDKs use to
// generate a fresh, replay-resistant ciphertext per request (never reusing one
// ciphertext across calls). This function is pure and independently unit-tested
// against a locally-generated test keypair in circle_usdc_test.go, so its
// correctness doesn't depend on holding real Circle credentials.
func entityCiphertext(entitySecretHex string, pub *rsa.PublicKey) (string, error) {
	secret, err := hex.DecodeString(entitySecretHex)
	if err != nil {
		return "", fmt.Errorf("entity secret must be hex-encoded: %w", err)
	}
	if len(secret) != circleEntitySecretBytes {
		return "", fmt.Errorf("entity secret must be %d bytes, got %d", circleEntitySecretBytes, len(secret))
	}
	ct, err := rsa.EncryptOAEP(sha256.New(), rand.Reader, pub, secret, nil)
	if err != nil {
		return "", fmt.Errorf("entity secret encryption failed: %w", err)
	}
	return base64.StdEncoding.EncodeToString(ct), nil
}

// fetchEntityPublicKey retrieves Circle's current RSA public key for entity-secret
// encryption. Not cached — fetched fresh per settlement call, since Circle may
// rotate it and the cost of one extra GET is negligible next to the transfer call.
func fetchEntityPublicKey(cfg circleConfig) (*rsa.PublicKey, error) {
	req, err := http.NewRequest(http.MethodGet, circleBaseURL+"/config/entity/publicKey", nil)
	if err != nil {
		return nil, err
	}
	req.Header.Set("Authorization", "Bearer "+cfg.APIKey)
	resp, err := circleHTTPClient.Do(req)
	if err != nil {
		return nil, fmt.Errorf("fetching Circle entity public key: %w", err)
	}
	defer resp.Body.Close()
	body, _ := io.ReadAll(io.LimitReader(resp.Body, 1<<16))
	if resp.StatusCode != http.StatusOK {
		return nil, fmt.Errorf("Circle publicKey endpoint returned %d: %s", resp.StatusCode, string(body))
	}
	var out struct {
		Data struct {
			PublicKey string `json:"publicKey"` // PEM-encoded per Circle docs
		} `json:"data"`
	}
	if err := json.Unmarshal(body, &out); err != nil {
		return nil, fmt.Errorf("parsing Circle publicKey response: %w", err)
	}
	return parseRSAPublicKeyPEM(out.Data.PublicKey)
}

// resolveUSDCTokenID looks up USDC's Circle tokenId by inspecting the source
// wallet's own balances — network-agnostic by construction (no per-chain
// magic constant to keep in sync when the settlement network changes), at
// the cost of requiring the wallet to already hold a nonzero USDC balance
// (i.e. it must have been funded before the first live transfer attempt).
func resolveUSDCTokenID(cfg circleConfig) (string, error) {
	req, err := http.NewRequest(http.MethodGet, circleBaseURL+"/wallets/"+url.PathEscape(cfg.SourceWalletID)+"/balances", nil)
	if err != nil {
		return "", err
	}
	req.Header.Set("Authorization", "Bearer "+cfg.APIKey)
	resp, err := circleHTTPClient.Do(req)
	if err != nil {
		return "", fmt.Errorf("fetching source wallet balances: %w", err)
	}
	defer resp.Body.Close()
	body, _ := io.ReadAll(io.LimitReader(resp.Body, 1<<16))
	if resp.StatusCode != http.StatusOK {
		return "", fmt.Errorf("balances lookup returned %d: %s", resp.StatusCode, string(body))
	}
	var out struct {
		Data struct {
			TokenBalances []struct {
				Token struct {
					ID     string `json:"id"`
					Symbol string `json:"symbol"`
				} `json:"token"`
			} `json:"tokenBalances"`
		} `json:"data"`
	}
	if err := json.Unmarshal(body, &out); err != nil {
		return "", fmt.Errorf("parsing balances response: %w", err)
	}
	for _, tb := range out.Data.TokenBalances {
		if tb.Token.Symbol == "USDC" {
			return tb.Token.ID, nil
		}
	}
	return "", fmt.Errorf("no USDC balance found on source wallet %s — fund it with testnet USDC first", cfg.SourceWalletID)
}

// fetchWalletAddress resolves a Circle developer-wallet ID to its on-chain
// address via GET /v1/w3s/wallets/{id} — needed because the transfer
// endpoint's destinationAddress field wants the address, not the wallet ID.
func fetchWalletAddress(cfg circleConfig, walletID string) (string, error) {
	req, err := http.NewRequest(http.MethodGet, circleBaseURL+"/wallets/"+url.PathEscape(walletID), nil)
	if err != nil {
		return "", err
	}
	req.Header.Set("Authorization", "Bearer "+cfg.APIKey)
	resp, err := circleHTTPClient.Do(req)
	if err != nil {
		return "", fmt.Errorf("fetching wallet %s: %w", walletID, err)
	}
	defer resp.Body.Close()
	body, _ := io.ReadAll(io.LimitReader(resp.Body, 1<<16))
	if resp.StatusCode != http.StatusOK {
		return "", fmt.Errorf("wallet lookup for %s returned %d: %s", walletID, resp.StatusCode, string(body))
	}
	var out struct {
		Data struct {
			Wallet struct {
				Address string `json:"address"`
			} `json:"wallet"`
		} `json:"data"`
	}
	if err := json.Unmarshal(body, &out); err != nil {
		return "", fmt.Errorf("parsing wallet response for %s: %w", walletID, err)
	}
	if out.Data.Wallet.Address == "" {
		return "", fmt.Errorf("wallet %s has no address in response: %s", walletID, body)
	}
	return out.Data.Wallet.Address, nil
}

// parseRSAPublicKeyPEM decodes a PEM-encoded RSA public key (PKIX or
// PKCS1 form, both seen in the wild from Circle's docs/examples).
func parseRSAPublicKeyPEM(pemStr string) (*rsa.PublicKey, error) {
	block, _ := pem.Decode([]byte(pemStr))
	if block == nil {
		return nil, fmt.Errorf("no PEM block found in Circle public key response")
	}
	if pub, err := x509.ParsePKIXPublicKey(block.Bytes); err == nil {
		if rsaPub, ok := pub.(*rsa.PublicKey); ok {
			return rsaPub, nil
		}
		return nil, fmt.Errorf("Circle public key is not RSA")
	}
	if rsaPub, err := x509.ParsePKCS1PublicKey(block.Bytes); err == nil {
		return rsaPub, nil
	}
	return nil, fmt.Errorf("unable to parse Circle public key PEM (neither PKIX nor PKCS1)")
}

// circleTransferRequest mirrors the fields for
// POST /v1/w3s/developer/transactions/transfer (walletId, tokenId,
// destinationAddress, amounts, feeLevel) — confirmed against a live call, see
// module header.
type circleTransferRequest struct {
	IdempotencyKey     string   `json:"idempotencyKey"`
	EntitySecretCipher string   `json:"entitySecretCiphertext"`
	WalletID           string   `json:"walletId"`
	TokenID            string   `json:"tokenId"`
	DestinationAddress string   `json:"destinationAddress"`
	Amounts            []string `json:"amounts"`
	FeeLevel           string   `json:"feeLevel"`
}

type circleTransferResponse struct {
	Data struct {
		ID    string `json:"id"`
		State string `json:"state"`
	} `json:"data"`
}

// errCircleAmountMismatch is returned when a booking_ref that was already
// settled is re-submitted with a DIFFERENT total_cents — this is never
// silently accepted (see finding: a replay must not report success for a
// request it never actually executed).
var errCircleAmountMismatch = fmt.Errorf("booking_ref already settled for a different total_cents")

// circleSettle is the live analogue of alipaySettle: same idempotent-per-booking
// contract, but this one makes a genuine outbound HTTP call instead of hashing.
// Returns (settlement, configured=false) immediately, with no HTTP call, if Circle
// credentials aren't set — see circleNotConfiguredNote.
//
// Concurrency: only one live HTTP attempt per booking_ref runs at a time. A
// concurrent call for the same booking_ref while one is already in flight waits
// for it to finish rather than firing a second transfer — the store mutex is
// deliberately NOT held across the network round trip (that would serialize
// every unrelated store operation in the process), so a per-booking in-flight
// marker does this narrowly instead.
func (st *store) circleSettle(cfg circleConfig, bookingRef string, totalCents int) (circleSettlement, bool, error) {
	for {
		st.mu.Lock()
		if rec, ok := st.circleSettlements[bookingRef]; ok {
			st.mu.Unlock()
			if rec.TotalCents != totalCents {
				return circleSettlement{}, true, errCircleAmountMismatch
			}
			return rec, true, nil // idempotent replay — no second HTTP call
		}
		wait, inFlight := st.circleInFlight[bookingRef]
		if inFlight {
			st.mu.Unlock()
			<-wait // another goroutine is already settling this booking_ref
			continue
		}
		if !cfg.configured() {
			st.mu.Unlock()
			return circleSettlement{}, false, nil
		}
		done := make(chan struct{})
		st.circleInFlight[bookingRef] = done
		st.mu.Unlock()
		defer func() {
			st.mu.Lock()
			delete(st.circleInFlight, bookingRef)
			st.mu.Unlock()
			close(done)
		}()
		break
	}

	pub, err := fetchEntityPublicKey(cfg)
	if err != nil {
		return circleSettlement{}, true, fmt.Errorf("circle: %w", err)
	}
	cipher, err := entityCiphertext(cfg.EntitySecretHex, pub)
	if err != nil {
		return circleSettlement{}, true, fmt.Errorf("circle: %w", err)
	}

	// The transfer endpoint's destinationAddress wants the merchant wallet's
	// on-chain address, not its Circle wallet ID — resolved live via GET
	// /v1/w3s/wallets/{id} (confirmed necessary via a real 400 from Circle
	// when an ID was sent where an address belongs).
	destAddr, err := fetchWalletAddress(cfg, cfg.MerchantWalletID)
	if err != nil {
		return circleSettlement{}, true, fmt.Errorf("circle: resolving merchant wallet address: %w", err)
	}

	tokenID, err := resolveUSDCTokenID(cfg)
	if err != nil {
		return circleSettlement{}, true, fmt.Errorf("circle: %w", err)
	}

	// USDC has 6 decimals; total_cents is USD cents, so amount = total_cents / 100
	// dollars, expressed as a decimal string per Circle's documented "amounts" field.
	amountUSD := fmt.Sprintf("%d.%02d", totalCents/100, totalCents%100)

	reqBody := circleTransferRequest{
		IdempotencyKey:     deterministicUUID(bookingRef), // stable per booking_ref, so a retried
		EntitySecretCipher: cipher,                        // request after a network failure can't double-spend
		WalletID:           cfg.SourceWalletID,
		TokenID:            tokenID,
		DestinationAddress: destAddr,
		Amounts:            []string{amountUSD},
		FeeLevel:           "MEDIUM",
	}
	buf, err := json.Marshal(reqBody)
	if err != nil {
		return circleSettlement{}, true, err
	}
	req, err := http.NewRequest(http.MethodPost, circleBaseURL+"/developer/transactions/transfer", bytes.NewReader(buf))
	if err != nil {
		return circleSettlement{}, true, err
	}
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("Authorization", "Bearer "+cfg.APIKey)

	resp, err := circleHTTPClient.Do(req)
	if err != nil {
		return circleSettlement{}, true, fmt.Errorf("circle transfer request failed: %w", err)
	}
	defer resp.Body.Close()
	body, _ := io.ReadAll(io.LimitReader(resp.Body, 1<<16))
	if resp.StatusCode != http.StatusOK && resp.StatusCode != http.StatusCreated && resp.StatusCode != http.StatusAccepted {
		return circleSettlement{}, true, fmt.Errorf("circle transfer returned %d: %s", resp.StatusCode, string(body))
	}
	var parsed circleTransferResponse
	if err := json.Unmarshal(body, &parsed); err != nil {
		return circleSettlement{}, true, fmt.Errorf("parsing circle transfer response: %w", err)
	}

	rec := circleSettlement{
		BookingRef:    bookingRef,
		TotalCents:    totalCents,
		TransactionID: parsed.Data.ID,
		Status:        parsed.Data.State,
	}
	st.mu.Lock()
	st.circleSettlements[bookingRef] = rec
	st.mu.Unlock()
	return rec, true, nil
}

func (st *store) circleSummary() (recs []circleSettlement, total int) {
	st.mu.Lock()
	defer st.mu.Unlock()
	for _, r := range st.circleSettlements {
		recs = append(recs, r)
		total += r.TotalCents
	}
	sort.Slice(recs, func(i, j int) bool { return recs[i].BookingRef < recs[j].BookingRef })
	return recs, total
}

// maybeCircleSettle is the genuine agent-driven entry point into this rail —
// called from dispatchTool (mcp.go) right after checkoutTool returns from a
// complete_checkout call, and ONLY after that call's st.mu has been released
// (see the module header for why this ordering is mandatory, not stylistic).
//
// It looks for "settlement_rail":"circle_usdc" on the checkoutTool response
// (set by checkout.go when the session opted in via
// checkout.settlement_rail="circle_usdc" at create_checkout) and, if present,
// settles the booking's own total_cents — which checkoutTool already bounded
// by min(user_budget, BudgetHardMaxCents) before the booking_ref was even
// generated, so no separate cap check is needed here (unlike the admin
// endpoint, which is a direct entry point with no such upstream gate).
//
// resp is mutated in place (adding "circle_settlement") and returned for
// call-site convenience. A booking with no settlement_rail opt-in is
// returned completely unchanged — this must never affect a booking that
// didn't ask for it.
func maybeCircleSettle(st *store, resp map[string]any) map[string]any {
	if resp == nil || resp["settlement_rail"] != "circle_usdc" {
		return resp
	}
	bookingRef, _ := resp["booking_ref"].(string)
	totalCents, _ := resp["total_cents"].(int64)
	if bookingRef == "" || totalCents <= 0 {
		return resp
	}
	cfg := loadCircleConfig()
	rec, attempted, err := st.circleSettle(cfg, bookingRef, int(totalCents))
	switch {
	case !attempted:
		resp["circle_settlement"] = map[string]any{
			"error": "CIRCLE_NOT_CONFIGURED", "note": circleNotConfiguredNote,
		}
	case err == errCircleAmountMismatch:
		// Structurally shouldn't happen (booking_ref is generated fresh per
		// completed checkout, so a mismatch would mean a booking_ref collision)
		// but handled explicitly rather than falling into the generic err branch.
		log.Printf("circle settle amount mismatch for booking_ref=%s (complete_checkout path)", bookingRef)
		resp["circle_settlement"] = map[string]any{"error": "booking_ref_amount_mismatch"}
	case err != nil:
		log.Printf("circle settle failed for booking_ref=%s (complete_checkout path): %v", bookingRef, err)
		resp["circle_settlement"] = map[string]any{
			"error": "circle_transfer_failed", "note": "see server logs for details",
		}
	default:
		resp["circle_settlement"] = map[string]any{
			"transaction_id": rec.TransactionID,
			"status":         rec.Status,
			"rail":           "circle_usdc_testnet",
			"network":        circleNetwork,
			"note":           circleNote,
		}
	}
	return resp
}

// circleAdminHandler wires the Circle rail's HTTP surface. budgetHardMaxCents
// is the same ceiling enforced elsewhere in checkout.go (BudgetHardMaxCents) —
// this rail must not be a way to move money past a limit the rest of the
// system enforces.
func circleAdminHandler(st *store, budgetHardMaxCents int64) http.HandlerFunc {
	cfg := loadCircleConfig()
	return func(w http.ResponseWriter, r *http.Request) {
		switch r.Method {
		case http.MethodGet:
			recs, total := st.circleSummary()
			writeJSON(w, http.StatusOK, map[string]any{
				"settlements":         recs,
				"total_settled_cents": total,
				"count":               len(recs),
				"rail":                "circle_usdc_testnet",
				"network":             circleNetwork,
				"configured":          cfg.configured(),
				"note":                circleNote,
			})
		case http.MethodPost:
			body, _ := io.ReadAll(io.LimitReader(r.Body, 4096))
			var rf struct {
				BookingRef string `json:"booking_ref"`
				TotalCents int    `json:"total_cents"`
			}
			if err := json.Unmarshal(body, &rf); err != nil || rf.BookingRef == "" || rf.TotalCents <= 0 {
				writeJSON(w, http.StatusBadRequest, map[string]string{
					"error": "booking_ref and positive total_cents required"})
				return
			}
			if int64(rf.TotalCents) > budgetHardMaxCents {
				writeJSON(w, http.StatusBadRequest, map[string]string{
					"error": "exceeds_budget_hard_max",
					"note":  "total_cents exceeds this server's BUDGET_HARD_MAX_USD ceiling",
				})
				return
			}
			rec, attempted, err := st.circleSettle(cfg, rf.BookingRef, rf.TotalCents)
			if !attempted {
				writeJSON(w, http.StatusNotImplemented, map[string]string{
					"error": "CIRCLE_NOT_CONFIGURED",
					"note":  circleNotConfiguredNote,
				})
				return
			}
			if err == errCircleAmountMismatch {
				writeJSON(w, http.StatusConflict, map[string]string{
					"error": "booking_ref_amount_mismatch",
					"note":  "this booking_ref was already settled for a different total_cents",
				})
				return
			}
			if err != nil {
				// Upstream response bodies (logged here) can contain wallet IDs and
				// other operational detail — never echoed verbatim to the caller.
				log.Printf("circle settle failed for booking_ref=%s: %v", rf.BookingRef, err)
				writeJSON(w, http.StatusBadGateway, map[string]string{
					"error": "circle_transfer_failed",
					"note":  "see server logs for details",
				})
				return
			}
			writeJSON(w, http.StatusOK, map[string]any{
				"transaction_id": rec.TransactionID,
				"status":         rec.Status,
				"booking_ref":    rec.BookingRef,
				"total_cents":    rec.TotalCents,
				"rail":           "circle_usdc_testnet",
				"network":        circleNetwork,
				"note":           circleNote,
			})
		default:
			writeJSON(w, http.StatusMethodNotAllowed, map[string]string{"error": "method not allowed"})
		}
	}
}

// deterministicUUID derives a stable, UUIDv4-shaped idempotency key from
// booking_ref so a retried settlement request (e.g. after a network blip) reuses
// the same key rather than risking a double transfer — same var-0 discipline as
// alipaySimTxnID, applied to Circle's required idempotencyKey field instead of a
// fake transaction id. Version/variant nibbles are forced to valid v4 values
// since Circle documents idempotencyKey as a UUIDv4.
func deterministicUUID(seed string) string {
	h := sha256.Sum256([]byte("circle-idem|" + seed))
	h[6] = (h[6] & 0x0f) | 0x40 // version 4
	h[8] = (h[8] & 0x3f) | 0x80 // variant 10xx
	return fmt.Sprintf("%x-%x-%x-%x-%x", h[0:4], h[4:6], h[6:8], h[8:10], h[10:16])
}
