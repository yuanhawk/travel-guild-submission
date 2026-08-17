package main

import (
	"crypto/ecdsa"
	"crypto/rand"
	"encoding/hex"
	"encoding/json"
	"net/http"
	"sync"
	"time"
)

type lineItem struct {
	HotelID  string `json:"hotel_id"`
	Checkin  string `json:"checkin"`
	Checkout string `json:"checkout"`
	Adults   int    `json:"adults"`
	// #44 — 2nd checkout KIND (FOOD_DELIVERY) on the SAME rails. When FoodID is
	// set the item is priced via findFood (×Qty); when it is "" the hotel path is
	// BYTE-IDENTICAL to before (the food branch is fully gated behind FoodID!="").
	// omitempty keeps a pure-lodging line item BYTE-IDENTICAL in sessionView:
	// when FoodID=="" and Qty==0 these keys are absent from the JSON, so a
	// lodging-only checkout serializes exactly as it did before #44.
	FoodID string `json:"food_id,omitempty"`
	Qty    int    `json:"qty,omitempty"`
}

// idFor returns the counterparty/catalog id for a line item: the FoodID when set
// (FOOD_DELIVERY), else the HotelID (HOTEL — the legacy implicit kind). Used by
// the insolvency loop so it works for either kind without forking the path.
func idFor(li lineItem) string {
	if li.FoodID != "" {
		return li.FoodID
	}
	return li.HotelID
}

// qtyOf returns the line-item quantity, defaulting to 1 (food line items may
// carry a Qty; absent/<=0 means a single unit).
func qtyOf(li lineItem) int {
	if li.Qty > 0 {
		return li.Qty
	}
	return 1
}

// WALLET-003 — overflow guard constants and safe-arithmetic helpers.
// maxQty caps food/item line quantities to a sane per-line ceiling.
// maxNights caps hotel stays to 10 years (adversarial input guard).
const maxQty = 100_000
const maxNights = 3_650

// checkedMul multiplies two int64 values and returns (product, ok).
// ok=false means the result overflowed int64 (positive or negative).
func checkedMul(a, b int64) (int64, bool) {
	if a == 0 || b == 0 {
		return 0, true
	}
	p := a * b
	if p/b != a {
		return 0, false
	}
	return p, true
}

// checkedAdd adds two int64 values and returns (sum, ok).
// ok=false means the result overflowed int64.
func checkedAdd(a, b int64) (int64, bool) {
	s := a + b
	if (s > a) != (b > 0) {
		return 0, false
	}
	return s, true
}

type checkoutSession struct {
	ID     string `json:"id"`
	// UserID is the bound end user (session_token/owner_token-verified upstream
	// by the Python orchestrator's _authorize_trip_action); enforced at L2 in
	// prod (RequireSignatures) alongside AgentID — see the END-USER OWNERSHIP
	// checks in checkoutTool.
	UserID          string     `json:"user_id"`
	AgentID         string     `json:"-"` // bound authenticated agent (ownership)
	LineItems       []lineItem `json:"line_items"`
	TotalCents      int64      `json:"total_cents"`
	UserBudgetCents int64      `json:"user_budget_cents,omitempty"` // per-trip budget from caller
	Currency        string     `json:"currency"`
	BuyerConsent    bool       `json:"buyer_consent"`
	Status          string     `json:"status"` // incomplete | complete | cancelled
	BookingRef      string     `json:"booking_ref,omitempty"`
	// SIMULATED prepaid wallet binding. When non-empty, complete_checkout debits
	// and cancel_checkout credits the wallet keyed by this id (the deterministic
	// per-run request digest). Empty → ALL wallet logic is skipped (backward-
	// compat: responses are byte-identical to pre-wallet behaviour).
	WalletSessionID string `json:"-"`
	// SettlementRail selects a REAL settlement rail for complete_checkout,
	// triggered AFTER st.mu is released (see maybeCircleSettle in
	// circle_usdc.go — the rail makes live network calls, which must never
	// run while holding the store lock). "" → no live settlement attempted
	// (byte-identical to pre-Circle behaviour). Currently only "circle_usdc"
	// is recognized; anything else is ignored.
	SettlementRail string `json:"-"`
}

type store struct {
	mu             sync.Mutex
	sessions       map[string]*checkoutSession
	idempotencyMap map[string]string             // client idempotency_key -> session ID
	userKeys       map[string]*ecdsa.PublicKey   // user_id -> registered device pubkey (L3)
	unavailable    map[string]bool               // L3 sim: hotel_id -> sold-out (sim.go)
	unavailableTransfers map[transferKey]bool     // cascade sim: cancelled flight/transfer edges (sim.go)
	counterpartyInsolvent map[string]bool         // N1 sim: counterparty_id -> insolvent → bookings VOID (sim.go)
	alipaySettlements map[string]alipaySettlement // #57 AP2 sim: booking_ref -> SIMULATED settlement (alipay_sim.go)
	wallets           map[string]*wallet          // SIMULATED prepaid wallet: wallet_session_id -> balance+ledger (wallet.go)
	circleSettlements map[string]circleSettlement // Circle Agentic Economy: booking_ref -> REAL testnet settlement (circle_usdc.go)
	circleInFlight    map[string]chan struct{}     // booking_ref -> close-when-done, guards against concurrent double-transfer (circle_usdc.go)
	// NEW-4 aggregate spend ceiling. Cents RESERVED-OR-SETTLED by the Circle
	// rail for this process's lifetime: incremented under st.mu at the same
	// moment the in-flight marker is taken (i.e. BEFORE the network round
	// trip, so concurrent settles for different booking_refs cannot
	// collectively overshoot the cap), decremented again only if that attempt
	// fails. Invariant once no settle is in flight:
	//   circleCommittedCents == sum(circleSettlements[*].TotalCents)
	// In-memory only, like everything else in this store — see circleSettle.
	circleCommittedCents int64
}

func newStore() *store {
	return &store{
		sessions:       map[string]*checkoutSession{},
		idempotencyMap: map[string]string{},
		userKeys:       map[string]*ecdsa.PublicKey{},
		unavailable:    map[string]bool{},
		unavailableTransfers: map[transferKey]bool{},
		counterpartyInsolvent: map[string]bool{},
		alipaySettlements: map[string]alipaySettlement{},
		wallets:           map[string]*wallet{},
		circleSettlements: map[string]circleSettlement{},
		circleInFlight:    map[string]chan struct{}{},
	}
}

func (s *store) registerUserKey(userID string, pub *ecdsa.PublicKey) {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.userKeys[userID] = pub
}

func lineItemsTotalCents(items []lineItem) (int64, bool) {
	var total int64
	for _, li := range items {
		// #44 — FOOD_DELIVERY branch, gated behind FoodID!="". Priced as
		// (item price + delivery fee) × Qty in integer cents. When FoodID is ""
		// the HOTEL path below is BYTE-IDENTICAL to before.
		if li.FoodID != "" {
			f, ok := findFood(li.FoodID)
			if !ok {
				return 0, false
			}
			// WALLET-003: overflow-safe multiply (qty capped at maxQty by create_checkout).
			linePrice, ok2 := checkedMul(f.PriceCents+f.DeliveryFeeCents, int64(qtyOf(li)))
			if !ok2 {
				return 0, false
			}
			newTotal, ok3 := checkedAdd(total, linePrice)
			if !ok3 {
				return 0, false
			}
			total = newTotal
			continue
		}
		h, ok := findHotel(li.HotelID)
		if !ok {
			return 0, false
		}
		// WALLET-003: cap nights and overflow-safe multiply.
		n := int64(nights(li.Checkin, li.Checkout))
		if n > maxNights {
			return 0, false
		}
		linePrice, ok2 := checkedMul(h.PriceCentsNight, n)
		if !ok2 {
			return 0, false
		}
		newTotal, ok3 := checkedAdd(total, linePrice)
		if !ok3 {
			return 0, false
		}
		total = newTotal
	}
	return total, true
}

// lineItemsTotalCentsChecked is the sim-aware variant used by checkoutTool.
// Returns (total, unavailableHotelID, ok):
//   - ok=false when a hotel_id is not in the catalog.
//   - unavailableHotelID!="" when a hotel is marked sold-out by the sim.
func lineItemsTotalCentsChecked(st *store, items []lineItem) (total int64, unavailableID string, ok bool) {
	for _, li := range items {
		// #44 — FOOD_DELIVERY branch, gated behind FoodID!="". No sim sold-out
		// gate for food (inventory is simulated-available); priced in integer
		// cents. The HOTEL path below is BYTE-IDENTICAL to before.
		if li.FoodID != "" {
			f, found := findFood(li.FoodID)
			if !found {
				return 0, "", false
			}
			// WALLET-003: overflow-safe multiply (qty capped at maxQty by create_checkout).
			linePrice, ok2 := checkedMul(f.PriceCents+f.DeliveryFeeCents, int64(qtyOf(li)))
			if !ok2 {
				return 0, "", false
			}
			newTotal, ok3 := checkedAdd(total, linePrice)
			if !ok3 {
				return 0, "", false
			}
			total = newTotal
			continue
		}
		h, found := findHotel(li.HotelID)
		if !found {
			return 0, "", false
		}
		// Sim availability gate: reject if the hotel is marked sold-out.
		if !st.simIsAvailable(li.HotelID) {
			return 0, li.HotelID, true // ok=true (hotel exists), but unavailableID set
		}
		// WALLET-003: cap nights and overflow-safe multiply.
		n := int64(nights(li.Checkin, li.Checkout))
		if n > maxNights {
			return 0, "", false
		}
		linePrice, ok2 := checkedMul(h.PriceCentsNight, n)
		if !ok2 {
			return 0, "", false
		}
		newTotal, ok3 := checkedAdd(total, linePrice)
		if !ok3 {
			return 0, "", false
		}
		total = newTotal
	}
	return total, "", true
}

// checkoutTool serves create/update/complete_checkout. complete_checkout is the
// enforcement core (§7.4, §8.4): merchant hard-max + idempotency always enforced;
// L2 requires interactive buyer_consent; L3 requires a verified ap2_mandate. The
// agent overrides none of it. Sessions are owned by the creating agent identity.
//
// SEV-1b fix: create_checkout now accepts an optional user_budget_cents from the
// caller.  complete_checkout enforces min(user_budget_cents, BudgetHardMaxCents)
// instead of the static BudgetCeilingCents, so the merchant veto actually
// reflects the user's per-trip budget rather than a house static cap.
//
// SEV-1a fix: complete_checkout accepts an optional idempotency_key.  When a
// key is re-presented after a successful complete, the same booking_ref is
// returned without creating a second booking.
func checkoutTool(cfg config, st *store, tier, agentID, name string, args json.RawMessage) (map[string]any, int) {
	var a struct {
		Checkout struct {
			ID              string      `json:"id"`
			UserID          string      `json:"user_id"`
			LineItems       []lineItem  `json:"line_items"`
			UserBudgetCents int64       `json:"user_budget_cents"` // SEV-1b: caller's per-trip budget
			BuyerConsent    bool        `json:"buyer_consent"`
			AP2Mandate      *ap2Mandate `json:"ap2_mandate"`
			IdempotencyKey  string      `json:"idempotency_key"` // SEV-1a: client-supplied key
			// SIMULATED prepaid wallet (additive). When set on create_checkout the
			// session binds to this wallet so complete debits / cancel credits it.
			WalletSessionID    string `json:"wallet_session_id"`
			WalletBalanceCents int64  `json:"wallet_balance_cents"`
			// SettlementRail opts a checkout into a REAL settlement rail at
			// complete_checkout — currently "circle_usdc" (see circle_usdc.go).
			// Additive: "" (the default) skips this entirely.
			SettlementRail string `json:"settlement_rail"`
		} `json:"checkout"`
	}
	_ = json.Unmarshal(args, &a)
	c := a.Checkout

	switch name {
	case "create_checkout":
		if len(c.LineItems) == 0 {
			return map[string]any{"status": "error", "reason": "invalid_line_items"}, http.StatusBadRequest
		}
		// WALLET-003 — reject out-of-range qty/adults BEFORE any arithmetic to
		// prevent integer-overflow attacks on the total computation. Negative Qty
		// is invalid; Qty > maxQty is adversarially large. Negative Adults is
		// invalid (capacity gate compares signed ints). Zero qty → qtyOf() returns
		// 1, which is safe and the intended "single unit" semantic.
		for _, li := range c.LineItems {
			if li.Qty < 0 || li.Qty > maxQty {
				return map[string]any{"status": "error", "reason": "invalid_quantity",
					"max_qty": maxQty}, http.StatusBadRequest
			}
			if li.Adults < 0 {
				return map[string]any{"status": "error", "reason": "invalid_adults"}, http.StatusBadRequest
			}
		}
		// L3 sim: reject if any line item references a sold-out hotel.
		// This makes the availability gate visible to agents at checkout time.
		total, unavailID, ok := lineItemsTotalCentsChecked(st, c.LineItems)
		if !ok {
			return map[string]any{"status": "error", "reason": "invalid_line_items"}, http.StatusBadRequest
		}
		if unavailID != "" {
			return map[string]any{
				"status":   "unavailable",
				"reason":   "hotel_sold_out",
				"hotel_id": unavailID,
			}, http.StatusConflict
		}
		// SEV-2: Capacity check — reject if adults exceeds hotel MaxOccupancy.
		// Per-adult pricing deferred; per-night pricing unchanged (see SPEC-NOTES.md).
		for _, li := range c.LineItems {
			// #44 — capacity is a HOTEL-only concept; food lines skip it.
			if li.HotelID != "" && li.Adults > 0 {
				h, ok2 := findHotel(li.HotelID)
				if ok2 && h.MaxOccupancy > 0 && li.Adults > h.MaxOccupancy {
					return map[string]any{
						"status":        "error",
						"reason":        "exceeds_room_capacity",
						"hotel_id":      li.HotelID,
						"adults":        li.Adults,
						"max_occupancy": h.MaxOccupancy,
					}, http.StatusBadRequest
				}
			}
		}
		s := &checkoutSession{
			ID: "co_" + randHex(8), UserID: c.UserID, AgentID: agentID, LineItems: c.LineItems,
			TotalCents: total, UserBudgetCents: c.UserBudgetCents, Currency: "USD", Status: "incomplete",
			// SIMULATED prepaid wallet binding — persisted so complete/cancel know
			// which wallet to draw down / refund. Empty → no wallet logic (back-compat).
			WalletSessionID: c.WalletSessionID,
			SettlementRail:  c.SettlementRail,
		}
		st.mu.Lock()
		st.sessions[s.ID] = s
		st.mu.Unlock()
		return sessionView(s), http.StatusOK

	case "update_checkout":
		st.mu.Lock()
		defer st.mu.Unlock()
		s, ok := st.sessions[c.ID]
		if !ok {
			return map[string]any{"status": "error", "reason": "checkout_not_found"}, http.StatusNotFound
		}
		if s.AgentID != agentID {
			return map[string]any{"status": "error", "reason": "not_session_owner"}, http.StatusForbidden
		}
		// END-USER OWNERSHIP (defense-in-depth). The AgentID check above only isolates
		// different UCP agent COMPANIES — every end user of THIS orchestrator shares one
		// process-wide signing key, so AgentID is IDENTICAL across them and cannot tell
		// user A from user B. This binds the checkout to the end-user id the orchestrator
		// already verified (session_token/owner_token in server.py _authorize_trip_action),
		// so one end user cannot advance another's checkout if the merchant is ever
		// exposed off-loopback or a caller forwards a reconstructible checkout id.
		// Prod-gated (RequireSignatures) so the unsigned demo/CI path is byte-identical;
		// unowned sessions (UserID=="") are the documented anon-legacy fail-open.
		if cfg.RequireSignatures && s.UserID != "" && s.UserID != c.UserID {
			return map[string]any{"status": "error", "reason": "not_session_owner"}, http.StatusForbidden
		}
		if c.BuyerConsent {
			s.BuyerConsent = true
		}
		return sessionView(s), http.StatusOK

	case "complete_checkout":
		st.mu.Lock()
		defer st.mu.Unlock()

		// SEV-1a: idempotency_key — if we've already completed a session for this
		// key, return the same booking_ref without a second booking.
		if c.IdempotencyKey != "" {
			if prevID, seen := st.idempotencyMap[c.IdempotencyKey]; seen {
				if prev, ok2 := st.sessions[prevID]; ok2 && prev.Status == "complete" {
					// #1 OWNERSHIP (HIGH): only the SAME authenticated agent that
					// booked this session may replay its idempotency_key. Without this
					// guard the short-circuit returns BEFORE the ownership check on the
					// normal path below, so a different agent replaying a guessed/
					// observed key is handed back another party's booking_ref — a
					// cross-agent leak + consent bypass on the money path. NOTE: we do
					// NOT bind to the checkout id — the legitimate retry-after-timeout
					// pattern reuses the key against a NEW checkout (that is the point
					// of idempotency). (Unsigned-demo agents both have agentID=="" so
					// the demo path still matches — no regression.)
					// END-USER OWNERSHIP extension of the #1 OWNERSHIP guard above: a
					// prod (RequireSignatures) replay of a guessed/observed
					// idempotency_key must not hand back another end user's
					// booking_ref even when it comes from the SAME agent (every end
					// user of this orchestrator shares one process-wide AgentID).
					if prev.AgentID == agentID &&
						!(cfg.RequireSignatures && prev.UserID != "" && prev.UserID != c.UserID) {
						v := sessionView(prev)
						v["idempotent"] = true
						// Re-attach the CURRENT wallet balance WITHOUT re-debiting
						// (the booking was already drawn down on the first commit).
						if prev.WalletSessionID != "" {
							if wlt, ok3 := st.walletGetLocked(prev.WalletSessionID); ok3 {
								v["wallet_session_id"] = prev.WalletSessionID
								v["wallet_balance_cents"] = wlt.BalanceCents
								v["simulated"] = true
								v["wallet_note"] = walletSimNote
							}
						}
						// Signal the settlement rail so the caller (dispatchTool, AFTER
						// releasing st.mu) re-confirms/re-reports it — circleSettle is
						// itself idempotent per booking_ref, so this never double-settles.
						if prev.SettlementRail != "" {
							v["settlement_rail"] = prev.SettlementRail
						}
						return v, http.StatusOK
					}
				}
			}
		}

		s, ok := st.sessions[c.ID]
		if !ok {
			return map[string]any{"status": "error", "reason": "checkout_not_found"}, http.StatusNotFound
		}
		if s.AgentID != agentID {
			return map[string]any{"status": "error", "reason": "not_session_owner"}, http.StatusForbidden
		}
		// END-USER OWNERSHIP (defense-in-depth) — see update_checkout above for the
		// full rationale. Prod-gated; unowned sessions (UserID=="") fail open.
		if cfg.RequireSignatures && s.UserID != "" && s.UserID != c.UserID {
			return map[string]any{"status": "error", "reason": "not_session_owner"}, http.StatusForbidden
		}
		// A cancelled (voided) checkout can NEVER be re-completed. The booking it
		// once held has been released; re-completing would resurrect a superseded
		// itinerary and break the exactly-one-live-booking invariant (cascade §12.1).
		if s.Status == "cancelled" {
			return map[string]any{"status": "error", "reason": "checkout_cancelled", "id": s.ID}, http.StatusConflict
		}
		if s.Status == "complete" {
			v := sessionView(s)
			v["idempotent"] = true
			// Re-attach the CURRENT wallet balance WITHOUT re-debiting (already
			// drawn down when this session first committed).
			if s.WalletSessionID != "" {
				if wlt, ok3 := st.walletGetLocked(s.WalletSessionID); ok3 {
					v["wallet_session_id"] = s.WalletSessionID
					v["wallet_balance_cents"] = wlt.BalanceCents
					v["simulated"] = true
					v["wallet_note"] = walletSimNote
				}
			}
			return v, http.StatusOK
		}
		// N1 COUNTERPARTY INSOLVENCY (sim.go): if any line item references a
		// counterparty (counterparty_id == catalog_id == hotel_id, §19.1) that the
		// World-Simulator has flipped INSOLVENT, the commit goes VOID — there is no
		// counterparty to book against (an insolvent supplier's paper is worthless).
		// Distinct from sold-out (substitutable) and flight-cancel (re-routable):
		// the ticket is stranded. This is the fault the Fraud gate pre-empts.
		for _, li := range s.LineItems {
			// NOTE: we already hold st.mu (locked at the top of complete_checkout);
			// use the Locked variant — re-locking would deadlock (non-reentrant).
			// #44 — idFor(li) resolves to FoodID for food lines, HotelID otherwise,
			// so the N1 insolvency gate works for either kind without forking.
			cpID := idFor(li)
			if !st.simCounterpartySolventLocked(cpID) {
				return map[string]any{
					"status":          "void",
					"reason":          "counterparty_insolvent",
					"id":              s.ID,
					"counterparty_id": cpID,
					"message":         "counterparty ceased operations (insolvent) — booking VOID, no counterparty to rebook against (N1)",
				}, http.StatusConflict
			}
		}
		liveCents, ok := lineItemsTotalCents(s.LineItems)
		if !ok {
			return map[string]any{"status": "error", "reason": "item_unavailable"}, http.StatusConflict
		}
		s.TotalCents = liveCents

		// SIMULATED prepaid wallet — INSUFFICIENT-FUNDS gate (additive; HTTP 402).
		// Fires AFTER the N1 insolvency gate + liveCents recompute and BEFORE the
		// existing 403 budget vetoes (hard-max + L2/L3 budget), which run UNCHANGED.
		// Distinct from the 403 price_exceeds_budget veto: 402 means the trip is
		// WITHIN the user's budget but EXCEEDS the funded wallet balance.
		// Pure READ (walletGetLocked) BEFORE any state mutation — a rejected commit
		// leaves wallet + session untouched. Skipped when WalletSessionID==""
		// (backward-compat: byte-identical).
		//
		// WALLET-002 fix: when WalletSessionID is set but the wallet is NOT found,
		// FAIL CLOSED (402 wallet_not_found) instead of falling through silently.
		// WALLET-001 fix: when RequireSignatures (prod) the wallet is bound to the
		// agent that funded it; a different agent is 403 wallet_not_owner.
		if s.WalletSessionID != "" {
			wlt, ok2 := st.walletGetLocked(s.WalletSessionID)
			if !ok2 {
				// WALLET-002: wallet bound but missing → FAIL CLOSED.
				return map[string]any{
					"status": "denied", "reason": "wallet_not_found",
					"id": s.ID, "wallet_session_id": s.WalletSessionID,
				}, http.StatusPaymentRequired
			}
			// WALLET-001: ownership check — prod only (RequireSignatures gate).
			// Unsigned demo (agentID=="" and owner=="") satisfies the equality.
			if cfg.RequireSignatures && wlt.OwnerAgentID != "" && wlt.OwnerAgentID != agentID {
				return map[string]any{
					"status": "denied", "reason": "wallet_not_owner",
					"id": s.ID, "wallet_session_id": s.WalletSessionID,
				}, http.StatusForbidden
			}
			// H1 fix (end-user ownership, cross-user fund-drain): the AgentID check
			// above only isolates different UCP agent COMPANIES — every end user of
			// THIS orchestrator shares the SAME process-wide AgentID, so AgentID
			// alone cannot tell victim user A's wallet from attacker user B's. Without
			// this, user B could bind their OWN checkout session (create_checkout has
			// no cross-user check at bind time) to victim user A's wallet_session_id
			// and drain A's funded balance. Mirrors the read-path check already
			// correct at wallet.go (walletGetChecked / dispatchWalletGet) and the
			// AgentID check just above. Prod-gated; unowned wallets (OwnerUserID=="")
			// or unowned sessions (s.UserID=="") fail open, matching every other
			// END-USER OWNERSHIP check in this file.
			//
			// TRUST BOUNDARY (security review, 2026-07-04, informational/non-blocking): this
			// fail-open is only safe because the Python orchestrator is REQUIRED to
			// inject a session-token-verified, non-empty UserID on BOTH the
			// wallet-funding call (walletFund's ownerUserID) and the completing
			// session (this s.UserID) before forwarding to this Go merchant (#161).
			// If a caller ever reaches this endpoint with a client-controlled empty
			// UserID against a victim wallet that DOES have an owner, this gate is
			// bypassed by construction — it cannot distinguish "legitimately
			// anonymous" from "attacker stripped the field" on its own; closing that
			// gap is an upstream orchestrator responsibility, not a local fix here.
			// Mirrored primitive-level trust boundary: wallet.go's walletDebitLocked.
			if cfg.RequireSignatures && s.UserID != "" && wlt.OwnerUserID != "" && wlt.OwnerUserID != s.UserID {
				return map[string]any{
					"status": "denied", "reason": "wallet_not_owner",
					"id": s.ID, "wallet_session_id": s.WalletSessionID,
				}, http.StatusForbidden
			}
			// WALLET-OWNERSHIP-EMPTY-OWNER-BYPASS fix: when RequireSignatures is on
			// and the wallet has no recorded owner, only an unsigned caller
			// (agentID=="") may use it. A signed caller claiming an ownerless wallet
			// is rejected — otherwise any authenticated agent could drain wallets
			// that were funded without an ownerID.
			if cfg.RequireSignatures && wlt.OwnerAgentID == "" && agentID != "" {
				return map[string]any{
					"status": "denied", "reason": "wallet_no_owner_signed_caller",
					"id": s.ID, "wallet_session_id": s.WalletSessionID,
				}, http.StatusForbidden
			}
			if liveCents > wlt.BalanceCents {
				return map[string]any{
					"status": "denied", "reason": "insufficient_funds", "id": s.ID,
					"total_cents":          liveCents,
					"wallet_balance_cents": wlt.BalanceCents,
					"wallet_session_id":    s.WalletSessionID,
					"currency":             "USD",
				}, http.StatusPaymentRequired
			}
		}

		// Merchant absolute ceiling — always enforced, every tier.
		if liveCents > cfg.BudgetHardMaxCents {
			return map[string]any{"status": "denied", "reason": "price_exceeds_hard_max", "id": s.ID,
				"total_cents": liveCents, "hard_max_cents": cfg.BudgetHardMaxCents, "currency": "USD"}, http.StatusForbidden
		}

		if tier == "L3" {
			if c.AP2Mandate == nil {
				return map[string]any{"status": "requires_mandate", "id": s.ID,
					"message": "L3 autonomous checkout requires a signed ap2_mandate"}, http.StatusOK
			}
			// C1 fix: read the device key under the lock we already hold (no re-lock).
			userPub := st.userKeys[s.UserID]
			if err := verifyMandate(*c.AP2Mandate, userPub, s.ID, s.Currency, liveCents, time.Now()); err != nil {
				return map[string]any{"status": "denied", "reason": err.Error(), "id": s.ID,
					"total_cents": liveCents, "currency": "USD"}, http.StatusForbidden
			}
		} else {
			// VULN-003 fix: consent MUST come ONLY from a prior update_checkout
			// (server-side state). The completing caller's own body is NEVER
			// trusted — an agent could bypass the HITL gate by setting
			// buyer_consent:true in its own complete_checkout payload. We ignore
			// c.BuyerConsent entirely; only s.BuyerConsent (set by update_checkout,
			// which is owner-checked) is authoritative.
			if !s.BuyerConsent {
				return map[string]any{"status": "requires_consent", "id": s.ID,
					"message": "buyer_consent required before complete_checkout (HITL gate) — call update_checkout first"}, http.StatusOK
			}
			// SEV-1b: enforce the user's per-trip budget, not just the house cap.
			// The binding ceiling is min(user_budget, BudgetHardMaxCents).
			// BudgetCeilingCents remains a fallback when no user budget is supplied.
			// When a user budget is supplied it IS the binding ceiling (the
			// caller's per-trip budget), bounded above by the house hard-max.
			// BudgetCeilingCents is only the fallback when no user budget is given.
			// (Prior bug: an `&& user_budget < ceiling` guard let the user budget
			// only LOWER the $800 default, never raise it — so every trip over
			// $800 was wrongly capped at the house default.)
			ceiling := cfg.BudgetCeilingCents
			if s.UserBudgetCents > 0 {
				ceiling = s.UserBudgetCents
			}
			if ceiling > cfg.BudgetHardMaxCents {
				ceiling = cfg.BudgetHardMaxCents
			}
			if liveCents > ceiling {
				return map[string]any{"status": "denied", "reason": "price_exceeds_budget", "id": s.ID,
					"total_cents": liveCents, "budget_ceiling_cents": ceiling, "currency": "USD"}, http.StatusForbidden
			}
		}

		s.Status = "complete"
		s.BookingRef = "BK-" + randHex(6)
		// SEV-1a: record the idempotency_key → session ID mapping so repeat
		// complete_checkout calls with the same key return the same booking.
		if c.IdempotencyKey != "" {
			st.idempotencyMap[c.IdempotencyKey] = s.ID
		}
		v := sessionView(s)
		// SIMULATED prepaid wallet DEBIT — draw the committed total down, under the
		// st.mu we already hold (Locked variant — NEVER re-lock; re-locking would
		// deadlock). The 402 gate above guarantees the wallet exists and has
		// sufficient funds, so this debit succeeds. The seenCheckout guard makes it
		// a no-op on idempotent replay. Additive: WalletSessionID=="" →
		// response byte-identical to before.
		// WALLET-002 defensive check: ok3 false here is theoretically impossible
		// (the gate already ran), but we fail hard rather than book without debit.
		// H1: pass s.UserID as the expected owner so the debit PRIMITIVE itself
		// re-asserts end-user ownership (defense-in-depth alongside the gate above).
		if s.WalletSessionID != "" {
			bal, ok3, reason3 := st.walletDebitLocked(s.WalletSessionID, s.ID, s.BookingRef, s.TotalCents, s.UserID)
			if !ok3 && reason3 != "already_debited" {
				// Defensive path: gate should have caught this. Return 500 rather
				// than silently booking without drawing down the wallet.
				return map[string]any{
					"status": "error", "reason": "wallet_debit_failed",
					"detail": reason3, "id": s.ID,
				}, http.StatusInternalServerError
			}
			v["wallet_session_id"] = s.WalletSessionID
			v["wallet_debit_cents"] = s.TotalCents
			v["wallet_balance_cents"] = bal
			v["simulated"] = true
			v["wallet_note"] = walletSimNote
		}
		// Signal a REAL settlement rail for the caller to trigger AFTER st.mu is
		// released — see the SettlementRail field comment and maybeCircleSettle
		// in circle_usdc.go. Additive: SettlementRail=="" → unchanged.
		if s.SettlementRail != "" {
			v["settlement_rail"] = s.SettlementRail
		}
		return v, http.StatusOK

	case "cancel_checkout":
		// VOID a checkout — release the booking it holds so it no longer "stands"
		// (§12.1 cascade: the superseded recovery booking is voided when the next
		// cycle commits, keeping exactly one itinerary live — no double-charge).
		//
		// Idempotent and safe by design:
		//   - unknown / never-created checkout id  → cancelled:true (safe success)
		//   - already-cancelled                    → cancelled:true (safe success)
		//   - completed                            → transition to "cancelled",
		//                                            drop the booking_ref (released)
		//   - incomplete                           → transition to "cancelled"
		// A cancelled checkout can never be re-completed (guarded above).
		st.mu.Lock()
		defer st.mu.Unlock()
		s, ok := st.sessions[c.ID]
		if !ok {
			// No such session — nothing to release. Idempotent safe success so
			// the caller never has to special-case a missing/superseded id.
			return map[string]any{"status": "cancelled", "id": c.ID, "cancelled": true,
				"already": "unknown"}, http.StatusOK
		}
		if s.AgentID != agentID {
			return map[string]any{"status": "error", "reason": "not_session_owner"}, http.StatusForbidden
		}
		// END-USER OWNERSHIP (defense-in-depth) — see update_checkout above for the
		// full rationale. Prod-gated; unowned sessions (UserID=="") fail open.
		if cfg.RequireSignatures && s.UserID != "" && s.UserID != c.UserID {
			return map[string]any{"status": "error", "reason": "not_session_owner"}, http.StatusForbidden
		}
		if s.Status == "cancelled" {
			return map[string]any{"status": "cancelled", "id": s.ID, "cancelled": true,
				"already": "cancelled", "currency": s.Currency}, http.StatusOK
		}
		releasedRef := s.BookingRef
		wasCompleted := s.Status == "complete"
		s.Status = "cancelled"
		s.BookingRef = "" // release the booking — it no longer stands
		resp := map[string]any{"status": "cancelled", "id": s.ID, "cancelled": true,
			"released_booking_ref": releasedRef, "total_cents": s.TotalCents,
			"currency": s.Currency}
		// SIMULATED prepaid wallet CREDIT (void → refund) — ONLY on the genuine
		// completed→cancelled transition (a previously-debited booking). Under the
		// st.mu we already hold (Locked variant — NEVER re-lock). The credit only
		// matches an un-credited debit, so a double-cancel never double-refunds.
		// Additive: WalletSessionID=="" → response byte-identical to before.
		if wasCompleted && s.WalletSessionID != "" {
			if bal, ok3 := st.walletCreditLocked(s.WalletSessionID, s.ID, releasedRef, s.TotalCents); ok3 {
				resp["wallet_session_id"] = s.WalletSessionID
				resp["wallet_credit_cents"] = s.TotalCents
				resp["wallet_balance_cents"] = bal
				resp["simulated"] = true
				resp["wallet_note"] = walletSimNote
			}
		}
		// HONESTY: unlike the simulated wallet above, a completed Circle
		// settlement is a REAL on-chain transfer — cancel_checkout has no
		// reversal/refund mechanism for it (that would require its own
		// outbound Circle transfer, not built). Say so explicitly rather than
		// silently cancelling the booking while real funds stay moved. This
		// also means a fresh complete_checkout on a NEW booking_ref for the
		// same itinerary triggers an independent, separately-real transfer.
		if wasCompleted && s.SettlementRail == "circle_usdc" {
			resp["circle_settlement_not_reversed"] = true
			resp["circle_settlement_note"] = "this booking had a REAL circle_usdc settlement; " +
				"cancelling the booking does not reverse or refund that on-chain transfer"
		}
		return resp, http.StatusOK
	}
	return map[string]any{"error": "unknown checkout tool: " + name}, http.StatusBadRequest
}

func sessionView(s *checkoutSession) map[string]any {
	v := map[string]any{
		"id": s.ID, "status": s.Status, "user_id": s.UserID,
		"line_items": s.LineItems, "total_cents": s.TotalCents, "currency": s.Currency,
		"buyer_consent": s.BuyerConsent, "booking_ref": s.BookingRef,
	}
	if s.UserBudgetCents > 0 {
		v["user_budget_cents"] = s.UserBudgetCents
	}
	return v
}

func randHex(n int) string {
	b := make([]byte, n)
	_, _ = rand.Read(b)
	return hex.EncodeToString(b)
}
