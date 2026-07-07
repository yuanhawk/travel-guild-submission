package main

import (
	"encoding/json"
	"io"
	"net/http"
	"strings"
)

type mcpRequest struct {
	JSONRPC string          `json:"jsonrpc"`
	ID      json.RawMessage `json:"id"`
	Method  string          `json:"method"`
	Params  json.RawMessage `json:"params"`
}

type toolCallParams struct {
	Name      string          `json:"name"`
	Arguments json.RawMessage `json:"arguments"`
}

func lowerHeaders(h http.Header) map[string]string {
	out := map[string]string{}
	for k, v := range h {
		if len(v) > 0 {
			out[strings.ToLower(k)] = v[0]
		}
	}
	return out
}

// parseRequestedTier reads the client's requested autonomy tier (an upper bound;
// the effective tier is min(requested, granted)).
func parseRequestedTier(args json.RawMessage, def string) string {
	var m struct {
		Meta struct {
			AutonomyLevel string `json:"autonomy_level"`
		} `json:"meta"`
	}
	_ = json.Unmarshal(args, &m)
	if _, ok := tierCaps[m.Meta.AutonomyLevel]; ok {
		return m.Meta.AutonomyLevel
	}
	return def
}

// POST /api/ucp/mcp — MCP JSON-RPC 2.0 dispatcher with RFC 9421 verification
// (present-but-invalid always rejected; absent rejected only when required) and
// identity-bound tier gating.
func mcpHandler(cfg config, st *store, v *sigVerifier) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodPost {
			writeJSON(w, http.StatusMethodNotAllowed, mcpErr(nil, -32600, "POST required"))
			return
		}
		body, _ := io.ReadAll(io.LimitReader(r.Body, 1<<20))

		present, id, sigErr := v.verify(r.Method, r.Host, r.URL.Path, r.URL.RawQuery, lowerHeaders(r.Header), body)
		if present && sigErr != nil {
			writeJSON(w, http.StatusUnauthorized, mcpErr(nil, -32001, "signature rejected: "+sigErr.Error()))
			return
		}
		if cfg.RequireSignatures && !present {
			writeJSON(w, http.StatusUnauthorized, mcpErr(nil, -32001, "signature required"))
			return
		}

		var req mcpRequest
		if err := json.Unmarshal(body, &req); err != nil {
			writeJSON(w, http.StatusBadRequest, mcpErr(nil, -32700, "parse error"))
			return
		}
		switch req.Method {
		case "tools/list":
			writeJSON(w, http.StatusOK, mcpResult(req.ID, map[string]any{"tools": toolSchemas()}))
		case "tools/call":
			var p toolCallParams
			_ = json.Unmarshal(req.Params, &p)

			// Effective tier = min(client-requested, server-side grant for the
			// authenticated identity). Unsigned/unknown identity -> L1.
			requested := parseRequestedTier(p.Arguments, cfg.DefaultTier)
			tier := minTier(requested, grantedTier(cfg.TierGrants, id, cfg.UnsignedTier))
			var agentCaps []string
			agentID := ""
			if id != nil {
				agentCaps = id.Caps
				agentID = id.ProfileURL
			}
			active := negotiate(agentCaps, tier)

			if rc := toolCapability(p.Name); rc != "" && !active[rc] {
				writeJSON(w, http.StatusForbidden, mcpResult(req.ID, wrapResult(map[string]any{
					"status": "denied", "reason": "capability_not_active",
					"capability": rc, "autonomy_tier": tier,
				}, active)))
				return
			}
			domain, code := dispatchTool(cfg, st, tier, agentID, p)
			writeJSON(w, code, mcpResult(req.ID, wrapResult(domain, active)))
		default:
			writeJSON(w, http.StatusBadRequest, mcpErr(req.ID, -32601, "method not found"))
		}
	}
}

// sim_set_availability MCP method (L3 World Simulator control).
// Allows external callers (e.g. the society orchestrator, test harness) to
// mark a hotel sold-out via the MCP JSON-RPC surface.
func dispatchSimTool(st *store, p toolCallParams) (map[string]any, int) {
	var a struct {
		HotelID   string `json:"hotel_id"`
		Available bool   `json:"available"`
	}
	// Default available=true (absent = restore availability)
	type simArgs struct {
		HotelID   string `json:"hotel_id"`
		Available *bool  `json:"available"`
	}
	var sa simArgs
	_ = json.Unmarshal(p.Arguments, &sa)
	if sa.HotelID == "" {
		return map[string]any{"error": "hotel_id required"}, http.StatusBadRequest
	}
	if _, ok := findHotel(sa.HotelID); !ok {
		return map[string]any{"error": "hotel_not_found", "hotel_id": sa.HotelID}, http.StatusNotFound
	}
	a.HotelID = sa.HotelID
	a.Available = true
	if sa.Available != nil {
		a.Available = *sa.Available
	}
	st.simSetAvailability(a.HotelID, a.Available)
	return map[string]any{
		"hotel_id":  a.HotelID,
		"available": a.Available,
		"status":    "ok",
	}, http.StatusOK
}

// dispatchSimTransferTool serves sim_set_transfer (cascade flight-cancel control).
func dispatchSimTransferTool(st *store, p toolCallParams) (map[string]any, int) {
	type simArgs struct {
		FromCity string `json:"from_city"`
		ToCity   string `json:"to_city"`
		Feasible *bool  `json:"feasible"`
	}
	var sa simArgs
	_ = json.Unmarshal(p.Arguments, &sa)
	if sa.FromCity == "" || sa.ToCity == "" {
		return map[string]any{"error": "from_city and to_city required"}, http.StatusBadRequest
	}
	feasible := true
	if sa.Feasible != nil {
		feasible = *sa.Feasible
	}
	st.simSetTransfer(sa.FromCity, sa.ToCity, feasible)
	return map[string]any{
		"from_city": sa.FromCity,
		"to_city":   sa.ToCity,
		"feasible":  feasible,
		"status":    "ok",
	}, http.StatusOK
}

// dispatchSimCounterpartyTool serves sim_set_counterparty (N1 insolvency control).
func dispatchSimCounterpartyTool(st *store, p toolCallParams) (map[string]any, int) {
	type simArgs struct {
		CounterpartyID string `json:"counterparty_id"`
		Solvent        *bool  `json:"solvent"`
	}
	var sa simArgs
	_ = json.Unmarshal(p.Arguments, &sa)
	if sa.CounterpartyID == "" {
		return map[string]any{"error": "counterparty_id required"}, http.StatusBadRequest
	}
	solvent := true
	if sa.Solvent != nil {
		solvent = *sa.Solvent
	}
	st.simSetCounterparty(sa.CounterpartyID, solvent)
	return map[string]any{
		"counterparty_id": sa.CounterpartyID,
		"solvent":         solvent,
		"status":          "ok",
	}, http.StatusOK
}

// catalogKindIsFood reports whether a catalog tool call targets the FOOD_DELIVERY
// kind. The kind may be declared on the query (search_catalog) or product
// (lookup_catalog). Absent/anything-else → HOTEL (the legacy implicit kind), so
// existing hotel callers are unaffected.
func catalogKindIsFood(args json.RawMessage) bool {
	var m struct {
		Kind    string `json:"kind"`
		Query   struct {
			Kind string `json:"kind"`
		} `json:"query"`
		Product struct {
			Kind string `json:"kind"`
		} `json:"product"`
	}
	_ = json.Unmarshal(args, &m)
	food := string(KindFoodDelivery)
	return m.Kind == food || m.Query.Kind == food || m.Product.Kind == food
}

func dispatchTool(cfg config, st *store, tier, agentID string, p toolCallParams) (map[string]any, int) {
	switch p.Name {
	case "search_catalog", "lookup_catalog":
		// #44 — the catalog now serves TWO kinds. Route to the SIMULATED
		// food-delivery catalog when the query declares kind=="FOOD_DELIVERY";
		// otherwise the HOTEL path is unchanged (sim-availability gated).
		if catalogKindIsFood(p.Arguments) {
			return foodCatalogTool(p.Name, p.Arguments)
		}
		// Pass st so the sim availability state gates catalog results.
		return catalogToolWithStore(st, p.Name, p.Arguments)
	case "create_checkout", "update_checkout", "complete_checkout", "cancel_checkout":
		return checkoutTool(cfg, st, tier, agentID, p.Name, p.Arguments)
	case "sim_set_availability":
		// L3 World Simulator control: mark hotel available/unavailable.
		return dispatchSimTool(st, p)
	case "sim_set_transfer":
		// Cascade fault control: cancel/restore an inter-leg flight/transfer.
		return dispatchSimTransferTool(st, p)
	case "sim_get_transfers":
		// Read the cancelled-transfer set as authoritative data (Transport agent).
		return map[string]any{"cancelled_transfers": st.simListCancelledTransfers()}, http.StatusOK
	case "sim_set_counterparty":
		// N1 fault control: mark a counterparty solvent/insolvent. Insolvent →
		// complete_checkout VOIDs any booking against it.
		return dispatchSimCounterpartyTool(st, p)
	case "sim_get_counterparties":
		// Read the insolvent-counterparty set as authoritative data.
		return map[string]any{"insolvent_counterparties": st.simListInsolventCounterparties()}, http.StatusOK
	case "wallet_fund":
		// SIMULATED prepaid wallet (dev.ucp.sim.control capability required):
		// create-OR-RESET a wallet for {wallet_session_id} at {wallet_balance_cents}.
		// agentID passed for WALLET-001 owner binding.
		return dispatchWalletFund(st, agentID, p)
	case "wallet_get":
		// SIMULATED prepaid wallet: read the balance + ledger for a session.
		// cfg+agentID passed for WALLET-001 ownership check in prod.
		return dispatchWalletGet(st, cfg, agentID, p)
	default:
		return map[string]any{"error": "unknown tool: " + p.Name}, http.StatusBadRequest
	}
}

func wrapResult(domain map[string]any, active map[string]bool) map[string]any {
	sc := map[string]any{}
	for k, val := range domain {
		sc[k] = val
	}
	sc["ucp"] = map[string]any{"version": ucpVersion, "capabilities": activeCapManifest(active)}
	blob, _ := json.Marshal(sc)
	return map[string]any{
		"structuredContent": sc,
		"content":           []map[string]any{{"type": "text", "text": string(blob)}},
	}
}

func mcpResult(id json.RawMessage, result any) map[string]any {
	return map[string]any{"jsonrpc": "2.0", "id": rawOrNull(id), "result": result}
}

func mcpErr(id json.RawMessage, code int, msg string) map[string]any {
	return map[string]any{"jsonrpc": "2.0", "id": rawOrNull(id), "error": map[string]any{"code": code, "message": msg}}
}

func rawOrNull(id json.RawMessage) any {
	if len(id) == 0 {
		return nil
	}
	return id
}

func toolSchemas() []map[string]any {
	str := map[string]any{"type": "string"}
	intt := map[string]any{"type": "integer"}
	boolt := map[string]any{"type": "boolean"}
	obj := func(props map[string]any, required ...string) map[string]any {
		return map[string]any{"type": "object", "properties": props, "required": required}
	}
	meta := map[string]any{"type": "object", "description": "UCP identity + requested tier: {ucp-agent:{profile}, autonomy_level: L1|L2|L3} (effective tier = min(requested, server grant))"}
	// #44 — line items now carry an optional food_id/qty (FOOD_DELIVERY kind) in
	// addition to the hotel_id/checkin/checkout/adults (HOTEL kind). Exactly one
	// of hotel_id or food_id is set per line; nothing is required so a food line
	// (food_id only) and a hotel line (hotel_id+dates) both validate.
	lineItems := map[string]any{"type": "array", "items": obj(map[string]any{
		"hotel_id": str, "checkin": str, "checkout": str, "adults": intt,
		"food_id": str, "qty": intt,
	})}
	mandate := map[string]any{"type": "object", "description": "ap2_mandate (L3): user-signed budget/expiry consent (cents)",
		"properties": map[string]any{"user_id": str, "checkout_id": str, "budget_ceiling_cents": intt, "currency": str, "valid_until": str, "signature": str}}

	return []map[string]any{
		// SIMULATED prepaid wallet (sim/control — not a commerce tool; no cap guard).
		{"name": "wallet_fund",
			"description": "SIMULATED prepaid wallet: create-OR-RESET the wallet for wallet_session_id with balance wallet_balance_cents (settlement-layer state, honestly labeled simulated:true). var-0 keystone — keyed by the deterministic per-run digest and reset-per-run by overwrite, so an identical input replays byte-identically. complete_checkout debits this wallet; cancel_checkout credits it. Optional user_id binds end-user ownership (task #161; prod-enforced on wallet_get). Demo/test only.",
			"inputSchema": obj(map[string]any{
				"wallet_session_id":    str,
				"wallet_balance_cents": intt,
				"user_id":              str,
			}, "wallet_session_id", "wallet_balance_cents")},
		{"name": "wallet_get",
			"description": "SIMULATED prepaid wallet: read the current balance + ledger for wallet_session_id ({seed_cents, balance_cents, ledger[], simulated:true}). Read-only. Optional user_id is checked against the wallet's end-user owner in prod (task #161).",
			"inputSchema": obj(map[string]any{
				"wallet_session_id": str,
				"user_id":           str,
			}, "wallet_session_id")},
		// L3 World Simulator control — not a commerce tool; no cap guard.
		{"name": "sim_set_availability",
			"description": "L3 World Simulator: mark a hotel available (true) or sold-out (false). Affects search_catalog (exclusion), lookup_catalog (status:unavailable), and create_checkout (rejection). Demo/test only.",
			"inputSchema": obj(map[string]any{
				"hotel_id":  str,
				"available": boolt,
			}, "hotel_id", "available")},
		// Cascade fault control — cancel/restore an inter-leg flight or transfer.
		{"name": "sim_set_transfer",
			"description": "World Simulator (cascade): cancel (feasible=false) or restore (true) an inter-leg flight/transfer edge from_city→to_city. Directional. Read by the Transport agent (sim_get_transfers) which re-routes around it. Distinct from sim_set_availability (which gates hotels). Demo/test only.",
			"inputSchema": obj(map[string]any{
				"from_city": str,
				"to_city":   str,
				"feasible":  boolt,
			}, "from_city", "to_city", "feasible")},
		{"name": "sim_get_transfers",
			"description": "World Simulator (cascade): list the currently-cancelled inter-leg transfer edges as authoritative read-only data ({cancelled_transfers:[{from,to}]}). The Transport agent consumes this to detect flight cancellations.",
			"inputSchema": obj(map[string]any{})},
		// N1 fault control — flip a counterparty (carrier/OTA/lodging) insolvent.
		{"name": "sim_set_counterparty",
			"description": "World Simulator (N1): mark a counterparty (counterparty_id == catalog_id, e.g. a carrier or hotel id) solvent (true) or INSOLVENT (false). An insolvent counterparty's bookings go VOID: complete_checkout rejects any session referencing it (status:void, reason:counterparty_insolvent) — no counterparty to rebook against. Distinct from sim_set_availability (sold-out, substitutable) and sim_set_transfer (flight cancel, re-routable). The fault the society Fraud gate pre-empts. Demo/test only.",
			"inputSchema": obj(map[string]any{
				"counterparty_id": str,
				"solvent":         boolt,
			}, "counterparty_id", "solvent")},
		{"name": "sim_get_counterparties",
			"description": "World Simulator (N1): list the currently-insolvent counterparty ids as authoritative read-only data ({insolvent_counterparties:[...]}).",
			"inputSchema": obj(map[string]any{})},
		{"name": "search_catalog", "description": "Search the catalog (backend data only — no fabricated prices). Money in integer cents. Default kind=HOTEL (SEA hotel catalog). Pass query.kind=\"FOOD_DELIVERY\" to search the SIMULATED late-night food-delivery catalog instead (filter by city + optional diet + delivery_window). Food inventory is honestly simulated; the checkout against it is a real UCP prepaid transaction (NOT a live Ele.me integration).",
			"inputSchema": obj(map[string]any{"meta": meta, "query": obj(map[string]any{
				"kind": str, "city": str, "checkin": str, "checkout": str, "adults": intt, "max_cents": intt,
				"diet": str, "delivery_window": str,
			}, "city")}, "query")},
		{"name": "lookup_catalog", "description": "Look up a catalog item by id (live availability + price in cents). Default kind=HOTEL (hotel_id). Pass product.kind=\"FOOD_DELIVERY\" with product.food_id to look up a SIMULATED food-delivery offer.",
			"inputSchema": obj(map[string]any{"meta": meta, "product": obj(map[string]any{
				"kind": str, "hotel_id": str, "food_id": str, "checkin": str, "checkout": str, "adults": intt,
			})}, "product")},
		{"name": "create_checkout", "description": "Create a checkout session for line items. Returns id + computed total (cents), status=incomplete. Pass user_budget_cents to bind the merchant veto to the user's per-trip budget (SEV-1b: the veto enforces min(user_budget_cents, BudgetHardMaxCents)).",
			"inputSchema": obj(map[string]any{"meta": meta, "checkout": obj(map[string]any{
				"user_id": str, "line_items": lineItems,
				"user_budget_cents": map[string]any{"type": "integer", "description": "Caller's per-trip budget in cents. When provided, complete_checkout vetos against min(user_budget_cents, BudgetHardMaxCents) instead of the house default ceiling."},
				"wallet_session_id": map[string]any{"type": "string", "description": "Optional SIMULATED prepaid wallet binding (the deterministic per-run digest). When set, complete_checkout debits this wallet (HTTP 402 insufficient_funds when the total exceeds the funded balance, distinct from the 403 budget veto) and cancel_checkout credits it. Empty → no wallet logic (backward-compatible)."},
			}, "user_id", "line_items")}, "checkout")},
		{"name": "update_checkout", "description": "Update a checkout session (e.g. set buyer_consent for L2 HITL approval). Owner-only.",
			"inputSchema": obj(map[string]any{"meta": meta, "checkout": obj(map[string]any{
				"id": str, "buyer_consent": boolt,
			}, "id")}, "checkout")},
		{"name": "complete_checkout", "description": "Finalize a checkout. Hard-max enforced server-side. L2: requires buyer_consent (HITL). L3: requires a valid ap2_mandate. Owner-only; agent cannot override. Pass idempotency_key to get the same booking_ref on retries (SEV-1a idempotency).",
			"inputSchema": obj(map[string]any{"meta": meta, "checkout": obj(map[string]any{
				"id": str, "buyer_consent": boolt, "ap2_mandate": mandate,
				"idempotency_key": map[string]any{"type": "string", "description": "Client-supplied idempotency key. A repeat call with the same key after a successful complete returns the original booking_ref without creating a second booking."},
			}, "id")}, "checkout")},
		{"name": "cancel_checkout", "description": "VOID a checkout, releasing the booking it holds (its booking_ref is dropped; status → cancelled). Owner-only. Idempotent and safe: cancelling an unknown or already-cancelled id returns success; a cancelled checkout can NEVER be re-completed. Used by cascade recovery (§12.1) to void the superseded booking when the next cycle commits — keeping exactly one live itinerary, no double-charge. Narrative-only refundability (no merchant refund ledger).",
			"inputSchema": obj(map[string]any{"meta": meta, "checkout": obj(map[string]any{
				"id": str,
			}, "id")}, "checkout")},
	}
}
