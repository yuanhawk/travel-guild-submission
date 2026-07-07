package main

import (
	_ "embed"
	"encoding/json"
	"net/http"
	"strings"
)

// FoodItem — a single delivery offer in the SIMULATED food-delivery catalog
// (task #44, the 2nd real prepaid checkout KIND). Money is integer cents (UCP
// minor units; no float), exactly like the hotel Hotel row.
//
// HONESTY: inventory here is honestly SIMULATED for the demo — these are OUR OWN
// seeded rows, NOT a live Ele.me / AliCloud feed. The TRANSACTION against them is
// real (RFC 9421 signed → HITL consent → integer-cents budget veto →
// complete_checkout). A city with no row returns an empty result ("no supper
// available"); we never fabricate a row or a price.
type FoodItem struct {
	ID               string   `json:"id"`
	MerchantName     string   `json:"merchant_name"`
	Item             string   `json:"item"`
	City             string   `json:"city"`
	Country          string   `json:"country"`
	PriceCents       int64    `json:"price_cents"`
	DeliveryWindow   string   `json:"delivery_window"`
	Diet             []string `json:"diet"`
	MinOrderCents    int64    `json:"min_order_cents"`
	DeliveryFeeCents int64    `json:"delivery_fee_cents"`
}

// food_catalog.json is a small DETERMINISTIC seed (fixed row order, integer
// cents) embedded at build time — the distroless image has no runtime FS.
//
//go:embed food_catalog.json
var foodCatalogJSON []byte

// foodCatalog is the loaded seed. var-0: deterministic order, integer cents; the
// double-unmarshal test asserts a stable load.
var foodCatalog []FoodItem

func init() {
	if err := json.Unmarshal(foodCatalogJSON, &foodCatalog); err != nil {
		panic("food_catalog.json unmarshal: " + err.Error())
	}
}

func findFood(id string) (FoodItem, bool) {
	for _, f := range foodCatalog {
		if f.ID == id {
			return f, true
		}
	}
	return FoodItem{}, false
}

// foodCatalogTool serves search over the SIMULATED food-delivery catalog. It
// filters by city (required to scope to a destination), optional diet tag, and
// optional delivery_window substring, and returns ONLY real seeded rows with
// integer-cents totals. A city with no row → count:0, empty results (honest "no
// supper available"), never a fabricated row.
//
// Routed from mcp.go when a catalog query carries kind == "FOOD_DELIVERY".
func foodCatalogTool(name string, args json.RawMessage) (map[string]any, int) {
	switch name {
	case "search_catalog":
		var a struct {
			Query struct {
				City           string `json:"city"`
				Diet           string `json:"diet"`
				DeliveryWindow string `json:"delivery_window"`
				MaxCents       int64  `json:"max_cents"`
			} `json:"query"`
		}
		_ = json.Unmarshal(args, &a)
		q := a.Query
		results := []map[string]any{}
		for _, f := range foodCatalog {
			if q.City != "" && !strings.EqualFold(f.City, q.City) {
				continue
			}
			if q.Diet != "" && !foodHasDiet(f, q.Diet) {
				continue
			}
			if q.DeliveryWindow != "" && !strings.Contains(f.DeliveryWindow, q.DeliveryWindow) {
				continue
			}
			// Integer-cents total = item price + delivery fee (no float).
			total := f.PriceCents + f.DeliveryFeeCents
			if q.MaxCents > 0 && total > q.MaxCents {
				continue
			}
			results = append(results, map[string]any{
				"food_id": f.ID, "merchant_name": f.MerchantName, "item": f.Item,
				"city": f.City, "country": f.Country,
				"price_cents": f.PriceCents, "delivery_fee_cents": f.DeliveryFeeCents,
				"total_cents": total, "currency": "USD",
				"delivery_window": f.DeliveryWindow, "diet": f.Diet,
				"min_order_cents": f.MinOrderCents,
			})
		}
		return map[string]any{
			"source": "simulated", "kind": string(KindFoodDelivery),
			"count": len(results), "results": results,
			// HONESTY disclosure carried on every food search response.
			"disclosure": "Food-delivery inventory is honestly simulated for the demo; the transaction is a real UCP prepaid checkout (RFC 9421 signed, HITL consent, integer-cents budget veto). NOT a live Ele.me integration; no Ele.me/AliCloud partnership claimed.",
		}, http.StatusOK

	case "lookup_catalog":
		var a struct {
			Product struct {
				FoodID string `json:"food_id"`
			} `json:"product"`
		}
		_ = json.Unmarshal(args, &a)
		f, ok := findFood(a.Product.FoodID)
		if !ok {
			return map[string]any{"error": "not_found", "food_id": a.Product.FoodID}, http.StatusNotFound
		}
		return map[string]any{
			"food":        f,
			"available":   true,
			"source":      "simulated",
			"kind":        string(KindFoodDelivery),
			"total_cents": f.PriceCents + f.DeliveryFeeCents,
			"currency":    "USD",
			"disclosure":  "Food-delivery inventory is honestly simulated for the demo; the transaction is a real UCP prepaid checkout (RFC 9421 signed, HITL consent, integer-cents budget veto). NOT a live Ele.me integration; no Ele.me/AliCloud partnership claimed.",
		}, http.StatusOK
	}
	return map[string]any{"error": "unknown food catalog tool: " + name}, http.StatusBadRequest
}

func foodHasDiet(f FoodItem, diet string) bool {
	for _, d := range f.Diet {
		if strings.EqualFold(d, diet) {
			return true
		}
	}
	return false
}
