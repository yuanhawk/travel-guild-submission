package main

// alipay_sim.go — #57 AP2: SIMULATED settlement rail.
//
// Demonstrates the post-booking settlement leg of the AP2 flow WITHOUT a real PSP.
// A completed booking_ref + total_cents "settles" to a deterministic transaction id.
//
// HONESTLY LABELED: every response carries "simulated": true + a note that real Alipay
// requires a business-account keypair (KIV — see DESIGN.md Future Work). We never imply a
// real money movement happened. Endpoints:
//
//	POST /admin/sim/alipay/settle  {"booking_ref":"...","total_cents":120000}
//	     -> {"transaction_id","status":"paid","booking_ref","total_cents","simulated":true,"note"}
//	GET  /admin/sim/alipay/settle
//	     -> {"settlements":[...],"total_settled_cents","count","simulated":true,"note"}
//
// var-0: transaction_id is a pure function of (booking_ref,total_cents) — no clock/random —
// and settlement is idempotent per booking_ref, so replays are byte-identical.

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"sort"
)

const alipaySimNote = "SIMULATED settlement — Alipay production requires a business-account keypair (KIV)."

type alipaySettlement struct {
	BookingRef    string `json:"booking_ref"`
	TotalCents    int    `json:"total_cents"`
	TransactionID string `json:"transaction_id"`
	Status        string `json:"status"`
}

// alipaySimTxnID derives a deterministic settlement id (no clock/random => var-0 safe).
func alipaySimTxnID(bookingRef string, totalCents int) string {
	h := sha256.Sum256([]byte(fmt.Sprintf("%s|%d", bookingRef, totalCents)))
	return "alipay-sim-" + hex.EncodeToString(h[:])[:16]
}

// alipaySettle records a settlement; idempotent per booking_ref (re-settle => same record).
func (st *store) alipaySettle(bookingRef string, totalCents int) alipaySettlement {
	st.mu.Lock()
	defer st.mu.Unlock()
	if rec, ok := st.alipaySettlements[bookingRef]; ok {
		return rec
	}
	rec := alipaySettlement{
		BookingRef:    bookingRef,
		TotalCents:    totalCents,
		TransactionID: alipaySimTxnID(bookingRef, totalCents),
		Status:        "paid",
	}
	st.alipaySettlements[bookingRef] = rec
	return rec
}

func (st *store) alipaySummary() (recs []alipaySettlement, total int) {
	st.mu.Lock()
	defer st.mu.Unlock()
	for _, r := range st.alipaySettlements {
		recs = append(recs, r)
		total += r.TotalCents
	}
	sort.Slice(recs, func(i, j int) bool { return recs[i].BookingRef < recs[j].BookingRef })
	return recs, total
}

func alipaySimAdminHandler(st *store) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		switch r.Method {
		case http.MethodGet:
			recs, total := st.alipaySummary()
			writeJSON(w, http.StatusOK, map[string]any{
				"settlements":         recs,
				"total_settled_cents": total,
				"count":               len(recs),
				"simulated":           true,
				"note":                alipaySimNote,
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
			rec := st.alipaySettle(rf.BookingRef, rf.TotalCents)
			writeJSON(w, http.StatusOK, map[string]any{
				"transaction_id": rec.TransactionID,
				"status":         rec.Status,
				"booking_ref":    rec.BookingRef,
				"total_cents":    rec.TotalCents,
				"simulated":      true,
				"note":           alipaySimNote,
			})
		default:
			writeJSON(w, http.StatusMethodNotAllowed, map[string]string{"error": "method not allowed"})
		}
	}
}
