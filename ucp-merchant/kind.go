package main

// CounterpartyKind names the class of thing a checkout line item buys.
//
// HOTEL was the IMPLICIT (and only) kind until task #44 added a 2nd real prepaid
// checkout KIND — FOOD_DELIVERY — on the SAME rails (sign → HITL consent →
// integer-cents budget veto → complete_checkout). Only the catalog/pricing/
// line-item layer is generalized; the enforcement core is unchanged.
//
// TRANSPORT / INSURER / ACTIVITY / OTA are deliberately NOT enumerated here: we
// only add a kind when we actually check out against it. Adding a constant we do
// not exercise would be aspirational scaffolding, not a real capability.
type CounterpartyKind string

const (
	KindHotel        CounterpartyKind = "HOTEL"
	KindFoodDelivery CounterpartyKind = "FOOD_DELIVERY"
)
