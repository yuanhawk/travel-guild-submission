"""
line_item_assembler.py — PHASE-0 Budget line-item ASSEMBLER (§19.5, §16.3).

Design contract: AGENT-SOCIETY-A2A-DESIGN.md §19.5 ("Line-item ASSEMBLER owner
= Budget. When multiple agents emit fee line items (Compliance visa, Health
vaccine, Insurance premium) into ONE checkout/mandate, Budget deterministically
assembles + orders the final line_items (idempotent, keyed) before
create_checkout"), §16.3 (money-path cascade lesson — re-emission must not
double-charge), §17 ("Budget owns the money LIFECYCLE ... visa/permit FEES as
line items").

THE INVARIANT THIS PROTECTS
---------------------------
Several specialist agents independently emit fee line items into the SINGLE
checkout/mandate (the §0.2 one-package-one-mandate flow). Without a single
assembler:
  - ordering would be non-deterministic (variance in the assembled package),
  - a re-emitted fee (a re-run leg, a retried agent) would DOUBLE-CHARGE.

This module is Budget's deterministic, idempotent assembler. Each fee is keyed
by ``(source, kind, leg)``. Re-adding the same key REPLACES (never duplicates)
the prior entry — so re-emission can't double-add (mirrors the §16.3 cascade
lesson at the FEE level). The final ordering is a stable, deterministic sort, so
the assembled ``line_items`` is byte-identical given the same set of fees
regardless of insertion order or agent timing.

OUTPUT SHAPE
------------
The assembler produces the merchant's ``line_items`` shape (checkout.go keys by
``hotel_id``). LODGING items carry ``hotel_id`` + dates. FEE items (visa /
vaccine / premium) carry a synthetic ``hotel_id`` of the form
``fee:<source>:<kind>:<leg>`` so they are addressable + idempotent, plus the
canonical contracts.make_money() currency tag (Budget reads ``usd_cents``). The
merchant currently prices by catalog ``hotel_id``; whether a deployment maps fee
ids to catalog rows or sums fee usd_cents directly is a Budget-agent concern —
the ASSEMBLER's job (Phase-0) is the deterministic + idempotent ASSEMBLY + the
stable key, not the merchant wire. Domain logic (what a visa costs) stays in the
future agents.

Determinism: pure Python, closed kind-set, stable sort → zero variance.
"""

from __future__ import annotations

from enum import Enum
from typing import Any

from core.contracts import is_valid_money
from core.cost_basis import BASIS_DETERMINISTIC_ESTIMATE, make_basis

# ---------------------------------------------------------------------------
# Closed set of fee KINDs (the assembler's deterministic ordering domain).
# Lodging is the base; fees are appended in this canonical kind order, then by
# leg, then by source — a fully deterministic total order.
# ---------------------------------------------------------------------------


class LineItemKind(str, Enum):
    """Closed set of line-item kinds an agent may emit into the package."""

    LODGING = "lodging"      # Accommodation — the base catalog item (hotel_id)
    VISA = "visa"            # Compliance — eVisa/permit fee
    VACCINE = "vaccine"      # Health — required-vaccine upfront cost
    PREMIUM = "premium"      # Insurance — coverage premium
    TRANSPORT = "transport"  # Transport — inter-leg movement fee
    PERMIT = "permit"        # Compliance — IDP / other permit fee


# Canonical kind ordering for the assembled package (lodging first, then fees in
# a stable, documented order). Lower index sorts first.
_KIND_ORDER: dict[str, int] = {
    LineItemKind.LODGING.value: 0,
    LineItemKind.TRANSPORT.value: 1,
    LineItemKind.VISA.value: 2,
    LineItemKind.PERMIT.value: 3,
    LineItemKind.VACCINE.value: 4,
    LineItemKind.PREMIUM.value: 5,
}

_KIND_VALUES = frozenset(k.value for k in LineItemKind)

# Sentinel leg key for package-level (non-per-leg) fees, e.g. a single trip
# insurance premium. Sorts deterministically after numbered legs.
PACKAGE_LEG = "_package"


def _fee_key(source: str, kind: str, leg: str) -> tuple[str, str, str]:
    """The idempotency key for a fee: (source, kind, leg)."""
    return (str(source), str(kind), str(leg))


class LineItemAssembler:
    """
    Budget's deterministic, idempotent line-item assembler.

    Usage:
        a = LineItemAssembler()
        a.add_lodging(hotel_id="bali-alaya-ubud", leg="leg-0",
                      checkin="2026-10-01", checkout="2026-10-03", adults=1)
        a.add_fee(source="compliance", kind="visa", leg="leg-0",
                  money=fx_provider.normalize(6200, "USD").to_money(),
                  description="Ethiopia eVisa single-entry")
        items = a.assemble()   # deterministic, idempotent line_items

    Re-calling add_fee/add_lodging with the SAME key REPLACES the entry (never
    duplicates) → re-emission can't double-charge.
    """

    def __init__(self) -> None:
        # key -> entry. Insertion-order-independent: assemble() sorts.
        self._entries: dict[tuple[str, str, str], dict[str, Any]] = {}

    # -- lodging (the base catalog item) -----------------------------------

    def add_lodging(
        self,
        *,
        hotel_id: str,
        leg: str,
        checkin: str,
        checkout: str,
        adults: int = 1,
        source: str = "accommodation",
        basis: str = BASIS_DETERMINISTIC_ESTIMATE,
    ) -> None:
        """
        Add (or idempotently replace) a lodging line item for a leg.

        Keyed by (source, "lodging", leg). The merchant prices it by hotel_id.

        ``basis`` (#42 PART A) is the honest cost-basis discriminator the caller
        asserts for this line (additive strings only; never touches the price).
        """
        if not hotel_id:
            raise ValueError("add_lodging: hotel_id is required")
        key = _fee_key(source, LineItemKind.LODGING.value, leg)
        self._entries[key] = {
            "kind": LineItemKind.LODGING.value,
            "source": str(source),
            "leg": str(leg),
            "hotel_id": str(hotel_id),
            "checkin": str(checkin),
            "checkout": str(checkout),
            "adults": int(adults),
            # #42 PART A — basis disclosure (additive; does not alter pricing).
            **make_basis(basis),
        }

    # -- fees (visa / vaccine / premium / ...) -----------------------------

    def add_fee(
        self,
        *,
        source: str,
        kind: LineItemKind | str,
        leg: str,
        money: dict[str, Any],
        description: str = "",
        basis: str = BASIS_DETERMINISTIC_ESTIMATE,
    ) -> None:
        """
        Add (or idempotently replace) a FEE line item.

        Args:
            source:      emitting agent ("compliance", "health", "insurance", ...).
            kind:        a LineItemKind (visa/vaccine/premium/transport/permit).
            leg:         the leg key the fee attaches to, or PACKAGE_LEG for a
                         trip-level fee (e.g. a single insurance premium).
            money:       a contracts.make_money() currency tag (Budget reads usd_cents).
            description: optional human-readable label.
            basis:       #42 PART A honest cost-basis discriminator (fees are a
                         deterministic_estimate by default; additive strings only).

        Keyed by (source, kind, leg). Re-emission of the same key REPLACES the
        prior entry → never double-charges (the §16.3 invariant at fee level).
        """
        k = kind.value if isinstance(kind, LineItemKind) else str(kind)
        if k not in _KIND_VALUES:
            raise ValueError(
                f"add_fee: kind {k!r} not in closed set {sorted(_KIND_VALUES)}"
            )
        if k == LineItemKind.LODGING.value:
            raise ValueError("add_fee: use add_lodging() for lodging items")
        if not is_valid_money(money):
            raise ValueError(
                "add_fee: money must be a valid contracts.make_money() tag "
                "(Budget reads usd_cents from the FX seam, never an LLM number)"
            )
        key = _fee_key(source, k, leg)
        self._entries[key] = {
            "kind": k,
            "source": str(source),
            "leg": str(leg),
            # Synthetic, addressable, idempotent fee id (merchant line_items key).
            "hotel_id": f"fee:{source}:{k}:{leg}",
            "money": money,
            "usd_cents": int(money["usd_cents"]),
            "description": str(description),
            # #42 PART A — basis disclosure (additive; does not alter pricing).
            **make_basis(basis),
        }

    # -- assembly ----------------------------------------------------------

    def _sort_key(self, entry: dict[str, Any]) -> tuple[int, str, str]:
        """
        Deterministic total order: (kind_order, leg, source).

        Lodging first, then fees by canonical kind, then by leg, then by source.
        PACKAGE_LEG sorts after numbered legs (it begins with '_' > digits).
        """
        return (
            _KIND_ORDER[entry["kind"]],
            entry["leg"],
            entry["source"],
        )

    def assemble(self) -> list[dict[str, Any]]:
        """
        Return the deterministically-ordered, idempotent line_items list.

        Byte-identical given the same SET of (source, kind, leg) entries,
        regardless of insertion order — the cross-cutting money invariant.
        """
        return [dict(e) for e in sorted(self._entries.values(), key=self._sort_key)]

    def total_usd_cents(self) -> int:
        """
        Sum of usd_cents across all entries (fees + lodging that carry money).

        Lodging priced by the merchant (no money tag) contributes 0 here; the
        merchant remains authoritative for lodging totals. This helper is for
        the FEE total Budget adds to the package (the materially-wrong-by-$1k
        case the §17 Health interlock warns about).
        """
        return sum(int(e.get("usd_cents", 0)) for e in self._entries.values())

    def __len__(self) -> int:
        return len(self._entries)


def assemble_line_items(
    lodging: list[dict[str, Any]] | None = None,
    fees: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """
    Functional convenience wrapper: assemble from plain dicts in one call.

    Args:
        lodging: list of {hotel_id, leg, checkin, checkout, adults?, source?}.
        fees:    list of {source, kind, leg, money, description?}.

    Returns the deterministic, idempotent assembled line_items.
    """
    a = LineItemAssembler()
    for lo in lodging or []:
        a.add_lodging(**lo)
    for fe in fees or []:
        a.add_fee(**fe)
    return a.assemble()
