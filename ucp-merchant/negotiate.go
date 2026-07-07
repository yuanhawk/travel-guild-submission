package main

// Capability-intersection negotiation (§8.3) wired as the autonomy IAM gate
// (§12). Tier is derived SERVER-SIDE from the authenticated signer's grant — the
// client's requested tier is only an upper bound. Active capability set =
// (agent-declared ∩ merchant ∩ tier-allowed), orphaned extensions pruned.

// RFC9421-001 fix: dev.ucp.sim.control gates sim_*/wallet_* tools so an
// unsigned L1 caller (prod default) cannot reset balances or flip availability.
// L2 and L3 include it so the demo (UCP_UNSIGNED_TIER=L2) and CI continue to
// work without changes. L1 (prod unsigned floor) does NOT include it.
var tierCaps = map[string][]string{
	"L1": {"dev.ucp.shopping.catalog"},
	"L2": {"dev.ucp.shopping.catalog", "dev.ucp.shopping.checkout", "dev.ucp.sim.control"},
	"L3": {"dev.ucp.shopping.catalog", "dev.ucp.shopping.checkout", "dev.ucp.shopping.ap2_mandate", "dev.ucp.sim.control"},
}

var tierRank = map[string]int{"L1": 1, "L2": 2, "L3": 3}

func normTier(t string) string {
	if tierRank[t] == 0 {
		return "L1"
	}
	return t
}

// minTier returns the lower of two tiers (the effective grant ceiling).
func minTier(a, b string) string {
	a, b = normTier(a), normTier(b)
	if tierRank[a] <= tierRank[b] {
		return a
	}
	return b
}

// grantedTier maps an authenticated identity to its server-side tier grant.
// Unsigned / unknown identities get `unsignedTier` (default "L1" = advisory only;
// a deployment MAY raise it to L2 for a trusted local/demo network — NON-PROD).
func grantedTier(grants map[string]string, id *agentIdentity, unsignedTier string) string {
	if id == nil || id.ProfileURL == "" {
		return normTier(unsignedTier)
	}
	if t, ok := grants[id.ProfileURL]; ok {
		return normTier(t)
	}
	return normTier(unsignedTier)
}

func merchantCapIDs() map[string]bool {
	m := map[string]bool{}
	for _, c := range capabilities {
		m[c.ID] = true
	}
	return m
}

// negotiate returns the active capability set. Empty agentCaps (no profile) is
// treated as "declares all merchant caps" so the TIER is the gate. Extensions
// whose parent isn't active are pruned to a fixpoint.
func negotiate(agentCaps []string, tier string) map[string]bool {
	merchant := merchantCapIDs()
	if len(agentCaps) == 0 {
		agentCaps = make([]string, 0, len(merchant))
		for id := range merchant {
			agentCaps = append(agentCaps, id)
		}
	}
	allowed := map[string]bool{}
	for _, c := range tierCaps[normTier(tier)] {
		allowed[c] = true
	}
	active := map[string]bool{}
	for _, c := range agentCaps {
		if merchant[c] && allowed[c] {
			active[c] = true
		}
	}
	for {
		changed := false
		for _, c := range capabilities {
			if c.Extends != "" && active[c.ID] && !active[c.Extends] {
				delete(active, c.ID)
				changed = true
			}
		}
		if !changed {
			break
		}
	}
	return active
}

func toolCapability(tool string) string {
	switch tool {
	case "search_catalog", "lookup_catalog":
		return "dev.ucp.shopping.catalog"
	case "create_checkout", "update_checkout", "complete_checkout", "cancel_checkout":
		return "dev.ucp.shopping.checkout"
	// RFC9421-001 fix: sim_*/wallet_* tools require dev.ucp.sim.control.
	// L1 (prod unsigned default) does NOT include this cap → tools denied.
	// L2/L3 include it → demo + CI (UCP_UNSIGNED_TIER=L2) unaffected.
	case "sim_set_availability", "sim_set_transfer", "sim_get_transfers",
		"sim_set_counterparty", "sim_get_counterparties",
		"wallet_fund", "wallet_get":
		return "dev.ucp.sim.control"
	}
	return ""
}

func activeCapManifest(active map[string]bool) map[string]any {
	full := capabilityManifest()
	out := map[string]any{}
	for id := range active {
		if e, ok := full[id]; ok {
			out[id] = e
		}
	}
	return out
}
