package main

import (
	"crypto/ecdsa"
	"crypto/elliptic"
	"crypto/rand"
	"testing"
	"time"
)

// #57: the W3C-VC envelope is a faithful, reversible SHAPE over the verifiable mandate —
// wrap -> unwrap round-trips, the SAME verifyMandate check still applies, and tampering the
// VC's credentialSubject is rejected by that check (the proof binds the real fields).
func TestCheckoutMandateVCRoundTripAndVerify(t *testing.T) {
	priv, _ := ecdsa.GenerateKey(elliptic.P256(), rand.Reader)
	pub := &priv.PublicKey
	now := time.Now()
	m := ap2Mandate{UserID: "u1", CheckoutID: "co_1", BudgetCents: 100000, Currency: "USD",
		ValidUntil: now.Add(time.Hour).Format(time.RFC3339)}
	m.Signature = signMandate(m, priv)

	// Envelope shape.
	vc := m.toCheckoutVC("https://merchant.example")
	if len(vc.Context) == 0 || vc.Context[0] != vcContext {
		t.Fatalf("missing @context: %v", vc.Context)
	}
	hasType := false
	for _, ty := range vc.Type {
		if ty == "CheckoutMandate" {
			hasType = true
		}
	}
	if !hasType {
		t.Fatalf("type missing CheckoutMandate: %v", vc.Type)
	}
	if vc.CredentialSubject["checkout_id"] != "co_1" || vc.Proof.ProofValue != m.Signature {
		t.Fatalf("credentialSubject/proof not bound: %+v", vc)
	}

	// Round-trips back to the exact verifiable mandate.
	got, err := fromCheckoutVC(vc)
	if err != nil || got != m {
		t.Fatalf("VC round-trip lost fidelity: err=%v got=%+v want=%+v", err, got, m)
	}
	// The SAME server-side check passes for the unwrapped mandate.
	if err := verifyMandate(got, pub, "co_1", "USD", 80000, now); err != nil {
		t.Fatalf("unwrapped VC mandate failed verifyMandate: %v", err)
	}

	// Tamper the VC budget -> unwrap -> verifyMandate MUST reject (proof binds real fields).
	tampered := m.toCheckoutVC("https://merchant.example")
	tampered.CredentialSubject["budget_ceiling_cents"] = int64(999999)
	bad, err := fromCheckoutVC(tampered)
	if err != nil {
		t.Fatalf("unwrap tampered: %v", err)
	}
	if err := verifyMandate(bad, pub, "co_1", "USD", 80000, now); err == nil {
		t.Fatal("tampered VC budget unexpectedly verified")
	}
}

// Audit F6: every signed field tampered via the VC must be rejected, + malformed envelope errors.
func TestCheckoutMandateVCTamperVectorsAndMalformed(t *testing.T) {
	priv, _ := ecdsa.GenerateKey(elliptic.P256(), rand.Reader)
	pub := &priv.PublicKey
	now := time.Now()
	base := ap2Mandate{UserID: "u1", CheckoutID: "co_1", BudgetCents: 100000, Currency: "USD",
		ValidUntil: now.Add(time.Hour).Format(time.RFC3339)}
	base.Signature = signMandate(base, priv)

	// Each signed credentialSubject field, tampered via the VC, must fail the real signature.
	for _, tc := range []struct {
		name, key string
		val       any
	}{
		{"checkout_id", "checkout_id", "co_EVIL"},
		{"currency", "currency", "EUR"},
		{"user_id", "id", "attacker"},
	} {
		vc := base.toCheckoutVC("https://m")
		vc.CredentialSubject[tc.key] = tc.val
		m, err := fromCheckoutVC(vc)
		if err != nil {
			t.Fatalf("%s unwrap: %v", tc.name, err)
		}
		if err := verifyMandate(m, pub, "co_1", "USD", 80000, now); err == nil {
			t.Fatalf("tampered %s unexpectedly verified", tc.name)
		}
	}
	// Tampered expirationDate (ValidUntil is signed) -> reject.
	vexp := base.toCheckoutVC("https://m")
	vexp.ExpirationDate = now.Add(48 * time.Hour).Format(time.RFC3339)
	mexp, _ := fromCheckoutVC(vexp)
	if err := verifyMandate(mexp, pub, "co_1", "USD", 80000, now); err == nil {
		t.Fatal("tampered expirationDate unexpectedly verified")
	}
	// Garbage proof -> reject.
	vbad := base.toCheckoutVC("https://m")
	vbad.Proof.ProofValue = "Z2FyYmFnZQ=="
	mbad, _ := fromCheckoutVC(vbad)
	if err := verifyMandate(mbad, pub, "co_1", "USD", 80000, now); err == nil {
		t.Fatal("garbage proof unexpectedly verified")
	}
	// Malformed envelope: missing budget -> error (no silent default).
	vmiss := base.toCheckoutVC("https://m")
	delete(vmiss.CredentialSubject, "budget_ceiling_cents")
	if _, err := fromCheckoutVC(vmiss); err == nil {
		t.Fatal("missing budget_ceiling_cents should error, not default")
	}
}
