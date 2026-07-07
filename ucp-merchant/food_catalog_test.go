package main

import (
	"encoding/json"
	"testing"
)

// foodSearch / foodLookup build the args for the FOOD_DELIVERY catalog tools the
// same way a real MCP caller would (kind declared on the query/product).
func foodSearch(q map[string]any) json.RawMessage {
	q["kind"] = string(KindFoodDelivery)
	b, _ := json.Marshal(map[string]any{"query": q})
	return b
}
func foodLookup(p map[string]any) json.RawMessage {
	p["kind"] = string(KindFoodDelivery)
	b, _ := json.Marshal(map[string]any{"product": p})
	return b
}

// TestFoodCatalogLoadsDeterministic — the embedded seed loads, is non-empty, and
// a second unmarshal yields byte-identical row order (var-0: deterministic seed).
func TestFoodCatalogLoadsDeterministic(t *testing.T) {
	if len(foodCatalog) == 0 {
		t.Fatal("food catalog should be non-empty after init()")
	}
	var again []FoodItem
	if err := json.Unmarshal(foodCatalogJSON, &again); err != nil {
		t.Fatalf("re-unmarshal: %v", err)
	}
	if len(again) != len(foodCatalog) {
		t.Fatalf("row count drift: %d vs %d", len(again), len(foodCatalog))
	}
	for i := range again {
		if again[i].ID != foodCatalog[i].ID {
			t.Fatalf("row order drift at %d: %q vs %q", i, again[i].ID, foodCatalog[i].ID)
		}
		// Integer cents only — no float pricing anywhere in the seed.
		if again[i].PriceCents <= 0 || again[i].DeliveryFeeCents < 0 {
			t.Fatalf("row %q has non-positive integer-cents price/fee: %+v", again[i].ID, again[i])
		}
	}
}

func TestFindFood(t *testing.T) {
	if _, ok := findFood("food-tokyo-ramen-midnight"); !ok {
		t.Fatal("known food id should be found")
	}
	if _, ok := findFood("food-does-not-exist"); ok {
		t.Fatal("unknown food id must not be found")
	}
}

// TestFoodSearchByCity — search scopes to a city, returns ONLY real seeded rows,
// carries the simulated-source + honesty disclosure, and totals in integer cents.
func TestFoodSearchByCity(t *testing.T) {
	d, code := foodCatalogTool("search_catalog", foodSearch(map[string]any{"city": "bangkok"}))
	if code != 200 {
		t.Fatalf("search bangkok: want 200, got %d: %v", code, d)
	}
	if d["source"] != "simulated" {
		t.Fatalf("source must be simulated (honesty), got %v", d["source"])
	}
	if d["kind"] != string(KindFoodDelivery) {
		t.Fatalf("kind must be FOOD_DELIVERY, got %v", d["kind"])
	}
	if d["disclosure"] == nil || d["disclosure"] == "" {
		t.Fatal("every food search response must carry the honesty disclosure")
	}
	results := d["results"].([]map[string]any)
	if len(results) == 0 {
		t.Fatal("bangkok should have at least one seeded supper row")
	}
	for _, r := range results {
		if r["city"] != "bangkok" {
			t.Fatalf("city filter leaked a non-bangkok row: %v", r["city"])
		}
		// total_cents must equal price + delivery_fee in integer cents (no float).
		want := r["price_cents"].(int64) + r["delivery_fee_cents"].(int64)
		if r["total_cents"].(int64) != want {
			t.Fatalf("total_cents %v != price+fee %d", r["total_cents"], want)
		}
	}
}

// TestFoodSearchDietFilter — the diet tag narrows results; a vegan query never
// returns a non-vegan row.
func TestFoodSearchDietFilter(t *testing.T) {
	d, _ := foodCatalogTool("search_catalog", foodSearch(map[string]any{"city": "bali", "diet": "vegan"}))
	results := d["results"].([]map[string]any)
	if len(results) == 0 {
		t.Fatal("bali vegan should return the vegan bowl row")
	}
	for _, r := range results {
		diet := r["diet"].([]string)
		found := false
		for _, x := range diet {
			if x == "vegan" {
				found = true
			}
		}
		if !found {
			t.Fatalf("diet filter leaked a non-vegan row: %v", r)
		}
	}
}

// TestFoodSearchMaxCents — the integer-cents max_cents budget filter drops rows
// whose (price+fee) total exceeds the cap.
func TestFoodSearchMaxCents(t *testing.T) {
	d, _ := foodCatalogTool("search_catalog", foodSearch(map[string]any{"city": "tokyo", "max_cents": int64(500)}))
	results := d["results"].([]map[string]any)
	// tokyo ramen is 890+400=1290 > 500 → must be filtered out.
	for _, r := range results {
		if r["total_cents"].(int64) > 500 {
			t.Fatalf("max_cents filter leaked an over-budget row: %v", r["total_cents"])
		}
	}
}

// TestFoodSearchEmptyCityHonest — a city with no seeded row returns count:0 and an
// empty result set (honest "no supper available"); it never fabricates a row.
func TestFoodSearchEmptyCityHonest(t *testing.T) {
	d, code := foodCatalogTool("search_catalog", foodSearch(map[string]any{"city": "reykjavik"}))
	if code != 200 {
		t.Fatalf("empty-city search should still be 200, got %d", code)
	}
	if d["count"].(int) != 0 {
		t.Fatalf("city with no row must return count:0, got %v", d["count"])
	}
	if len(d["results"].([]map[string]any)) != 0 {
		t.Fatal("city with no row must return an empty result set (no fabricated supper)")
	}
}

func TestFoodLookup(t *testing.T) {
	d, code := foodCatalogTool("lookup_catalog", foodLookup(map[string]any{"food_id": "food-singapore-laksa-supper"}))
	if code != 200 || d["available"] != true {
		t.Fatalf("known food lookup should be available: %v", d)
	}
	if d["disclosure"] == nil || d["disclosure"] == "" {
		t.Fatal("food lookup must carry the honesty disclosure")
	}
	f := d["food"].(FoodItem)
	if d["total_cents"].(int64) != f.PriceCents+f.DeliveryFeeCents {
		t.Fatalf("lookup total_cents %v != price+fee", d["total_cents"])
	}

	// Unknown id → 404 not_found, never a fabricated row.
	d2, code2 := foodCatalogTool("lookup_catalog", foodLookup(map[string]any{"food_id": "food-nope"}))
	if code2 != 404 || d2["error"] != "not_found" {
		t.Fatalf("unknown food lookup should be 404 not_found, got %d %v", code2, d2)
	}
}
