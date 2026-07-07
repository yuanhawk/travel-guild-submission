package main

// RFC 9421 (HTTP Message Signatures) + RFC 9530 (Content-Digest) primitives for
// the UCP merchant, plus EC P-256 (ES256) key management for signing_keys.
// First slice: crypto foundation + manifest JWK. Request-verification wiring and
// ap2_mandate build on this.

import (
	"crypto/ecdsa"
	"crypto/elliptic"
	"crypto/rand"
	"crypto/sha256"
	"crypto/x509"
	"encoding/base64"
	"encoding/pem"
	"errors"
	"fmt"
	"math/big"
	"os"
	"strings"
)

var b64u = base64.RawURLEncoding

type merchantKey struct {
	priv *ecdsa.PrivateKey
	kid  string
}

// loadOrCreateKey loads an EC P-256 key from UCP_SIGNING_KEY_PATH (PEM, SEC1 or
// PKCS8); otherwise generates an ephemeral one (logged by caller).
func loadOrCreateKey() (*merchantKey, bool) {
	if p := os.Getenv("UCP_SIGNING_KEY_PATH"); p != "" {
		if data, err := os.ReadFile(p); err == nil {
			if k, err := parseECPrivPEM(data); err == nil {
				return &merchantKey{priv: k, kid: kidFor(&k.PublicKey)}, true
			}
		}
	}
	k, _ := ecdsa.GenerateKey(elliptic.P256(), rand.Reader)
	return &merchantKey{priv: k, kid: kidFor(&k.PublicKey)}, false
}

func parseECPrivPEM(data []byte) (*ecdsa.PrivateKey, error) {
	block, _ := pem.Decode(data)
	if block == nil {
		return nil, errors.New("no PEM block")
	}
	if k, err := x509.ParseECPrivateKey(block.Bytes); err == nil {
		return k, nil
	}
	if k, err := x509.ParsePKCS8PrivateKey(block.Bytes); err == nil {
		if ec, ok := k.(*ecdsa.PrivateKey); ok {
			return ec, nil
		}
	}
	return nil, errors.New("not an EC P-256 private key")
}

func kidFor(pub *ecdsa.PublicKey) string {
	h := sha256.Sum256(append(pub.X.Bytes(), pub.Y.Bytes()...))
	return "merch-" + b64u.EncodeToString(h[:6])
}

func coord(n *big.Int) string {
	b := make([]byte, 32)
	n.FillBytes(b)
	return b64u.EncodeToString(b)
}

// publicJWK returns the JWK for the manifest signing_keys array (RFC 7517).
func (m *merchantKey) publicJWK() map[string]any {
	return map[string]any{
		"kid": m.kid, "kty": "EC", "crv": "P-256",
		"x": coord(m.priv.PublicKey.X), "y": coord(m.priv.PublicKey.Y),
		"use": "sig", "alg": "ES256",
	}
}

// pubFromJWK reconstructs an EC P-256 public key from base64url x/y coords.
func pubFromJWK(x, y string) (*ecdsa.PublicKey, error) {
	xb, err := b64u.DecodeString(x)
	if err != nil {
		return nil, err
	}
	yb, err := b64u.DecodeString(y)
	if err != nil {
		return nil, err
	}
	return &ecdsa.PublicKey{Curve: elliptic.P256(),
		X: new(big.Int).SetBytes(xb), Y: new(big.Int).SetBytes(yb)}, nil
}

// contentDigest computes the RFC 9530 SHA-256 Content-Digest header value.
func contentDigest(body []byte) string {
	h := sha256.Sum256(body)
	return "sha-256=:" + base64.StdEncoding.EncodeToString(h[:]) + ":"
}

// signatureBase builds the RFC 9421 signature base: one line per covered
// component, then the @signature-params line.
func signatureBase(components []string, resolve func(string) string, params string) string {
	var b strings.Builder
	for _, c := range components {
		fmt.Fprintf(&b, "\"%s\": %s\n", c, resolve(c))
	}
	fmt.Fprintf(&b, "\"@signature-params\": %s", params)
	return b.String()
}

// signRaw signs msg with ECDSA P-256 over SHA-256, returning fixed-width r||s
// (64 bytes) per RFC 9421 (NOT ASN.1/DER).
func (m *merchantKey) signRaw(msg []byte) []byte {
	h := sha256.Sum256(msg)
	r, s, _ := ecdsa.Sign(rand.Reader, m.priv, h[:])
	out := make([]byte, 64)
	r.FillBytes(out[:32])
	s.FillBytes(out[32:])
	return out
}

// verifyRaw verifies a fixed-width r||s ECDSA P-256 signature.
func verifyRaw(pub *ecdsa.PublicKey, msg, sig []byte) bool {
	if len(sig) != 64 {
		return false
	}
	h := sha256.Sum256(msg)
	r := new(big.Int).SetBytes(sig[:32])
	s := new(big.Int).SetBytes(sig[32:])
	return ecdsa.Verify(pub, h[:], r, s)
}
