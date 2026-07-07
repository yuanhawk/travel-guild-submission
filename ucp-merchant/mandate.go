package main

// Pragmatic ap2_mandate (§8.4): non-repudiable L3 consent. The user's device key
// signs a mandate (budget ceiling in integer cents, expiry, bound to the
// checkout + currency); the merchant verifies it server-side before any
// autonomous booking. Full SD-JWT/JCS/PSP chain is DEFERRED (plan §8 scope).

import (
	"crypto/ecdsa"
	"encoding/base64"
	"errors"
	"fmt"
	"strings"
	"time"
)

type ap2Mandate struct {
	UserID      string `json:"user_id"`
	CheckoutID  string `json:"checkout_id"`
	BudgetCents int64  `json:"budget_ceiling_cents"`
	Currency    string `json:"currency"`
	ValidUntil  string `json:"valid_until"` // RFC3339
	Signature   string `json:"signature"`   // base64 r||s over mandateBase, by user device key
}

// hasDelim rejects characters that could collide the field delimiter and forge a
// different binding under the same signature (M1).
func hasDelim(s string) bool { return strings.ContainsAny(s, "|\n\r") }

// mandateBase is the fixed signing base. Fields are validated delimiter-free so
// the encoding is unambiguous (signer and verifier must produce identical bytes).
func mandateBase(m ap2Mandate) []byte {
	return []byte(fmt.Sprintf("%s|%s|%d|%s|%s",
		m.UserID, m.CheckoutID, m.BudgetCents, m.Currency, m.ValidUntil))
}

// signMandate is a helper (tests / a future user-device client).
func signMandate(m ap2Mandate, userPriv *ecdsa.PrivateKey) string {
	mk := &merchantKey{priv: userPriv}
	return base64.StdEncoding.EncodeToString(mk.signRaw(mandateBase(m)))
}

// verifyMandate authorizes an autonomous booking: signature vs the user's
// registered device key, binding to this checkout + currency, freshness, and
// that the budget covers the live total (all integer cents).
func verifyMandate(m ap2Mandate, userPub *ecdsa.PublicKey, checkoutID, currency string, liveCents int64, now time.Time) error {
	if userPub == nil {
		return errors.New("mandate_no_registered_key")
	}
	if hasDelim(m.UserID) || hasDelim(m.CheckoutID) || hasDelim(m.Currency) {
		return errors.New("mandate_invalid_fields")
	}
	if m.CheckoutID != checkoutID {
		return errors.New("mandate_mismatch")
	}
	if m.Currency != currency {
		return errors.New("mandate_currency_mismatch")
	}
	sig, err := base64.StdEncoding.DecodeString(m.Signature)
	if err != nil || !verifyRaw(userPub, mandateBase(m), sig) {
		return errors.New("mandate_invalid_signature")
	}
	t, err := time.Parse(time.RFC3339, m.ValidUntil)
	if err != nil || now.After(t) {
		return errors.New("mandate_expired")
	}
	if liveCents > m.BudgetCents {
		return errors.New("budget_exceeded")
	}
	return nil
}
