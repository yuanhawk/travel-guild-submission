package main

// catalog_provider_test.go — coverage for the SEEDED↔LIVE CATALOG edition-swap
// seam (catalog_provider.go), which had no dedicated test. These tests are
// ADDITIVE and assert the var-0 firewall: under the default/UAT edition the
// active provider is the byte-identical seeded pass-through and a live
// registration is a NO-OP; only TG_EDITION=prod honours a swap.

import (
	"reflect"
	"testing"
)

// fakeCatalogProvider is a throwaway stand-in for the (absent) private prod
// LiveCatalogProvider — just enough to prove RegisterCatalogProvider swaps it in
// under prod. It returns a single sentinel row distinguishable from mockCatalog.
type fakeCatalogProvider struct{}

func (fakeCatalogProvider) Lookup(id string) (Hotel, bool) {
	if id == "fake-1" {
		return Hotel{ID: "fake-1", Title: "Fake", PriceCentsNight: 999}, true
	}
	return Hotel{}, false
}

func (fakeCatalogProvider) All() []Hotel {
	return []Hotel{{ID: "fake-1", Title: "Fake", PriceCentsNight: 999}}
}

// restoreSeededProvider resets the package global back to the seeded default so a
// prod-swap test never leaks the fake provider into sibling tests.
func restoreSeededProvider() { activeCatalogProvider = SeededCatalogProvider{} }

// --------------------------------------------------------------------------
// Default provider == SeededCatalogProvider, byte-identical to mockCatalog.
// --------------------------------------------------------------------------

func TestActiveCatalogProviderDefaultsToSeeded(t *testing.T) {
	if _, ok := activeCatalogProvider.(SeededCatalogProvider); !ok {
		t.Fatalf("default activeCatalogProvider = %T, want SeededCatalogProvider", activeCatalogProvider)
	}
}

func TestSeededProviderAllIsByteIdenticalPassThrough(t *testing.T) {
	got := SeededCatalogProvider{}.All()
	// Same length, same order, same integer-cents — a true pass-through over the
	// embedded slice. reflect.DeepEqual catches any reordering or cents drift.
	if !reflect.DeepEqual(got, mockCatalog) {
		t.Fatalf("SeededCatalogProvider.All() is not byte-identical to mockCatalog")
	}
	if len(mockCatalog) == 0 {
		t.Fatalf("mockCatalog is empty — the seeded catalog must be embedded")
	}
	for i := range got {
		if got[i].ID != mockCatalog[i].ID || got[i].PriceCentsNight != mockCatalog[i].PriceCentsNight {
			t.Fatalf("row %d differs: got {%s,%d} want {%s,%d}",
				i, got[i].ID, got[i].PriceCentsNight, mockCatalog[i].ID, mockCatalog[i].PriceCentsNight)
		}
	}
}

func TestSeededProviderLookupIsByteIdenticalPassThrough(t *testing.T) {
	if len(mockCatalog) == 0 {
		t.Fatalf("mockCatalog is empty")
	}
	// Every embedded id resolves to the EXACT same row (same cents).
	for _, want := range mockCatalog {
		got, ok := SeededCatalogProvider{}.Lookup(want.ID)
		if !ok {
			t.Fatalf("Lookup(%q) not found in seeded catalog", want.ID)
		}
		if !reflect.DeepEqual(got, want) {
			t.Fatalf("Lookup(%q) returned a non-identical row", want.ID)
		}
		if got.PriceCentsNight != want.PriceCentsNight {
			t.Fatalf("Lookup(%q) cents = %d, want %d", want.ID, got.PriceCentsNight, want.PriceCentsNight)
		}
	}
	// An unknown id is a clean miss (mirrors the original findHotel scan).
	if _, ok := (SeededCatalogProvider{}).Lookup("definitely-not-a-real-id"); ok {
		t.Fatalf("Lookup of unknown id unexpectedly returned ok=true")
	}
}

// --------------------------------------------------------------------------
// Edition gating: currentEdition / isProdEdition.
// --------------------------------------------------------------------------

func TestCurrentEditionDefaultsToUAT(t *testing.T) {
	t.Setenv(editionEnv, "")
	if got := currentEdition(); got != "uat" {
		t.Fatalf("currentEdition() with empty env = %q, want uat", got)
	}
	if isProdEdition() {
		t.Fatalf("isProdEdition() true under empty (uat) edition")
	}
}

func TestCurrentEditionNonProdFailsSafeToUATCaseAndWhitespace(t *testing.T) {
	for _, raw := range []string{"uat", "UAT", "staging", "garbage", "  ", "prodd"} {
		t.Setenv(editionEnv, raw)
		if isProdEdition() {
			t.Fatalf("isProdEdition() true for non-prod edition %q", raw)
		}
	}
}

func TestIsProdEditionTrueOnlyForExactProd(t *testing.T) {
	for _, raw := range []string{"prod", "PROD", "  Prod  "} {
		t.Setenv(editionEnv, raw)
		if currentEdition() != "prod" {
			t.Fatalf("currentEdition() for %q = %q, want prod", raw, currentEdition())
		}
		if !isProdEdition() {
			t.Fatalf("isProdEdition() false for %q", raw)
		}
	}
}

// --------------------------------------------------------------------------
// RegisterCatalogProvider: NO-OP under UAT, swaps only under prod, ignores nil.
// --------------------------------------------------------------------------

func TestRegisterCatalogProviderIsNoOpUnderUAT(t *testing.T) {
	t.Setenv(editionEnv, "uat")
	defer restoreSeededProvider()

	RegisterCatalogProvider(fakeCatalogProvider{})
	if _, ok := activeCatalogProvider.(SeededCatalogProvider); !ok {
		t.Fatalf("UAT edition: registration mutated provider to %T (want seeded no-op)", activeCatalogProvider)
	}
}

func TestRegisterCatalogProviderIgnoresNil(t *testing.T) {
	// Even under prod, a nil provider must be ignored (no swap to a nil source).
	t.Setenv(editionEnv, "prod")
	defer restoreSeededProvider()

	RegisterCatalogProvider(nil)
	if _, ok := activeCatalogProvider.(SeededCatalogProvider); !ok {
		t.Fatalf("nil registration mutated provider to %T (want unchanged seeded)", activeCatalogProvider)
	}
}

func TestRegisterCatalogProviderSwapsUnderProd(t *testing.T) {
	t.Setenv(editionEnv, "prod")
	defer restoreSeededProvider()

	// Sanity: still seeded before the prod swap.
	if _, ok := activeCatalogProvider.(SeededCatalogProvider); !ok {
		t.Fatalf("pre-swap provider = %T, want seeded", activeCatalogProvider)
	}

	RegisterCatalogProvider(fakeCatalogProvider{})
	if _, ok := activeCatalogProvider.(fakeCatalogProvider); !ok {
		t.Fatalf("prod edition: provider = %T, want fakeCatalogProvider after registration", activeCatalogProvider)
	}
	// The swapped provider is genuinely active (live row reachable through it).
	if h, ok := activeCatalogProvider.Lookup("fake-1"); !ok || h.PriceCentsNight != 999 {
		t.Fatalf("swapped provider Lookup did not return the fake row")
	}

	// And the default restores to the byte-identical seeded pass-through.
	restoreSeededProvider()
	if !reflect.DeepEqual(activeCatalogProvider.All(), mockCatalog) {
		t.Fatalf("after restore, provider is not the byte-identical seeded catalog")
	}
}
