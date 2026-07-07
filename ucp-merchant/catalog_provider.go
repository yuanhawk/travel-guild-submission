package main

// catalog_provider.go — the SEEDED↔LIVE CATALOG data-source seam (Go side).
//
// WHY THIS FILE EXISTS
// --------------------
// The public submission edition (UAT) and the private prod edition share the
// SAME merchant CODE (budget veto, wallet, signing, checkout) but differ only in
// the catalog/price DATA the merchant reads. UAT reads the byte-deterministic
// seeded rows embedded from catalog.json; prod swaps in a live best-price /
// availability catalog. This is the Go mirror of the Python provider seam in
// society/providers (PriceProvider + SeededProvider).
//
// CatalogProvider is the swappable data source. The merchant fetches every
// catalog/price row through the package-level active provider, so prod can plug
// in a LiveCatalogProvider WITHOUT touching merchant logic.
//
// THE VARIANCE-0 (var-0) CONTRACT
// -------------------------------
// The seeded merchant MUST stay byte-identical: the integer-cents budget veto
// CONSUMES PriceCentsNight, so any perturbation of the seeded prices/order would
// break determinism. The default provider is SeededCatalogProvider, which is a
// thin pass-through over the embedded mockCatalog slice — same rows, same order,
// same prices. RegisterCatalogProvider is, by construction, a NO-OP unless the
// edition is explicitly "prod" (see below), so even if a live provider were
// linked into a UAT binary it could not perturb the seeded path.
//
// GO EDITION SEAM (no dynamic import)
// -----------------------------------
// Unlike Python, Go cannot dynamically import another repo's module at runtime.
// The idiomatic seam is therefore a package-level default + a registration hook:
//
//	  default  : activeCatalogProvider = SeededCatalogProvider{}   (this repo)
//	  prod swap: the prod binary's main() calls
//	             RegisterCatalogProvider(NewLiveCatalogProvider(...))
//
// The live implementation is intentionally ABSENT from this public repo — there
// is NO live-fetching / network code here. The prod LiveCatalogProvider lives in
// the private prod module and satisfies the CatalogProvider contract below.

import (
	"os"
	"strings"
)

// CatalogProvider is the data-source seam the merchant reads all catalog/price
// rows through. It mirrors the internal call shapes the merchant already used
// against the embedded catalog:
//
//   - Lookup(id) (Hotel, bool)  ← was the body of findHotel(id)
//   - All() []Hotel             ← was a direct `range mockCatalog` in search
//
// Money is integer cents on the Hotel row (UCP minor units; no float). The prod
// LiveCatalogProvider returns rows whose PriceCentsNight is a live fare; the
// seeded provider returns the deterministic embedded prices unchanged.
type CatalogProvider interface {
	// Lookup returns the catalog row for a hotel id (the budget-veto / checkout
	// price lookup). The bool is false when the id is not in the catalog.
	Lookup(id string) (Hotel, bool)

	// All returns the full catalog in a STABLE order (the search/iteration path).
	// The seeded provider returns the embedded slice as-is; a prod provider MUST
	// return a deterministically-ordered snapshot so search output is stable.
	All() []Hotel
}

// SeededCatalogProvider is the UAT default: a pure pass-through over the embedded
// mockCatalog. It preserves the CURRENT seeded behaviour EXACTLY — same rows,
// same order, same integer-cents prices — so it can never perturb var-0. No I/O,
// no network, no wall-clock.
type SeededCatalogProvider struct{}

// Lookup is byte-identical to the original findHotel linear scan over mockCatalog.
func (SeededCatalogProvider) Lookup(id string) (Hotel, bool) {
	for _, h := range mockCatalog {
		if h.ID == id {
			return h, true
		}
	}
	return Hotel{}, false
}

// All returns the embedded catalog slice in its loaded (byte-deterministic) order.
func (SeededCatalogProvider) All() []Hotel {
	return mockCatalog
}

// activeCatalogProvider is the process-wide catalog data source. It defaults to
// the seeded pass-through so the public/seeded merchant is byte-identical to
// today. The prod binary installs its LiveCatalogProvider via
// RegisterCatalogProvider during startup.
var activeCatalogProvider CatalogProvider = SeededCatalogProvider{}

// Edition env knob (NAMES only — never a secret). Mirrors the Python seam's
// TG_EDITION. Default "uat" → seeded; only "prod" honours a live registration.
const (
	editionEnv     = "TG_EDITION"
	defaultEdition = "uat"
)

// currentEdition returns the active edition tag, lower-cased. Default "uat".
// Any value other than "prod" is treated as "uat" (fail-safe to seeded).
func currentEdition() string {
	v := strings.ToLower(strings.TrimSpace(os.Getenv(editionEnv)))
	if v == "" {
		return defaultEdition
	}
	return v
}

// isProdEdition is true iff TG_EDITION is explicitly "prod".
func isProdEdition() bool { return currentEdition() == "prod" }

// RegisterCatalogProvider installs a live catalog data source. This is the prod
// swap hook: the private prod binary calls it during startup with its
// LiveCatalogProvider.
//
// VAR-0 FAIL-SAFE: registration is a NO-OP unless the edition is explicitly
// "prod". In the public/UAT edition the active provider therefore STAYS
// SeededCatalogProvider no matter what, so the byte-deterministic seeded path
// (and its integer-cents budget veto) cannot be perturbed. A nil provider is
// also ignored.
func RegisterCatalogProvider(p CatalogProvider) {
	if p == nil || !isProdEdition() {
		return
	}
	activeCatalogProvider = p
}
