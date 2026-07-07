package main

import (
	"crypto/ecdsa"
	"crypto/elliptic"
	"crypto/rand"
	"testing"
	"time"
)

func TestMandateVerify(t *testing.T) {
	priv, _ := ecdsa.GenerateKey(elliptic.P256(), rand.Reader)
	pub := &priv.PublicKey
	now := time.Now()
	m := ap2Mandate{UserID: "u1", CheckoutID: "co_1", BudgetCents: 100000, Currency: "USD",
		ValidUntil: now.Add(time.Hour).Format(time.RFC3339)}
	m.Signature = signMandate(m, priv)

	if err := verifyMandate(m, pub, "co_1", "USD", 80000, now); err != nil {
		t.Fatalf("valid mandate rejected: %v", err)
	}
	bad := func(desc string, mm ap2Mandate, ck, cur string, cents int64, n time.Time) {
		if err := verifyMandate(mm, pub, ck, cur, cents, n); err == nil {
			t.Fatalf("%s: should have been rejected", desc)
		}
	}
	bad("over budget", m, "co_1", "USD", 120000, now)
	bad("checkout mismatch", m, "co_OTHER", "USD", 80000, now)
	bad("currency mismatch", m, "co_1", "EUR", 80000, now)
	if err := verifyMandate(m, nil, "co_1", "USD", 80000, now); err == nil {
		t.Fatal("nil registered key accepted")
	}

	exp := m
	exp.ValidUntil = now.Add(-time.Hour).Format(time.RFC3339)
	exp.Signature = signMandate(exp, priv)
	bad("expired", exp, "co_1", "USD", 80000, now)

	tam := m
	tam.BudgetCents = 500000 // changed after signing
	bad("tampered budget", tam, "co_1", "USD", 400000, now)

	inj := ap2Mandate{UserID: "u1|co_x|9999999", CheckoutID: "co_1", BudgetCents: 100000, Currency: "USD",
		ValidUntil: now.Add(time.Hour).Format(time.RFC3339)}
	inj.Signature = signMandate(inj, priv)
	bad("delimiter injection", inj, "co_1", "USD", 80000, now)
}
