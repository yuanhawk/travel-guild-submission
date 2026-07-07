package main

// mandate_vc.go — #57 W3C-VC 2.0 two-tier mandate ENVELOPE (pragmatic stand-in).
//
// HONEST SCOPE (no overclaim): this wraps the existing, server-verified ECDSA ap2Mandate in
// a W3C-VC 2.0 *shape* (@context / type / credentialSubject / proof) so the mandate is legible
// to VC tooling and conformance scanners. It is NOT full W3C-VC conformance: the proof is the
// existing raw ECDSA P-256 r||s signature (verified by verifyMandate), NOT a
// JsonWebSignature2020 / SD-JWT with RFC 8785 JCS canonicalization — those are future work
// (DESIGN §13). Verification UNWRAPS the VC back to ap2Mandate and runs the SAME real check,
// so this layer adds presentation, never a new trust path.
//
// Two-tier (AP2): CheckoutMandate (this VC — platform-issued, carries the checkout budget
// binding) + PaymentMandate (the PSP fund-authorization tier). In this demo the PaymentMandate
// is fulfilled by the SIMULATED Alipay rail (alipay_sim.go); a real SD-JWT PSP credential is
// KIV (needs a business account).

import "errors"

const vcContext = "https://www.w3.org/ns/credentials/v2"

type vcProof struct {
	Type       string `json:"type"`       // pragmatic raw ECDSA P-256 r||s — NOT JsonWebSignature2020
	ProofValue string `json:"proofValue"` // == ap2Mandate.Signature (base64 r||s over mandateBase)
	Note       string `json:"note,omitempty"`
}

// checkoutMandateVC is the W3C-VC envelope over the checkout-budget mandate (AP2 tier 1).
type checkoutMandateVC struct {
	Context        []string `json:"@context"`
	Type           []string `json:"type"`
	Issuer         string   `json:"issuer"`         // presentation-only: NOT covered by the proof — never trust for authz (audit F5)
	ExpirationDate string   `json:"expirationDate"` // == ap2Mandate.ValidUntil (IS signed via mandateBase)
	CredentialSubject map[string]any `json:"credentialSubject"`
	Proof             vcProof        `json:"proof"`
	Note              string         `json:"note,omitempty"` // disclaimer travels with the JSON, not just the source (audit F4)
}

// paymentMandate is AP2 tier 2 (PSP fund authorization). STUB: settlement is the SIMULATED
// Alipay rail (alipay_sim.go); a real SD-JWT PSP credential is KIV.
type paymentMandate struct {
	HandlerID   string `json:"handler_id"`
	AmountCents int64  `json:"amount_cents"`
	PSP         string `json:"psp"` // e.g. "alipay_sim"
	Simulated   bool   `json:"simulated"`
}

const vcProofNote = "pragmatic raw ECDSA r||s over the canonical mandate base; full JsonWebSignature2020/SD-JWT+JCS is future work (DESIGN §13)"

// toCheckoutVC wraps the verifiable mandate in the W3C-VC shape (no clock — pure transform).
func (m ap2Mandate) toCheckoutVC(issuer string) checkoutMandateVC {
	return checkoutMandateVC{
		Context:        []string{vcContext},
		Type:           []string{"VerifiableCredential", "CheckoutMandate"},
		Issuer:         issuer,
		ExpirationDate: m.ValidUntil,
		CredentialSubject: map[string]any{
			"id":                   m.UserID,
			"checkout_id":          m.CheckoutID,
			"budget_ceiling_cents": m.BudgetCents,
			"currency":             m.Currency,
		},
		Proof: vcProof{Type: "UcpEcdsaP256-2026", ProofValue: m.Signature, Note: vcProofNote},
		Note:  "pragmatic W3C-VC SHAPE for tooling legibility — NOT a conformant Verifiable Credential (see proof.note)",
	}
}

// fromCheckoutVC unwraps the envelope back to the verifiable ap2Mandate, so the SAME
// server-side check (verifyMandate) applies. Errors on a malformed envelope.
func fromCheckoutVC(vc checkoutMandateVC) (ap2Mandate, error) {
	cs := vc.CredentialSubject
	uid, _ := cs["id"].(string)
	cid, _ := cs["checkout_id"].(string)
	cur, _ := cs["currency"].(string)
	var budget int64
	switch b := cs["budget_ceiling_cents"].(type) {
	case int64:
		budget = b
	case int:
		budget = int64(b)
	case float64:
		budget = int64(b)
	default:
		return ap2Mandate{}, errors.New("vc: bad or missing budget_ceiling_cents")
	}
	if uid == "" || cid == "" {
		return ap2Mandate{}, errors.New("vc: missing credentialSubject id/checkout_id")
	}
	return ap2Mandate{
		UserID:      uid,
		CheckoutID:  cid,
		BudgetCents: budget,
		Currency:    cur,
		ValidUntil:  vc.ExpirationDate,
		Signature:   vc.Proof.ProofValue,
	}, nil
}
