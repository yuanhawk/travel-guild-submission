package main

import (
	_ "embed"
	"encoding/json"
	"net/http"
	"strings"
	"time"
)

// Hotel — mock catalog row. Money is integer cents (UCP minor units; no float).
// Replaced by PolarDB `hotels` + Tair live price (Phase 1).
type Hotel struct {
	ID              string   `json:"id"`
	Title           string   `json:"title"`
	City            string   `json:"city"`
	Country         string   `json:"country"`
	StarRating      float64  `json:"star_rating"`
	ReviewScore     float64  `json:"review_score"`
	MetroAccess     bool     `json:"metro_access"`
	TransportScore  float64  `json:"transport_score"`
	PriceCentsNight int64    `json:"price_cents_per_night"`
	Amenities       []string `json:"amenities"`
	// SEV-2: MaxOccupancy is the maximum number of adults per room.
	// 0 means unchecked (legacy rows). Per-adult pricing deferred (see SPEC-NOTES.md).
	MaxOccupancy    int      `json:"max_occupancy"`
}

// Canonical mock catalog — IDs + city scheme MUST match mcp_services/travel_memory's
// store seed so a recommended hotel is bookable here. Unified via PolarDB (Phase 2).
// MaxOccupancy rules: 5-star→4, 4-star→4, 3-star→3, 2-star (hostel)→6.
//go:embed catalog.json
var catalogJSON []byte

// mockCatalog loads from the shared catalog.json — the SINGLE source of truth for the
// Go merchant AND the Python travel_memory store (test_catalog_parity asserts agreement).
// Embedded at build time (the distroless image has no runtime filesystem). Rows carry
// real-climatology / named-hotel data; generated rows are provenance-tagged.
var mockCatalog []Hotel

func init() {
	if err := json.Unmarshal(catalogJSON, &mockCatalog); err != nil {
		panic("catalog.json unmarshal: " + err.Error())
	}
	cityCountries = buildCityCountries(mockCatalog)
}

func nights(checkin, checkout string) int {
	const f = "2006-01-02"
	ci, e1 := time.Parse(f, checkin)
	co, e2 := time.Parse(f, checkout)
	if e1 != nil || e2 != nil {
		return 1
	}
	n := int(co.Sub(ci).Hours() / 24)
	if n < 1 {
		return 1
	}
	return n
}

// findHotel resolves a hotel id through the active CatalogProvider (the data-source
// seam). UAT defaults to SeededCatalogProvider, whose Lookup is byte-identical to
// the original linear scan over mockCatalog; prod swaps in a live provider.
func findHotel(id string) (Hotel, bool) {
	return activeCatalogProvider.Lookup(id)
}

// catalogTool serves search_catalog + lookup_catalog (cap dev.ucp.shopping.catalog).
// This is the legacy variant with no sim awareness (used by old test paths).
func catalogTool(name string, args json.RawMessage) (map[string]any, int) {
	return catalogToolWithStore(nil, name, args)
}

// catalogToolWithStore is the sim-aware dispatch used by the live MCP handler.
// When st==nil, behaves identically to the original catalogTool (backward-compat).
// cityMatches tolerates the common "X" vs "X city" naming gap so a query like "cebu"
// matches the catalog city "cebu city" (and "ho chi minh" -> "ho chi minh city",
// "quezon" -> "quezon city"). It accepts an exact case-insensitive match OR a strict
// trailing-" city"-token bridge — NOT a broad prefix (which falsely collided 274 pairs,
// e.g. "ho" -> "ho chi minh city").
//
// R1 fix (city->country audit #236/#240): the old cityCanonical hardcoded alias
// map ("penang" -> "george town") was built on a FALSE premise — none of the 18
// "george town" rows are actually Penang (8 are George, South Africa; 2 Exuma,
// Bahamas; 8 Georgetown, Guyana). It has been REMOVED, not replaced — Penang
// lodging discovery is a separate data-completeness gap, out of scope here.
//
// The "X"/"X city" bridge itself was ALSO confirmed live-cross-wiring: it matched
// regardless of country, so "mexico" (Philippines, Pampanga) <-> "mexico city"
// (actual Mexico) and "san carlos" (Venezuela) <-> "san carlos city" (Philippines)
// silently bridged across two different countries. The bridge now only fires when
// the two city-name forms share at least one country IN THE CATALOG (built once
// at init from every hotel's own city/country field — see cityCountries below);
// unknown-either-side is conservative (no bridge), matching the "never silently
// wrong" posture used elsewhere in this codebase.
func canonCity(s string) string {
	return strings.ToLower(strings.TrimSpace(s))
}

// cityCountries maps a canonicalized bare city name -> the set of countries
// (verbatim catalog `country` values) that name appears under anywhere in the
// active catalog. Built once from mockCatalog (the embedded catalog.json) at
// init — city/country pairs are static; only per-hotel availability is dynamic
// (that lives in activeCatalogProvider, not here).
var cityCountries map[string]map[string]bool

func buildCityCountries(hotels []Hotel) map[string]map[string]bool {
	out := map[string]map[string]bool{}
	for _, h := range hotels {
		c := canonCity(h.City)
		if c == "" {
			continue
		}
		if out[c] == nil {
			out[c] = map[string]bool{}
		}
		out[c][h.Country] = true
	}
	return out
}

// sameCountry reports whether it is safe to bridge "X" <-> "X city" for these two
// (already-canonicalized) names. If EITHER side has no rows of its own in the
// catalog, there is no genuine homonym to disambiguate (nothing competes with the
// bridge target) — allow it, exactly as before this fix. Only when BOTH sides
// have their own catalog rows AND those rows never share a country is this the
// confirmed cross-wiring bug class (e.g. "mexico"=Philippines vs "mexico
// city"=Mexico) — block it.
func sameCountry(a, b string) bool {
	as, bs := cityCountries[a], cityCountries[b]
	if len(as) == 0 || len(bs) == 0 {
		return true
	}
	for country := range as {
		if bs[country] {
			return true
		}
	}
	return false
}

func cityMatches(hotelCity, query string) bool {
	h := canonCity(hotelCity)
	q := canonCity(query)
	if h == q {
		return true
	}
	// Bridge ONLY the "X" <-> "X city" naming (cebu <-> cebu city, ho chi minh <-> ho chi
	// minh city), and ONLY within the same country — see the fix note above.
	if h == q+" city" || q == h+" city" {
		return sameCountry(h, q)
	}
	return false
}

func catalogToolWithStore(st *store, name string, args json.RawMessage) (map[string]any, int) {
	switch name {
	case "search_catalog":
		var a struct {
			Query struct {
				City      string `json:"city"`
				Checkin   string `json:"checkin"`
				Checkout  string `json:"checkout"`
				Adults    int    `json:"adults"`
				MaxCents  int64  `json:"max_cents"`
			} `json:"query"`
		}
		_ = json.Unmarshal(args, &a)
		q := a.Query
		n := nights(q.Checkin, q.Checkout)
		results := []map[string]any{}
		cityAvail := 0 // #70: city-matching + available rows BEFORE the price filter — lets the
		// agent distinguish a genuine inventory gap (0) from an over-budget shortfall (>0)
		// without a second search call.
		// Iterate the active CatalogProvider (data-source seam). UAT's
		// SeededCatalogProvider returns the embedded mockCatalog slice as-is →
		// byte-identical order; prod returns its live snapshot.
		if q.City == "" {
			// No city filter would otherwise iterate the entire catalog unfiltered —
			// a full-dataset dump via one call. Every real caller (orchestrator agents,
			// food-delivery search) always supplies a city; treat a blank city as
			// "no match" rather than "match everything".
			return map[string]any{"source": "mock", "nights": n, "count": 0,
				"city_available_count": 0, "results": results}, http.StatusOK
		}
		for _, h := range activeCatalogProvider.All() {
			if !cityMatches(h.City, q.City) {
				continue
			}
			// L3 sim: exclude sold-out hotels from search results.
			// Agents see the same catalog state the simulator controls.
			if st != nil && !st.simIsAvailable(h.ID) {
				continue
			}
			cityAvail++
			total := h.PriceCentsNight * int64(n)
			if q.MaxCents > 0 && total > q.MaxCents {
				continue
			}
			results = append(results, map[string]any{
				"hotel_id": h.ID, "title": h.Title, "city": h.City, "country": h.Country,
				"star_rating": h.StarRating, "review_score": h.ReviewScore,
				"transport_score": h.TransportScore, "metro_access": h.MetroAccess,
				"nights": n, "total_cents": total, "currency": "USD", "amenities": h.Amenities,
			})
		}
		return map[string]any{"source": "mock", "nights": n, "count": len(results),
			"city_available_count": cityAvail, "results": results}, http.StatusOK

	case "lookup_catalog":
		var a struct {
			Product struct {
				HotelID  string `json:"hotel_id"`
				Checkin  string `json:"checkin"`
				Checkout string `json:"checkout"`
				Adults   int    `json:"adults"`
			} `json:"product"`
		}
		_ = json.Unmarshal(args, &a)
		h, ok := findHotel(a.Product.HotelID)
		if !ok {
			return map[string]any{"error": "not_found", "hotel_id": a.Product.HotelID}, http.StatusNotFound
		}
		// L3 sim: return status:unavailable for sold-out hotels.
		if st != nil && !st.simIsAvailable(h.ID) {
			return map[string]any{
				"status":   "unavailable",
				"hotel_id": h.ID,
				"title":    h.Title,
				"source":   "mock",
			}, http.StatusConflict
		}
		out := map[string]any{"hotel": h, "available": true, "source": "mock"}
		if a.Product.Checkin != "" && a.Product.Checkout != "" {
			n := nights(a.Product.Checkin, a.Product.Checkout)
			out["nights"] = n
			out["total_cents"] = h.PriceCentsNight * int64(n)
			out["currency"] = "USD"
		}
		return out, http.StatusOK
	}
	return map[string]any{"error": "unknown catalog tool: " + name}, http.StatusBadRequest
}
