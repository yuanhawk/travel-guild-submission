"""
providers.booking — the fail-CLOSED, deny-by-default BOOKING AUTHORITY seam.

WHAT THIS IS
------------
The clean, swappable interface for the AUTHORITY layer that gates any real
booking / purchase mandate. This is the seam that decides whether an agent is
*allowed* to execute a booking on a principal's behalf — it sits ABOVE the
handoff-link builder (utils/booking_links.py, advisory hand-outs only) and the
UCP merchant proxy (mcp/travel_memory/booking.py, the commerce transport). Those
two are deliberately NOT modified by this seam; this module is the gate that a
live edition would consult BEFORE any execution.

This public repo ships:

  * ``Identity``               — the principal an agent acts for.
  * ``GrantConstraints``       — structured, immutable constraints on a grant.
  * ``AuthorityGrant``         — the mandate authority (scope, spend cap, expiry).
  * ``BookingRequest``         — a concrete booking mandate the agent wants run.
  * ``BookingResult``          — the closed-set outcome the caller consumes.
  * ``BookingProvider``        — the ABSTRACT contract the prod provider implements.
  * ``DenyingBookingProvider`` — the in-repo default: DENIES every mandate.
  * ``get_booking_provider``   — edition-keyed factory (public→Denying, prod→Live).
  * ``make_booking_provider``  — in-repo factory symbol (returns the Denying default);
                                 documents the EXACT attribute name the prod module
                                 must expose.

There is deliberately NO booking-execution code, NO credentials, NO network, and
NO secrets in this module — that lives ONLY in the private prod repo behind the
``BookingProvider`` contract.

THE FAIL-CLOSED CONTRACT (this is the whole point)
--------------------------------------------------
Pricing (providers.pricing) fails OPEN: a missing prod overlay degrades to an
honest "unavailable" DISPLAY. Booking must fail CLOSED. With NO prod provider
loaded — i.e. the public/judging repo, or ``TG_EDITION != prod``, or the prod
import failing — the default provider MUST:

  * DENY every authorize/execute call (status "denied"),
  * NEVER execute, NEVER charge, NEVER contact a merchant,
  * NEVER fabricate a success, and
  * NEVER raise into the caller.

The refusal IS the safety property. A denied result is the correct, safe outcome
of this repo; anything else would be a bug.

THE VARIANCE-0 (var-0) CONTRACT
-------------------------------
Booking authority is an ACTION / DISPLAY-layer concern. Nothing in this module is
on the deterministic plan path. It MUST NEVER be fed into:

  * the deterministic engine digest,
  * the day_plans / itinerary structure,
  * any cache key.

This seam is intentionally NOT wired into the orchestrator in this commit. It is
a dormant CONTRACT: constructing/using it performs no live work (the default
denies), so it cannot perturb the seeded digest, day_plans, or any cache key.

THE PROD CONTRACT (what the private LiveBookingProvider must implement)
----------------------------------------------------------------------
A ``LiveBookingProvider`` satisfying ``BookingProvider`` with the same method
signatures:

    authorize(grant, request) -> BookingResult
        Validate ONLY — decide whether the mandate is permitted. MUST enforce
        the spend cap, scope, currency, expiry and constraints (see
        ``authorization_denial_reason``). MUST NOT execute or charge. Returns a
        "denied" result with a reason on any failure; a "pending"/"executed"
        style result is reserved for execute().

    execute(grant, request) -> BookingResult
        Perform the booking IFF authorize() would permit it. MUST re-check the
        grant (never trust a stale authorize), MUST enforce the spend cap, and
        MUST fail CLOSED (return "denied"/"failed", never raise, never a
        fabricated success) on ANY doubt.

and a module-level ``make_booking_provider() -> BookingProvider`` factory in the
prod module named by ``TG_PROVIDER_MODULE`` (default "tg_prod.providers"). The
prod provider MUST return the ``BookingResult`` shape defined here (or an object
exposing the same ``status`` + ``as_dict()``), so both editions are indistinct
to the caller EXCEPT that prod can actually authorize/execute.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

from providers.edition import load_prod_factory

# ---------------------------------------------------------------------------
# Status discriminators — the CLOSED set the caller keys off.
# ---------------------------------------------------------------------------
STATUS_DENIED = "denied"      # refused by the authority layer (the safe default)
STATUS_EXECUTED = "executed"  # a real booking was committed (prod only)
STATUS_FAILED = "failed"      # attempted but did not complete (prod only)
STATUS_PENDING = "pending"    # authorized, awaiting completion (prod only)

ALLOWED_STATUSES = frozenset({
    STATUS_DENIED,
    STATUS_EXECUTED,
    STATUS_FAILED,
    STATUS_PENDING,
})

# The deny sentinel emitted when NO prod provider is loaded. Its presence is the
# fail-closed signal the tests (and any auditor) assert on.
REASON_NO_PROVIDER = "no_booking_provider"


# ===========================================================================
# Value types (frozen — immutable by construction)
# ===========================================================================

@dataclass(frozen=True)
class Identity:
    """The principal an agent acts for. Carries NO credentials/secrets — an
    opaque id + a human-facing label only."""

    user_id: str
    display_name: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {"user_id": self.user_id, "display_name": self.display_name}


@dataclass(frozen=True)
class GrantConstraints:
    """Structured, immutable constraints attached to an AuthorityGrant.

    A live provider MUST honour every constraint before executing. Adding a
    field here is a tightening; a live provider that does not understand a
    constraint MUST fail closed (deny) rather than ignore it.
    """

    refundable_only: bool = False
    # Optional allow-list of merchant ids the grant may transact with. Empty
    # tuple means "unrestricted by merchant" (other checks still apply). A tuple
    # (not a set/list) keeps the dataclass hashable and immutable.
    allowed_merchants: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "refundable_only": self.refundable_only,
            "allowed_merchants": list(self.allowed_merchants),
        }


@dataclass(frozen=True)
class AuthorityGrant:
    """The mandate AUTHORITY: what an agent is permitted to book for a principal.

    Immutable. Carries a spend cap, a scope, an expiry and structured
    constraints — but NO credentials. A live provider treats this as the ONLY
    source of truth for what it may do, and MUST deny anything outside it.
    """

    grant_id: str
    principal: Identity
    scope: str                       # what it may book, e.g. "hotel" / "flight"
    spend_cap_cents: int             # hard ceiling, integer cents
    currency: str                    # ISO code the cap is denominated in
    valid_until: str                 # ISO date "YYYY-MM-DD" — inclusive expiry
    constraints: GrantConstraints = field(default_factory=GrantConstraints)

    def as_dict(self) -> dict[str, Any]:
        return {
            "grant_id": self.grant_id,
            "principal": self.principal.as_dict(),
            "scope": self.scope,
            "spend_cap_cents": int(self.spend_cap_cents),
            "currency": str(self.currency).upper(),
            "valid_until": self.valid_until,
            "constraints": self.constraints.as_dict(),
        }


@dataclass(frozen=True)
class BookingRequest:
    """A concrete booking mandate the agent wants executed under a grant.

    Immutable and non-secret: an item reference + an amount, never a card /
    token / credential. Money is integer cents, matching the merchant contract.
    """

    request_id: str
    scope: str              # must match the grant's scope, e.g. "hotel"
    merchant: str           # merchant id the booking would run against
    item_ref: str           # opaque catalog/offer reference
    amount_cents: int       # integer cents the booking would charge
    currency: str           # ISO code the amount is denominated in
    refundable: bool = False  # whether the offer being booked is refundable

    def as_dict(self) -> dict[str, Any]:
        return {
            "request_id": self.request_id,
            "scope": self.scope,
            "merchant": self.merchant,
            "item_ref": self.item_ref,
            "amount_cents": int(self.amount_cents),
            "currency": str(self.currency).upper(),
            "refundable": self.refundable,
        }


@dataclass(frozen=True)
class BookingResult:
    """The closed-set outcome the caller consumes.

    ``status`` is one of ALLOWED_STATUSES. ``reason`` is a stable machine token
    ("no_booking_provider", "spend_cap_exceeded", ...). ``detail`` is a
    non-secret human string. ``confirmation_ref`` is ONLY ever set on a real
    "executed" result from prod — it is None for every denied/failed outcome, so
    ``confirmation_ref is None`` is a reliable "nothing was booked" check.
    """

    status: str
    reason: str
    detail: str = ""
    request_id: str | None = None
    confirmation_ref: str | None = None

    @property
    def is_executed(self) -> bool:
        """True ONLY for a real committed booking. False for denied/failed/pending."""
        return self.status == STATUS_EXECUTED

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "reason": self.reason,
            "detail": self.detail,
            "request_id": self.request_id,
            "confirmation_ref": self.confirmation_ref,
        }


# ===========================================================================
# Shared, PURE authorization predicate (reusable by the prod provider)
# ===========================================================================

def authorization_denial_reason(
    grant: AuthorityGrant,
    request: BookingRequest,
    *,
    now: str,
) -> str | None:
    """Return a machine deny-reason iff ``grant`` does NOT permit ``request``,
    else None. PURE and deterministic — ``now`` (an ISO "YYYY-MM-DD" date) is
    passed in, never read from the wall clock, so this is var-0 safe and unit
    testable.

    This is a shared fail-closed primitive: a live provider SHOULD call it and
    deny on any non-None result BEFORE executing. It is intentionally
    conservative — an unparseable/mismatched field denies rather than passes.
    The in-repo default does NOT need it (it denies unconditionally); it exists
    so the prod LiveBookingProvider has one enforcement point to reuse.
    """
    # Scope must match exactly.
    if (request.scope or "").strip() != (grant.scope or "").strip():
        return "scope_mismatch"
    # Currency must match exactly (no implicit FX at the authority layer).
    if (request.currency or "").strip().upper() != (grant.currency or "").strip().upper():
        return "currency_mismatch"
    # Amount must be a sane, non-negative integer within the cap.
    try:
        amount = int(request.amount_cents)
        cap = int(grant.spend_cap_cents)
    except (TypeError, ValueError):
        return "amount_unparseable"
    if amount < 0:
        return "amount_negative"
    if amount > cap:
        return "spend_cap_exceeded"
    # Expiry: deny if today is past the grant's inclusive valid_until. Compared
    # as ISO "YYYY-MM-DD" strings, which sort chronologically. Any malformed
    # date denies (fail closed).
    if not _is_iso_date(now) or not _is_iso_date(grant.valid_until):
        return "expiry_unparseable"
    if now > grant.valid_until:
        return "grant_expired"
    # Merchant allow-list (empty tuple = unrestricted).
    allowed = grant.constraints.allowed_merchants
    if allowed and (request.merchant or "") not in allowed:
        return "merchant_not_allowed"
    # Refundability constraint.
    if grant.constraints.refundable_only and not request.refundable:
        return "refundable_required"
    return None


def _is_iso_date(s: Any) -> bool:
    """True iff ``s`` is exactly a 10-char YYYY-MM-DD (pure string check)."""
    if not isinstance(s, str) or len(s) != 10 or s[4] != "-" or s[7] != "-":
        return False
    return s[0:4].isdigit() and s[5:7].isdigit() and s[8:10].isdigit()


# ===========================================================================
# The abstract provider contract
# ===========================================================================

class BookingProvider(ABC):
    """The swappable BOOKING AUTHORITY interface.

    A provider MUST:
      * enforce the grant's spend cap, scope, currency, expiry and constraints
        (see ``authorization_denial_reason``);
      * fail CLOSED — return a "denied"/"failed" ``BookingResult`` (never raise,
        never fabricate a success) on ANY error or doubt;
      * treat ``AuthorityGrant`` as the sole source of truth for what is allowed.

    The public repo ships only ``DenyingBookingProvider`` (denies everything);
    the private prod repo ships the LiveBookingProvider that can actually
    authorize/execute.
    """

    @abstractmethod
    def authorize(self, grant: AuthorityGrant, request: BookingRequest) -> BookingResult:
        """Validate ONLY. Decide whether the mandate is permitted. MUST NOT
        execute or charge. Returns a "denied" result (with a reason) if not
        permitted."""
        raise NotImplementedError

    @abstractmethod
    def execute(self, grant: AuthorityGrant, request: BookingRequest) -> BookingResult:
        """Perform the booking IFF permitted. MUST re-check the grant and fail
        CLOSED on any doubt. In the public repo this ALWAYS denies."""
        raise NotImplementedError


# ===========================================================================
# DenyingBookingProvider — the in-repo default (fail CLOSED)
# ===========================================================================

_DENY_DETAIL = (
    "No booking authority provider is loaded in this edition. Booking is "
    "fail-CLOSED: every mandate is denied. A live booking provider is a "
    "prod-edition feature and lives in the private repo."
)


@dataclass(frozen=True)
class DenyingBookingProvider(BookingProvider):
    """The default provider for the public/judging edition.

    Every method returns ``BookingResult(status="denied",
    reason="no_booking_provider")``. It performs NO I/O, contacts NO merchant,
    holds NO credentials, and NEVER raises into the caller. The denial is the
    safety property — this is the correct behaviour for a repo with no live
    booking authority.
    """

    detail: str = _DENY_DETAIL

    def _deny(self, request: BookingRequest) -> BookingResult:
        rid = request.request_id if isinstance(request, BookingRequest) else None
        return BookingResult(
            status=STATUS_DENIED,
            reason=REASON_NO_PROVIDER,
            detail=self.detail,
            request_id=rid,
            confirmation_ref=None,  # nothing was booked — always None here
        )

    def authorize(self, grant: AuthorityGrant, request: BookingRequest) -> BookingResult:
        return self._deny(request)

    def execute(self, grant: AuthorityGrant, request: BookingRequest) -> BookingResult:
        # NEVER executes. NEVER charges. NEVER contacts a merchant.
        return self._deny(request)


# ===========================================================================
# Edition-keyed factory
# ===========================================================================

# The EXACT attribute name the prod module (TG_PROVIDER_MODULE, default
# "tg_prod.providers") must expose: make_booking_provider() -> BookingProvider.
_PROD_FACTORY = "make_booking_provider"


def get_booking_provider() -> BookingProvider:
    """Return the active booking-authority provider for the current edition.

    Public / judging edition (TG_EDITION != prod, or the prod module absent /
    non-importable / missing the factory / raising on construction) →
    ``DenyingBookingProvider`` (fail CLOSED). Only an explicit, importable prod
    edition that hands back a real provider can return anything else. Never
    raises.
    """
    factory = load_prod_factory(_PROD_FACTORY)
    if factory is not None:
        try:
            provider = factory()
        except Exception:  # noqa: BLE001 — ANY prod-construction failure → fail CLOSED
            return DenyingBookingProvider()
        if provider is not None:
            return provider
    # No prod provider → DENY everything. This is the safe default.
    return DenyingBookingProvider()


def make_booking_provider() -> BookingProvider:
    """In-repo factory symbol, mirroring the name the prod module must expose.

    In the PUBLIC repo this returns the fail-CLOSED ``DenyingBookingProvider``.
    It exists so (a) the symbol name is documented in-tree and (b) callers can
    construct the default directly. The edition swap goes through
    ``get_booking_provider()`` (which loads the PROD module's function of the
    same name), NOT this one.
    """
    return DenyingBookingProvider()
