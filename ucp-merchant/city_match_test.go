package main

import "testing"

// #70: tolerant city match must fix the "X" vs "X city" gap WITHOUT false positives.
func TestCityMatches(t *testing.T) {
	cases := []struct {
		hotelCity, query string
		want             bool
	}{
		{"cebu city", "cebu city", true}, // exact
		{"cebu city", "cebu", true},      // the bug: common name -> "X city"
		{"cebu", "cebu city", true},      // symmetric
		{"Cebu City", "cebu", true},      // case-insensitive
		{"ho chi minh city", "ho chi minh", true},
		{"quezon city", "quezon", true},
		{"manila", "manila", true},
		{"puerto princesa", "puerto princesa", true},
		// R1 fix (#236/#240): the old "penang"->"george town" hardcoded alias was a FALSE
		// premise (none of the 18 "george town" rows are Penang) and has been removed, not
		// replaced. These must NOT match any more.
		{"george town", "penang", false},
		{"penang", "george town", false},
		// Cross-country "X"/"X city" homonyms confirmed live-cross-wiring (#236) are covered by
		// TestCityMatchesCrossCountryHomonym below (against a synthetic catalog snapshot — the
		// real conflicting rows live in catalog_supplement.json, not yet embedded here).
		// NO false positives:
		{"new york", "york", false},     // "york" is not a leading word of "new york"
		{"york", "new york", false},     // nor vice-versa
		{"cebu city", "manila", false},  // different city
		{"san diego", "san francisco", false},
		{"cebu city", "ceb", false}, // partial token, not a whole word
		// collision-negatives: the OLD broad leading-word rule matched these (274-pair bug).
		// The strict " city"-suffix bridge must REJECT them:
		{"ho chi minh city", "ho", false},  // Ghana 'Ho' must NOT pull Vietnam
		{"ho chi minh", "ho", false},
		{"new york city", "new", false},
		{"panama city", "panama", true},    // legit "X city" bridge still works
		{"panama city", "pan", false},      // but a partial token does not
	}
	for _, c := range cases {
		if got := cityMatches(c.hotelCity, c.query); got != c.want {
			t.Errorf("cityMatches(%q,%q)=%v, want %v", c.hotelCity, c.query, got, c.want)
		}
	}
}

// TestCityMatchesCrossCountryHomonym (#236/#240) directly exercises the same-country
// guard against a SYNTHETIC catalog snapshot reproducing the confirmed cross-wiring:
// "mexico" (Philippines, Pampanga) <-> "mexico city" (actual Mexico), and "san carlos"
// (Venezuela) <-> "san carlos city" (Philippines). This is synthetic rather than reading
// the embedded catalog.json because the live conflicting rows currently only exist in
// catalog_supplement.json (not yet embedded in the Go binary) — this test proves the
// LOGIC rejects a genuine same-name/different-country pair whenever the data DOES
// contain one, independent of catalog.json's present-day contents.
func TestCityMatchesCrossCountryHomonym(t *testing.T) {
	saved := cityCountries
	defer func() { cityCountries = saved }()
	cityCountries = map[string]map[string]bool{
		"mexico":          {"Philippines": true},
		"mexico city":     {"Mexico": true},
		"san carlos":      {"Venezuela": true},
		"san carlos city": {"Philippines": true},
		"cebu city":       {"Philippines": true}, // "cebu" bare has no rows of its own (mirrors reality)
		"quezon":          {"Philippines": true},
		"quezon city":     {"Philippines": true},
	}
	cases := []struct {
		hotelCity, query string
		want             bool
	}{
		{"mexico", "mexico city", false},          // genuine cross-country homonym -> blocked
		{"mexico city", "mexico", false},
		{"san carlos", "san carlos city", false},  // genuine cross-country homonym -> blocked
		{"san carlos city", "san carlos", false},
		{"cebu city", "cebu", true},                // "cebu" has no rows of its own -> not a real
		                                             // homonym conflict, bridge still stands
		{"quezon city", "quezon", true},             // both sides same country -> bridge stands
	}
	for _, c := range cases {
		if got := cityMatches(c.hotelCity, c.query); got != c.want {
			t.Errorf("cityMatches(%q,%q)=%v, want %v", c.hotelCity, c.query, got, c.want)
		}
	}
}
