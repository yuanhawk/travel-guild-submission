package main

// sim.go — World Simulator: mutable availability state for L3-core testing.
//
// Adds a sim control that marks hotels unavailable (sold-out) so the society
// can demonstrate reactive disruption-recovery.
//
// Design: §12.1 §12.8 §12.9 (AGENT-SOCIETY-A2A-DESIGN.md).
//
// TWO distinct exogenous fault classes (both visible-as-data, never
// LLM-invented; the operator triggers, never touches recovery — §10.5):
//
//  1. HOTEL SOLD-OUT  — `flip_availability(hotel_id, false)` (the L3-core fault).
//     Maps onto the catalog the Accommodation agent reads.
//
//  2. FLIGHT / TRANSFER CANCEL — `flip_transfer(from_city, to_city, false)`
//     (the cascade fault, §12.1 "Flight cancel / missed connection"). Maps onto
//     the inter-leg transfer data the Transport agent reads, NOT the hotel
//     catalog. A cancelled flight makes an inter-city edge infeasible; the
//     Transport agent owns recovery (re-route via hub / re-sequence / shift day).
//
// Both states are simple in-process maps held on the *store (shared lock):
//   unavailable          map[hotel_id]bool        (sold-out hotels)
//   unavailableTransfers map[directedCityPair]bool (cancelled flights/transfers)
// All agents read the same state the simulator mutates — no hidden channel.
//
// How the HOTEL state gates the commerce surface:
//   search_catalog   — EXCLUDES unavailable hotels (they don't appear in results).
//   lookup_catalog   — Returns status:"unavailable" for an unavailable hotel.
//   create_checkout  — Rejects any line_item referencing an unavailable hotel.
//
// How the TRANSFER state surfaces (merchant-side, exogenous, visible-as-data):
//   GET /admin/sim/transfer        — lists cancelled transfers.
//   sim_get_transfers (MCP)        — same, callable over JSON-RPC so the
//                                    Transport agent can read the fault as data.
// The transfer fault is intentionally NOT a hard checkout gate: a flight cancel
// does not make a hotel unbookable; it makes the inter-leg ROUTE infeasible,
// which the Transport agent detects and re-routes around BEFORE checkout. The
// merchant publishes the cancelled-transfer set as authoritative read-only data;
// the Transport agent's feasibility check consumes it.
//
// MCP methods:
//   sim_set_availability(hotel_id, available bool)
//   sim_set_transfer(from_city, to_city, feasible bool)
//   sim_get_transfers()  -> {cancelled_transfers: [{from, to}, ...]}
// Admin HTTP endpoints (loopback-only in the Docker mesh; no auth in demo mode):
//   POST /admin/sim/availability  { "hotel_id": "...", "available": bool }
//   POST /admin/sim/transfer      { "from_city": "...", "to_city": "...", "feasible": bool }
//   GET  /admin/sim/transfer      -> { "cancelled_transfers": [...] }

import (
	"encoding/json"
	"io"
	"net/http"
	"strings"
)

// ---------------------------------------------------------------------------
// THIRD exogenous fault class: COUNTERPARTY INSOLVENCY (N1) — sim.go
// ---------------------------------------------------------------------------
//
//  3. COUNTERPARTY INSOLVENT — `simSetCounterparty(counterparty_id, false)`
//     (the N1 fault the Fraud gate pre-empts). A counterparty (carrier / OTA /
//     lodging operator) CEASES OPERATIONS: any booking committed against it goes
//     VOID with NO counterparty to rebook against. This is DISTINCT from a
//     sold-out hotel (sim_set_availability — which has a substitute) and from a
//     cancelled flight edge (sim_set_transfer — which the Transport agent
//     re-routes around). An insolvent supplier's paper is worthless: the ticket
//     is stranded.
//
//     Identity (§19.1 of the society design): a counterparty_id == its catalog_id
//     (the catalog id IS the join key, no second namespace). The merchant keys
//     line items by hotel_id (a catalog id), so the insolvency set keys on the
//     same id space the line items already use.
//
//     How it surfaces (merchant-side, exogenous, visible-as-data):
//       complete_checkout — REJECTS (status:"void", reason:"counterparty_insolvent")
//                           any session whose line items reference an insolvent
//                           counterparty: the commit is VOID (no booking_ref). A
//                           ticket the agent thought it held is worthless.
//       sim_get_counterparties (MCP) / GET /admin/sim/counterparty — list the
//                           insolvent set as authoritative read-only data.
//     The Fraud gate (society) blocks committing against a known-high-insolvency-
//     risk counterparty BEFORE this fault can strand the traveler; this sim flip
//     lets the DC3 scenario DEMONSTRATE the fault the gate pre-empts.

// transferKey is a directed (from,to) city pair, lower-cased. A flight cancel is
// directional — ET120 Gondar→Lalibela cancelling does not cancel Lalibela→Gondar.
type transferKey struct {
	From string
	To   string
}

func newTransferKey(from, to string) transferKey {
	return transferKey{From: strings.ToLower(strings.TrimSpace(from)), To: strings.ToLower(strings.TrimSpace(to))}
}

// simSetAvailability marks a hotel available or unavailable in the sim state.
// Thread-safe: uses the store lock.
func (s *store) simSetAvailability(hotelID string, available bool) {
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.unavailable == nil {
		s.unavailable = map[string]bool{}
	}
	if available {
		delete(s.unavailable, hotelID)
	} else {
		s.unavailable[hotelID] = true
	}
}

// simIsAvailable returns false when the hotel has been marked unavailable.
// Thread-safe (reads under lock).
func (s *store) simIsAvailable(hotelID string) bool {
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.unavailable == nil {
		return true
	}
	return !s.unavailable[hotelID]
}

// simListUnavailable returns the set of hotel_ids currently marked unavailable.
func (s *store) simListUnavailable() []string {
	s.mu.Lock()
	defer s.mu.Unlock()
	out := []string{}
	for id := range s.unavailable {
		out = append(out, id)
	}
	return out
}

// simAdminHandler handles POST /admin/sim/availability for external test control.
//
// Request body: {"hotel_id": "bali-legian-beach", "available": false}
// Response:     {"hotel_id": "...", "available": false, "status": "ok"}
func simAdminHandler(st *store) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodPost {
			writeJSON(w, http.StatusMethodNotAllowed, map[string]string{"error": "POST required"})
			return
		}
		body, _ := io.ReadAll(io.LimitReader(r.Body, 4096))
		var req struct {
			HotelID   string `json:"hotel_id"`
			Available bool   `json:"available"`
		}
		// Default available=true when field absent (use pointer to detect)
		type reqFull struct {
			HotelID   string `json:"hotel_id"`
			Available *bool  `json:"available"`
		}
		var rf reqFull
		if err := json.Unmarshal(body, &rf); err != nil || rf.HotelID == "" {
			writeJSON(w, http.StatusBadRequest, map[string]string{"error": "hotel_id required"})
			return
		}
		avail := true
		if rf.Available != nil {
			avail = *rf.Available
		}
		req.HotelID = rf.HotelID
		req.Available = avail

		// Validate hotel exists in catalog
		if _, ok := findHotel(req.HotelID); !ok {
			writeJSON(w, http.StatusNotFound, map[string]any{
				"error":    "hotel_not_found",
				"hotel_id": req.HotelID,
			})
			return
		}
		st.simSetAvailability(req.HotelID, req.Available)
		writeJSON(w, http.StatusOK, map[string]any{
			"hotel_id":  req.HotelID,
			"available": req.Available,
			"status":    "ok",
		})
	}
}

// simStatusHandler handles GET /admin/sim/availability for inspection.
func simStatusHandler(st *store) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodGet {
			writeJSON(w, http.StatusMethodNotAllowed, map[string]string{"error": "GET required"})
			return
		}
		writeJSON(w, http.StatusOK, map[string]any{
			"unavailable_hotels": st.simListUnavailable(),
		})
	}
}

// ---------------------------------------------------------------------------
// Transfer / flight cancellation fault (cascade) — §12.1
// ---------------------------------------------------------------------------

// simSetTransfer marks an inter-leg transfer (a flight/road edge between two
// cities) feasible or cancelled. Directional: from→to. Thread-safe.
func (s *store) simSetTransfer(from, to string, feasible bool) {
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.unavailableTransfers == nil {
		s.unavailableTransfers = map[transferKey]bool{}
	}
	k := newTransferKey(from, to)
	if feasible {
		delete(s.unavailableTransfers, k)
	} else {
		s.unavailableTransfers[k] = true
	}
}

// simTransferFeasible returns false when the from→to transfer has been cancelled.
// Thread-safe (reads under lock).
func (s *store) simTransferFeasible(from, to string) bool {
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.unavailableTransfers == nil {
		return true
	}
	return !s.unavailableTransfers[newTransferKey(from, to)]
}

// simListCancelledTransfers returns the set of cancelled transfer edges.
func (s *store) simListCancelledTransfers() []map[string]string {
	s.mu.Lock()
	defer s.mu.Unlock()
	out := []map[string]string{}
	for k := range s.unavailableTransfers {
		out = append(out, map[string]string{"from": k.From, "to": k.To})
	}
	return out
}

// simTransferAdminHandler handles POST/GET /admin/sim/transfer.
//
// POST body: {"from_city":"gondar","to_city":"lalibela","feasible":false}
// POST resp: {"from_city":"...","to_city":"...","feasible":false,"status":"ok"}
// GET  resp: {"cancelled_transfers":[{"from":"...","to":"..."}, ...]}
func simTransferAdminHandler(st *store) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		switch r.Method {
		case http.MethodGet:
			writeJSON(w, http.StatusOK, map[string]any{
				"cancelled_transfers": st.simListCancelledTransfers(),
			})
			return
		case http.MethodPost:
			body, _ := io.ReadAll(io.LimitReader(r.Body, 4096))
			// Default feasible=true when absent (use pointer to detect).
			var rf struct {
				FromCity string `json:"from_city"`
				ToCity   string `json:"to_city"`
				Feasible *bool  `json:"feasible"`
			}
			if err := json.Unmarshal(body, &rf); err != nil || rf.FromCity == "" || rf.ToCity == "" {
				writeJSON(w, http.StatusBadRequest, map[string]string{"error": "from_city and to_city required"})
				return
			}
			feasible := true
			if rf.Feasible != nil {
				feasible = *rf.Feasible
			}
			st.simSetTransfer(rf.FromCity, rf.ToCity, feasible)
			writeJSON(w, http.StatusOK, map[string]any{
				"from_city": rf.FromCity,
				"to_city":   rf.ToCity,
				"feasible":  feasible,
				"status":    "ok",
			})
			return
		default:
			writeJSON(w, http.StatusMethodNotAllowed, map[string]string{"error": "GET or POST required"})
		}
	}
}

// ---------------------------------------------------------------------------
// Counterparty insolvency fault (N1) — the fault the Fraud gate pre-empts.
// ---------------------------------------------------------------------------

// normCounterparty lower-cases + trims a counterparty/catalog id (the §19.1 join
// key space — counterparty_id == catalog_id == the line item's hotel_id).
func normCounterparty(id string) string {
	return strings.ToLower(strings.TrimSpace(id))
}

// simSetCounterparty marks a counterparty solvent (true) or INSOLVENT (false).
// An insolvent counterparty's committed bookings go VOID (complete_checkout
// rejects them). Thread-safe (store lock).
func (s *store) simSetCounterparty(counterpartyID string, solvent bool) {
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.counterpartyInsolvent == nil {
		s.counterpartyInsolvent = map[string]bool{}
	}
	k := normCounterparty(counterpartyID)
	if solvent {
		delete(s.counterpartyInsolvent, k)
	} else {
		s.counterpartyInsolvent[k] = true
	}
}

// simCounterpartySolvent returns false when the counterparty has been flipped
// insolvent. Thread-safe (reads under lock).
func (s *store) simCounterpartySolvent(counterpartyID string) bool {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.simCounterpartySolventLocked(counterpartyID)
}

// simCounterpartySolventLocked is the lock-free body — the CALLER MUST already
// hold s.mu (e.g. complete_checkout, which locks the store for the whole commit).
// Re-locking there would deadlock (the mutex is non-reentrant).
func (s *store) simCounterpartySolventLocked(counterpartyID string) bool {
	if s.counterpartyInsolvent == nil {
		return true
	}
	return !s.counterpartyInsolvent[normCounterparty(counterpartyID)]
}

// simListInsolventCounterparties returns the set of insolvent counterparty ids.
func (s *store) simListInsolventCounterparties() []string {
	s.mu.Lock()
	defer s.mu.Unlock()
	out := []string{}
	for id := range s.counterpartyInsolvent {
		out = append(out, id)
	}
	return out
}

// simCounterpartyAdminHandler handles POST/GET /admin/sim/counterparty.
//
// POST body: {"counterparty_id":"carrier-skylark-budget-air","solvent":false}
// POST resp: {"counterparty_id":"...","solvent":false,"status":"ok"}
// GET  resp: {"insolvent_counterparties":["...", ...]}
func simCounterpartyAdminHandler(st *store) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		switch r.Method {
		case http.MethodGet:
			writeJSON(w, http.StatusOK, map[string]any{
				"insolvent_counterparties": st.simListInsolventCounterparties(),
			})
			return
		case http.MethodPost:
			body, _ := io.ReadAll(io.LimitReader(r.Body, 4096))
			// Default solvent=true when absent (use pointer to detect).
			var rf struct {
				CounterpartyID string `json:"counterparty_id"`
				Solvent        *bool  `json:"solvent"`
			}
			if err := json.Unmarshal(body, &rf); err != nil || rf.CounterpartyID == "" {
				writeJSON(w, http.StatusBadRequest, map[string]string{"error": "counterparty_id required"})
				return
			}
			solvent := true
			if rf.Solvent != nil {
				solvent = *rf.Solvent
			}
			st.simSetCounterparty(rf.CounterpartyID, solvent)
			writeJSON(w, http.StatusOK, map[string]any{
				"counterparty_id": rf.CounterpartyID,
				"solvent":         solvent,
				"status":          "ok",
			})
			return
		default:
			writeJSON(w, http.StatusMethodNotAllowed, map[string]string{"error": "GET or POST required"})
		}
	}
}
