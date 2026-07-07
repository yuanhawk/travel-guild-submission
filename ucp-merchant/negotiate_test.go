package main

import "testing"

func TestNegotiateTiers(t *testing.T) {
	l1 := negotiate(nil, "L1")
	if !l1["dev.ucp.shopping.catalog"] || l1["dev.ucp.shopping.checkout"] {
		t.Fatalf("L1 should be catalog-only: %v", l1)
	}
	l2 := negotiate(nil, "L2")
	if !l2["dev.ucp.shopping.checkout"] || l2["dev.ucp.shopping.ap2_mandate"] {
		t.Fatalf("L2 should be catalog+checkout, no ap2: %v", l2)
	}
	l3 := negotiate(nil, "L3")
	if !l3["dev.ucp.shopping.checkout"] || !l3["dev.ucp.shopping.ap2_mandate"] {
		t.Fatalf("L3 should include checkout+ap2: %v", l3)
	}
}

func TestExtensionPruning(t *testing.T) {
	a := negotiate([]string{"dev.ucp.shopping.ap2_mandate"}, "L3")
	if a["dev.ucp.shopping.ap2_mandate"] {
		t.Fatalf("ap2 should be pruned without active checkout parent: %v", a)
	}
}

func TestMinTierAndGrant(t *testing.T) {
	if minTier("L3", "L1") != "L1" || minTier("L2", "L3") != "L2" || minTier("", "L3") != "L1" {
		t.Fatal("minTier wrong")
	}
	grants := map[string]string{"https://a/.well-known/ucp": "L3"}
	if grantedTier(grants, &agentIdentity{ProfileURL: "https://a/.well-known/ucp"}, "L1") != "L3" {
		t.Fatal("granted L3 not returned")
	}
	if grantedTier(grants, nil, "L1") != "L1" {
		t.Fatal("nil identity should default to unsigned floor L1")
	}
	if grantedTier(grants, &agentIdentity{ProfileURL: "https://unknown/"}, "L1") != "L1" {
		t.Fatal("unknown identity should be unsigned floor L1")
	}
	// NON-PROD demo: unsigned floor raised to L2.
	if grantedTier(grants, nil, "L2") != "L2" {
		t.Fatal("unsigned floor L2 not honored")
	}
}

func TestToolCapability(t *testing.T) {
	cases := map[string]string{
		"search_catalog": "dev.ucp.shopping.catalog", "lookup_catalog": "dev.ucp.shopping.catalog",
		"create_checkout": "dev.ucp.shopping.checkout", "complete_checkout": "dev.ucp.shopping.checkout",
		"unknown": "",
	}
	for tool, want := range cases {
		if got := toolCapability(tool); got != want {
			t.Errorf("toolCapability(%q)=%q want %q", tool, got, want)
		}
	}
}
