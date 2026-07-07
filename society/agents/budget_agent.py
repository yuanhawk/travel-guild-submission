"""
budget_agent.py — Budget/Finance A2A agent (Travel Guild M1).

Design contract: AGENT-SOCIETY-A2A-DESIGN.md §3.2, §0.2, §4.6.

Agent Card skills:
  ``budget.check``   — CHECK phase: create_checkout only (no capture).
  ``budget.commit``  — COMMIT phase: complete_checkout on an existing session.
  ``budget.enforce`` — LEGACY combined (backward-compat): check + commit in one call.

SEV-1a two-phase fix:
  The orchestrator must NOT commit a booking before the Critic and Transport
  gates pass.  The two new skills split the flow:
    1. budget.check:  create_checkout → returns {decision, checkout_id, total_cents}
                      with decision "check_ok" (within budget) or "veto".
                      No booking is created (complete_checkout is NOT called).
    2. budget.commit: complete_checkout(checkout_id, buyer_consent/ap2_mandate,
                      idempotency_key) → the actual irreversible booking.
                      Decision: "accept" | "veto" | "needs_consent" | "needs_mandate".

  A created-but-not-committed checkout from a rejected round is simply abandoned
  (never completed → no booking at the merchant).

SEV-1b fix:
  budget.check now passes total_budget_cents as user_budget_cents to
  create_checkout, so the merchant enforces min(user_budget, BudgetHardMaxCents)
  on complete_checkout rather than the static house ceiling.

Input for budget.check (data part, JSON):
    {
        "user_id":            str,
        "line_items":         [{"hotel_id": str, "checkin": str, "checkout": str, "adults": int}],
        "total_budget_cents": int,    # user's per-trip budget (SEV-1b)
        "autonomy_level":     str    (optional — "L2" | "L3", default "L2"),
        "idempotency_key":    str    (optional — threaded through to commit)
    }

Output for budget.check — typed BudgetCheckResult:
    {
        "decision":           "check_ok" | "veto",
        "checkout_id":        str,
        "total_cents":        int,
        "currency":           "USD",
        "provenance":         "merchant",
        "idempotency_key":    str | null,
        # veto only:
        "veto_reason":        str,
        "budget_ceiling_cents": int | null,
        "hard_max_cents":     int | null,
    }

Input for budget.commit (data part, JSON):
    {
        "user_id":         str,
        "checkout_id":     str,
        "buyer_consent":   bool   (optional — HITL L2 path),
        "ap2_mandate":     dict   (optional — L3 path),
        "autonomy_level":  str    (optional — "L2" | "L3", default "L2"),
        "idempotency_key": str    (optional — idempotent on repeat)
    }

Output for budget.commit — typed BudgetResult (same as legacy budget.enforce):
    {
        "decision":             "accept" | "veto" | "needs_consent" | "needs_mandate",
        "checkout_id":          str,
        "total_cents":          int,
        "currency":             "USD",
        "provenance":           "merchant",
        "idempotency_key":      str | null,
        # accept only:
        "booking_ref":          str | null,
        "status":               "complete" | "incomplete",
        # veto only:
        "veto_reason":          str,
        "budget_ceiling_cents": int | null,
        "hard_max_cents":       int | null,
        # needs_consent only:
        "consent_message":      str,
        # needs_mandate only:
        "mandate_message":      str,
    }

budget.enforce (legacy skill) is still supported for backward-compat:
    Input: {user_id, line_items, buyer_consent?, ap2_mandate?, autonomy_level?, idempotency_key?}
    Output: same as budget.commit.

The veto IS the real HTTP 403 from checkout.go.  The agent does not fabricate
prices or override the merchant — it round-trips the result faithfully.

§4.6 reliability:
  - Typed result schema above (all fields explicit, no opaque blobs).
  - Idempotency-key echo (caller can retry safely if merchant returns idempotent=true).
  - provenance: "merchant" tag on every result.
  - A2A Task state "input-required" for needs_consent / needs_mandate paths.
"""

from __future__ import annotations

import json
import logging
import os
import socket
import uuid
from typing import Any
from urllib.parse import urlparse

import httpx
from utils import ucp_signing
import uvicorn

from agents.a2a_agent import (
    A2AAgent,
    _data_part,
    _new_artifact,
    _task_transition,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Merchant MCP endpoint
# ---------------------------------------------------------------------------

MERCHANT_MCP_URL = os.environ.get(
    "MERCHANT_MCP_URL",
    "http://ucp-merchant:8090/api/ucp/mcp",
)


def _validate_merchant_url(url: str) -> None:
    """SSRF-001: startup-time guard for MERCHANT_MCP_URL.

    Scheme must be http or https.
    Link-local / metadata addresses (169.254.0.0/16) are blocked —
    these include AWS/GCP/Azure/Oracle IMDS endpoints.
    Private RFC-1918 addresses (10.x, 172.16-31.x, 192.168.x) are
    ALLOWED because the Go merchant lives on a private cluster IP.
    Unresolvable hostnames (cluster-internal DNS names that are not
    reachable from the dev box) are allowed.
    """
    try:
        parts = urlparse(url)
        if parts.scheme not in ("http", "https"):
            raise ValueError(
                f"MERCHANT_MCP_URL scheme must be http or https, got: {parts.scheme!r}"
            )
        hostname = parts.hostname or ""
        try:
            resolved_ip = socket.gethostbyname(hostname)
            # Block link-local / IMDS (169.254.0.0/16).
            if resolved_ip.startswith("169.254."):
                raise ValueError(
                    f"MERCHANT_MCP_URL resolves to link-local/metadata address "
                    f"{resolved_ip!r} — SSRF-001 block (IMDS at 169.254.169.254)"
                )
        except socket.gaierror:
            pass  # Unresolvable cluster-internal hostname → allow
    except ValueError:
        raise
    except Exception as exc:
        logger.warning("MERCHANT_MCP_URL validation skipped: %s", exc)


# Validate at import time so a mis-configured metadata URL is caught at startup.
_validate_merchant_url(MERCHANT_MCP_URL)

# Default HTTP timeout for merchant calls (seconds)
_MERCHANT_TIMEOUT = float(os.environ.get("MERCHANT_TIMEOUT", "15"))


# ---------------------------------------------------------------------------
# Typed result builder helpers
# ---------------------------------------------------------------------------

def _base_result(
    decision: str,
    checkout_id: str,
    total_cents: int | None,
    idempotency_key: str | None,
) -> dict[str, Any]:
    """Return a BudgetResult dict with the mandatory common fields."""
    return {
        "decision": decision,
        "checkout_id": checkout_id,
        "total_cents": total_cents,
        "currency": "USD",
        "provenance": "merchant",
        "idempotency_key": idempotency_key,
    }


def _accept_result(
    checkout_id: str,
    total_cents: int,
    booking_ref: str | None,
    status: str,
    idempotency_key: str | None,
) -> dict[str, Any]:
    r = _base_result("accept", checkout_id, total_cents, idempotency_key)
    r["booking_ref"] = booking_ref or None
    r["status"] = status
    return r


def _veto_result(
    checkout_id: str,
    total_cents: int,
    veto_reason: str,
    budget_ceiling_cents: int | None,
    hard_max_cents: int | None,
    idempotency_key: str | None,
) -> dict[str, Any]:
    r = _base_result("veto", checkout_id, total_cents, idempotency_key)
    r["veto_reason"] = veto_reason
    r["budget_ceiling_cents"] = budget_ceiling_cents
    r["hard_max_cents"] = hard_max_cents
    return r


def _insufficient_funds_result(
    checkout_id: str,
    total_cents: int,
    wallet_balance_cents: int | None,
    idempotency_key: str | None,
) -> dict[str, Any]:
    """
    SIMULATED prepaid wallet INSUFFICIENT-FUNDS terminal (merchant HTTP 402).

    DISTINCT from a budget veto (403 price_exceeds_budget): the trip is within the
    user's budget but EXCEEDS the funded wallet balance. The orchestrator treats
    this as a TERMINAL cannot_satisfy (not a re-plan trigger).
    """
    r = _base_result("insufficient_funds", checkout_id, total_cents, idempotency_key)
    r["wallet_balance_cents"] = wallet_balance_cents
    return r


def _cannot_price_result(
    checkout_id: str,
    total_cents: int,
    merchant_status: str,
    veto_reason: str,
    idempotency_key: str | None,
) -> dict[str, Any]:
    """
    M2 — HARD merchant failure at create_checkout (sold out / unavailable /
    error / cancelled, or a malformed response missing id/total). Distinct
    from a genuine budget veto: nothing was ever priced, so there is no
    ceiling to tighten and no leg to re-plan cheaper for. Emits decision
    "unavailable" for an explicit merchant unavailable status, "cannot_price"
    for every other hard failure (missing id/total/generic error) — the two
    decisions the orchestrator's D3 honest-terminal branch
    (`decision in ("cannot_price", "unavailable")`, orchestrator.py) is
    designed to catch. Emitting "veto" here instead (the old behaviour) makes
    that branch dead code: the failure falls into the generic budget-veto
    re-plan loop, which for an in-budget package leaves every leg already
    under ceiling, never re-plans, and fabricates a "$0 short" budget-
    shortfall message for what is actually a merchant-availability outage.
    """
    decision = "unavailable" if merchant_status == "unavailable" else "cannot_price"
    r = _base_result(decision, checkout_id, total_cents, idempotency_key)
    r["veto_reason"] = veto_reason
    return r


def _needs_consent_result(
    checkout_id: str,
    total_cents: int,
    consent_message: str,
    idempotency_key: str | None,
) -> dict[str, Any]:
    r = _base_result("needs_consent", checkout_id, total_cents, idempotency_key)
    r["consent_message"] = consent_message
    return r


def _needs_mandate_result(
    checkout_id: str,
    total_cents: int,
    mandate_message: str,
    idempotency_key: str | None,
) -> dict[str, Any]:
    r = _base_result("needs_mandate", checkout_id, total_cents, idempotency_key)
    r["mandate_message"] = mandate_message
    return r


# ---------------------------------------------------------------------------
# Merchant MCP client helpers
# ---------------------------------------------------------------------------

def _redact(d: Any) -> dict:
    """Redact sensitive fields from a merchant response dict before logging."""
    _SENSITIVE = {
        "booking_ref", "wallet_session_id", "wallet_balance_cents",
        "user_id", "card_number", "token",
    }
    return {
        k: ("***" if k in _SENSITIVE else v)
        for k, v in (d.items() if isinstance(d, dict) else {}.items())
    }


def _mcp_rpc(
    client: httpx.Client,
    tool_name: str,
    arguments: dict[str, Any],
) -> tuple[dict[str, Any], int]:
    """
    Call a merchant MCP tool.

    Returns (structured_content_dict, http_status_code).
    Raises httpx.HTTPError on connection failure.
    """
    payload = {
        "jsonrpc": "2.0",
        "id": str(uuid.uuid4()),
        "method": "tools/call",
        "params": {
            "name": tool_name,
            "arguments": arguments,
        },
    }
    # #53 — sign the checkout request (RFC 9421) IFF a client signing key is
    # configured; otherwise json=payload exactly as before (unsigned → byte-identical).
    _body = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode()
    try:
        _sig = ucp_signing.signed_headers("POST", MERCHANT_MCP_URL, _body)
    except ucp_signing.SigningError as _se:
        raise RuntimeError(f"merchant request signing failed — check UCP_CLIENT_SIGNING_KEY_PATH: {_se}") from _se
    if _sig:
        resp = client.post(MERCHANT_MCP_URL, content=_body,
                           headers={"Content-Type": "application/json", **_sig},
                           timeout=_MERCHANT_TIMEOUT)
    else:
        resp = client.post(MERCHANT_MCP_URL, json=payload, timeout=_MERCHANT_TIMEOUT)
    # Merchant returns HTTP 403 for denied checkouts — do NOT raise_for_status
    # so we can inspect the body; we record the actual status code.
    http_code = resp.status_code
    try:
        rpc_body = resp.json()
    except Exception:
        raise RuntimeError(
            f"Merchant returned non-JSON body (HTTP {http_code}): {resp.text[:200]}"
        )

    # The merchant wraps the domain result in result.structuredContent
    rpc_result = rpc_body.get("result", {})
    structured = rpc_result.get("structuredContent", {})
    return structured, http_code


# ---------------------------------------------------------------------------
# BudgetAgent
# ---------------------------------------------------------------------------

class BudgetAgent(A2AAgent):
    """
    Budget/Finance A2A agent (Travel Guild M1).

    Implements the ``budget.enforce`` skill: validates a proposed itinerary
    against the live UCP merchant and returns a typed BudgetResult artifact.
    The veto IS the merchant's real HTTP 403 from checkout.go — not a script.

    Args:
        host:              Bind host for the ASGI server.
        port:              Bind port for the ASGI server.
        merchant_transport: Optional httpx.BaseTransport to inject for testing.
                            If None, httpx uses its default transport (real HTTP).
    """

    def __init__(
        self,
        host: str = "0.0.0.0",
        port: int = 9101,
        merchant_transport: "httpx.BaseTransport | None" = None,
    ) -> None:
        self._host = host
        self._port = port
        self._merchant_transport = merchant_transport
        super().__init__()

    # ------------------------------------------------------------------
    # A2AAgent protocol
    # ------------------------------------------------------------------

    def _build_card(self) -> dict:
        url = f"http://{self._host}:{self._port}"
        return {
            "name": "budget-agent",
            "description": (
                "Budget/Finance specialist — the real merchant veto (SEV-1a two-phase). "
                "budget.check: create_checkout only (no capture) — returns check_ok or veto. "
                "budget.commit: complete_checkout on an existing session — the irreversible booking. "
                "budget.enforce: legacy combined (backward-compat). "
                "A veto IS a merchant 403, not an LLM opinion. "
                "Part of the Travel Guild multi-agent pipeline (Track 3, M1)."
            ),
            "url": url,
            "version": "2.0.0",
            "protocolVersion": "0.3.0",
            "preferredTransport": "JSONRPC",
            "capabilities": {
                "streaming": False,
                "pushNotifications": False,
            },
            "defaultInputModes": ["data"],
            "defaultOutputModes": ["data"],
            "skills": [
                {
                    "id": "budget.check",
                    "name": "Budget Check",
                    "description": (
                        "SEV-1a CHECK phase: create_checkout only — no booking committed. "
                        "Returns decision check_ok (within user budget) or veto (over budget). "
                        "Pass total_budget_cents to bind the merchant veto to the user's budget. "
                        "The orchestrator runs Transport + Critic gates BEFORE calling budget.commit."
                    ),
                    "tags": ["budget", "check", "veto", "merchant", "ucp", "two-phase"],
                    "inputSchema": {
                        "type": "object",
                        "properties": {
                            "user_id": {"type": "string"},
                            "line_items": {
                                "type": "array",
                                "items": {
                                    "type": "object",
                                    "properties": {
                                        "hotel_id": {"type": "string"},
                                        "checkin": {"type": "string"},
                                        "checkout": {"type": "string"},
                                        "adults": {"type": "integer"},
                                    },
                                    "required": ["hotel_id", "checkin", "checkout"],
                                },
                            },
                            "total_budget_cents": {"type": "integer"},
                            "autonomy_level": {"type": "string", "enum": ["L2", "L3"]},
                            "idempotency_key": {"type": "string"},
                        },
                        "required": ["user_id", "line_items"],
                    },
                    "examples": [
                        '{"user_id":"u1","total_budget_cents":80000,'
                        '"line_items":[{"hotel_id":"bali-alaya-ubud",'
                        '"checkin":"2025-10-01","checkout":"2025-10-03","adults":1}]}',
                    ],
                },
                {
                    "id": "budget.commit",
                    "name": "Budget Commit",
                    "description": (
                        "SEV-1a COMMIT phase: complete_checkout on an existing session. "
                        "The irreversible booking step — called ONLY after Transport + Critic "
                        "gates have passed. Requires checkout_id from budget.check. "
                        "Pass idempotency_key to get the same booking_ref on retries."
                    ),
                    "tags": ["budget", "commit", "booking", "merchant", "ucp", "two-phase"],
                    "inputSchema": {
                        "type": "object",
                        "properties": {
                            "user_id": {"type": "string"},
                            "checkout_id": {"type": "string"},
                            "buyer_consent": {"type": "boolean"},
                            "ap2_mandate": {"type": "object"},
                            "autonomy_level": {"type": "string", "enum": ["L2", "L3"]},
                            "idempotency_key": {"type": "string"},
                            "total_cents": {
                                "type": "integer",
                                "description": (
                                    "Optional: session total from prior budget.check result. "
                                    "Used as fallback when requires_consent/requires_mandate "
                                    "omit total_cents, so the consent surface shows the real amount."
                                ),
                            },
                        },
                        "required": ["user_id", "checkout_id"],
                    },
                    "examples": [
                        '{"user_id":"u1","checkout_id":"co_abc123","buyer_consent":true,'
                        '"idempotency_key":"trip-uuid-here"}',
                    ],
                },
                {
                    "id": "budget.fund",
                    "name": "Budget Fund (Simulated Wallet)",
                    "description": (
                        "Create-OR-RESET the SIMULATED prepaid wallet for a run "
                        "(wallet_session_id = the deterministic per-run digest). Seeds "
                        "balance←wallet_balance_cents and clears the ledger — reset-per-run "
                        "(var-0 keystone). complete_checkout debits this wallet (HTTP 402 "
                        "insufficient_funds when over balance, distinct from the 403 budget "
                        "veto); cancel_checkout credits it. Honestly labeled simulated:true."
                    ),
                    "tags": ["budget", "wallet", "simulated", "merchant", "ucp"],
                    "inputSchema": {
                        "type": "object",
                        "properties": {
                            "wallet_session_id": {"type": "string"},
                            "wallet_balance_cents": {"type": "integer"},
                        },
                        "required": ["wallet_session_id", "wallet_balance_cents"],
                    },
                    "examples": [
                        '{"wallet_session_id":"trip-deadbeef","wallet_balance_cents":500000}',
                    ],
                },
                {
                    "id": "budget.cancel",
                    "name": "Budget Cancel (Void)",
                    "description": (
                        "VOID a previously-committed checkout — release the booking it holds "
                        "(status→cancelled, booking_ref dropped). Owner-only at the merchant; "
                        "idempotent and safe (cancel of an unknown/already-cancelled id → success; "
                        "a cancelled checkout can never be re-completed). Used by cascade recovery "
                        "(§12.1) to void the SUPERSEDED booking when the next cycle commits, so "
                        "exactly one itinerary is ever live (no double-charge across cycles). "
                        "Narrative-only refundability — no merchant refund ledger."
                    ),
                    "tags": ["budget", "cancel", "void", "merchant", "ucp", "cascade"],
                    "inputSchema": {
                        "type": "object",
                        "properties": {
                            "user_id": {"type": "string"},
                            "checkout_id": {"type": "string"},
                            "autonomy_level": {"type": "string", "enum": ["L2", "L3"]},
                        },
                        "required": ["user_id", "checkout_id"],
                    },
                    "examples": [
                        '{"user_id":"u1","checkout_id":"co_abc123"}',
                    ],
                },
                {
                    "id": "budget.enforce",
                    "name": "Budget Enforce (Legacy)",
                    "description": (
                        "LEGACY combined check+commit in one call (backward-compat). "
                        "Creates a multi-item checkout on the UCP merchant, then "
                        "(if buyer_consent or ap2_mandate is present) attempts to "
                        "complete it. Maps the merchant verdict to an A2A decision: "
                        "accept | veto | needs_consent | needs_mandate. "
                        "A veto is the merchant's real 403 (price_exceeds_budget / "
                        "price_exceeds_hard_max) — the agent does not override it. "
                        "PREFER budget.check + budget.commit for new orchestrators."
                    ),
                    "tags": ["budget", "finance", "veto", "merchant", "ucp", "checkout", "legacy"],
                    "inputSchema": {
                        "type": "object",
                        "properties": {
                            "user_id": {"type": "string"},
                            "line_items": {
                                "type": "array",
                                "items": {
                                    "type": "object",
                                    "properties": {
                                        "hotel_id": {"type": "string"},
                                        "checkin": {"type": "string"},
                                        "checkout": {"type": "string"},
                                        "adults": {"type": "integer"},
                                    },
                                    "required": ["hotel_id", "checkin", "checkout"],
                                },
                            },
                            "buyer_consent": {"type": "boolean"},
                            "ap2_mandate": {"type": "object"},
                            "autonomy_level": {
                                "type": "string",
                                "enum": ["L2", "L3"],
                            },
                            "idempotency_key": {"type": "string"},
                        },
                        "required": ["user_id", "line_items"],
                    },
                    "examples": [
                        '{"user_id":"u1","line_items":[{"hotel_id":"bali-alaya-ubud",'
                        '"checkin":"2025-10-01","checkout":"2025-10-03","adults":1}],'
                        '"buyer_consent":true}',
                    ],
                },
            ],
        }

    def _register_skills(self) -> None:
        self.register_skill("budget.check", self._check_handler)
        self.register_skill("budget.commit", self._commit_handler)
        self.register_skill("budget.cancel", self._cancel_handler)
        self.register_skill("budget.fund", self._fund_handler)  # SIMULATED prepaid wallet
        self.register_skill("budget.enforce", self._enforce_handler)  # legacy

    async def _fund_handler(self, message: dict, task: dict) -> dict:
        """
        budget.fund skill handler — create-OR-RESET the SIMULATED prepaid wallet.

        Calls the merchant wallet_fund tool, keyed by the deterministic per-run
        wallet_session_id, so the wallet is reset-per-run (var-0 keystone). Returns
        the wallet view {seed_cents, balance_cents, ledger, simulated:true, note}.
        """
        payload = self._extract_payload(message)
        if payload is None:
            raise ValueError(
                "budget.fund requires a data part with JSON payload "
                "{wallet_session_id, wallet_balance_cents}"
            )
        wallet_session_id: str = payload.get("wallet_session_id", "")
        wallet_balance_cents: int = int(payload.get("wallet_balance_cents") or 0)
        # #161 — optional canonical merchant end-user id (see _fund_merchant).
        user_id: str = payload.get("user_id", "")
        if not wallet_session_id:
            raise ValueError("wallet_session_id is required")

        result_data = await self._fund_merchant(
            wallet_session_id=wallet_session_id,
            wallet_balance_cents=wallet_balance_cents,
            user_id=user_id,
        )
        return _new_artifact(
            name="budget.fund.result",
            parts=[_data_part(result_data)],
        )

    # ------------------------------------------------------------------
    # Skill handlers
    # ------------------------------------------------------------------

    async def _check_handler(self, message: dict, task: dict) -> dict:
        """
        budget.check skill handler — SEV-1a CHECK phase.

        Calls create_checkout only.  NO complete_checkout (no booking committed).
        Returns decision "check_ok" (within user budget) or "veto" (over budget).

        The veto at CHECK time uses the merchant's pre-computed total vs.
        total_budget_cents (simple Python comparison — this is a fast pre-filter).
        The authoritative veto happens at COMMIT time on checkout.go.

        D3 / gap #11: absent or <=0 total_budget_cents is treated as an unknown
        budget and returns a conservative veto (budget gate did not run) rather
        than silently approving every price.
        """
        payload = self._extract_payload(message)
        if payload is None:
            raise ValueError(
                "budget.check requires a data part with JSON payload "
                "{user_id, line_items, total_budget_cents, ...}"
            )

        user_id: str = payload.get("user_id", "")
        line_items: list[dict] = payload.get("line_items", [])
        total_budget_cents: int = int(payload.get("total_budget_cents") or 0)
        autonomy_level: str = payload.get("autonomy_level", "L2")
        idempotency_key: str | None = payload.get("idempotency_key")
        # SIMULATED prepaid wallet binding (the deterministic per-run digest). When
        # present it is threaded into create_checkout so the merchant binds the
        # session to the wallet for the commit-time debit / cancel-time credit.
        wallet_session_id: str | None = payload.get("wallet_session_id")

        if not user_id:
            raise ValueError("user_id is required")
        if not line_items:
            raise ValueError("line_items must be a non-empty list")

        # D3 / gap #11: absent or <=0 total_budget_cents means the budget gate
        # cannot run — fail conservative (veto) rather than silently approving.
        if total_budget_cents <= 0:
            logger.warning(
                "budget.check: no_budget_supplied user=%s — conservative veto "
                "(total_budget_cents=%d; budget gate did not run)",
                user_id, total_budget_cents,
            )
            return _new_artifact(
                name="budget.check.result",
                parts=[_data_part(_veto_result(
                    checkout_id="",
                    total_cents=0,
                    veto_reason="no_budget_supplied",
                    budget_ceiling_cents=None,
                    hard_max_cents=None,
                    idempotency_key=idempotency_key,
                ))],
            )

        result_data = await self._check_merchant(
            user_id=user_id,
            line_items=line_items,
            total_budget_cents=total_budget_cents,
            autonomy_level=autonomy_level,
            idempotency_key=idempotency_key,
            wallet_session_id=wallet_session_id,
        )

        return _new_artifact(
            name="budget.check.result",
            parts=[_data_part(result_data)],
        )

    async def _commit_handler(self, message: dict, task: dict) -> dict:
        """
        budget.commit skill handler — SEV-1a COMMIT phase.

        Calls complete_checkout on an existing session identified by checkout_id.
        This is the ONLY place where an irreversible booking is created.

        The orchestrator calls this ONLY after Transport + Critic gates pass.

        MED fix: if the caller (orchestrator) supplies total_cents from the prior
        budget.check result, thread it as session_total_cents so that
        requires_consent/requires_mandate responses that omit total_cents still
        surface the real amount rather than a misleading 0.
        """
        payload = self._extract_payload(message)
        if payload is None:
            raise ValueError(
                "budget.commit requires a data part with JSON payload "
                "{user_id, checkout_id, buyer_consent?, ap2_mandate?, idempotency_key?}"
            )

        user_id: str = payload.get("user_id", "")
        checkout_id: str = payload.get("checkout_id", "")
        buyer_consent: bool = bool(payload.get("buyer_consent", False))
        ap2_mandate: dict | None = payload.get("ap2_mandate")
        autonomy_level: str = payload.get("autonomy_level", "L2")
        idempotency_key: str | None = payload.get("idempotency_key")
        # Optional: orchestrator may supply the session total from budget.check so
        # that requires_consent/requires_mandate (which may omit total_cents) still
        # show the real amount on the consent surface (MED fix, gap #59).
        raw_session_total = payload.get("total_cents")
        session_total_cents: int = int(raw_session_total) if raw_session_total is not None else 0

        if not user_id:
            raise ValueError("user_id is required")
        if not checkout_id:
            raise ValueError("checkout_id is required")

        result_data = await self._commit_merchant(
            user_id=user_id,
            checkout_id=checkout_id,
            buyer_consent=buyer_consent,
            ap2_mandate=ap2_mandate,
            autonomy_level=autonomy_level,
            idempotency_key=idempotency_key,
            session_total_cents=session_total_cents,
            task=task,
        )

        if result_data["decision"] in ("needs_consent", "needs_mandate"):
            _task_transition(task, "input-required")

        return _new_artifact(
            name="budget.commit.result",
            parts=[_data_part(result_data)],
        )

    async def _cancel_handler(self, message: dict, task: dict) -> dict:
        """
        budget.cancel skill handler — VOID a committed checkout (§12.1 cascade).

        Calls cancel_checkout on the merchant to release the booking a checkout
        holds.  Idempotent and safe: cancelling an unknown/already-cancelled id
        returns a success result rather than an error, so the caller never has to
        special-case a missing or superseded checkout_id.
        """
        payload = self._extract_payload(message)
        if payload is None:
            raise ValueError(
                "budget.cancel requires a data part with JSON payload "
                "{user_id, checkout_id, autonomy_level?}"
            )

        user_id: str = payload.get("user_id", "")
        checkout_id: str = payload.get("checkout_id", "")
        autonomy_level: str = payload.get("autonomy_level", "L2")

        if not user_id:
            raise ValueError("user_id is required")
        if not checkout_id:
            raise ValueError("checkout_id is required")

        result_data = await self._cancel_merchant(
            user_id=user_id,
            checkout_id=checkout_id,
            autonomy_level=autonomy_level,
        )

        return _new_artifact(
            name="budget.cancel.result",
            parts=[_data_part(result_data)],
        )

    async def _enforce_handler(self, message: dict, task: dict) -> dict:
        """
        budget.enforce skill handler.

        Expects the first data part to carry the BudgetEnforceInput JSON.
        Returns a BudgetResult artifact.
        """
        # Parse input from data part (or fall back to JSON in text part)
        payload = self._extract_payload(message)
        if payload is None:
            raise ValueError(
                "budget.enforce requires a data part with JSON payload "
                "{user_id, line_items, ...}"
            )

        user_id: str = payload.get("user_id", "")
        line_items: list[dict] = payload.get("line_items", [])
        buyer_consent: bool = bool(payload.get("buyer_consent", False))
        ap2_mandate: dict | None = payload.get("ap2_mandate")
        autonomy_level: str = payload.get("autonomy_level", "L2")
        idempotency_key: str | None = payload.get("idempotency_key")
        # D3 / gap #60: forward user budget to legacy path (mirrors _check_merchant SEV-1b)
        total_budget_cents: int = int(payload.get("total_budget_cents") or 0)
        # SIMULATED prepaid wallet binding (mirrors _check_merchant) — back-compat
        # for the legacy combined path: absent → no wallet logic.
        wallet_session_id: str | None = payload.get("wallet_session_id")

        if not user_id:
            raise ValueError("user_id is required")
        if not line_items:
            raise ValueError("line_items must be a non-empty list")

        # Call the live merchant
        result_data = await self._call_merchant(
            user_id=user_id,
            line_items=line_items,
            total_budget_cents=total_budget_cents,
            buyer_consent=buyer_consent,
            ap2_mandate=ap2_mandate,
            autonomy_level=autonomy_level,
            idempotency_key=idempotency_key,
            wallet_session_id=wallet_session_id,
            task=task,
        )

        # For needs_consent / needs_mandate paths, transition Task to input-required
        # (A2A signal that the orchestrator must supply more input before proceeding)
        if result_data["decision"] in ("needs_consent", "needs_mandate"):
            _task_transition(task, "input-required")

        return _new_artifact(
            name="budget.enforce.result",
            parts=[_data_part(result_data)],
        )

    # ------------------------------------------------------------------
    # Merchant integration
    # ------------------------------------------------------------------

    async def _check_merchant(
        self,
        *,
        user_id: str,
        line_items: list[dict],
        total_budget_cents: int,
        autonomy_level: str,
        idempotency_key: str | None,
        wallet_session_id: str | None = None,
    ) -> dict[str, Any]:
        """
        SEV-1a CHECK phase: create_checkout only — no complete_checkout.

        Passes total_budget_cents as user_budget_cents so checkout.go stores it
        on the session and enforces it at complete_checkout time (SEV-1b).

        When wallet_session_id is supplied it is threaded into create_checkout so
        the merchant binds the session to the SIMULATED prepaid wallet (commit
        debits it; cancel credits it). Absent → no wallet logic (back-compat).

        Returns a BudgetCheckResult:
          {decision: "check_ok"|"veto", checkout_id, total_cents, ...}
        """
        meta = {"autonomy_level": autonomy_level}
        client_kwargs: dict[str, Any] = {"timeout": _MERCHANT_TIMEOUT}
        if self._merchant_transport is not None:
            client_kwargs["transport"] = self._merchant_transport

        with httpx.Client(**client_kwargs) as client:
            _checkout_args: dict[str, Any] = {
                "user_id": user_id,
                "line_items": line_items,
                "user_budget_cents": total_budget_cents,  # SEV-1b
            }
            if wallet_session_id:
                _checkout_args["wallet_session_id"] = wallet_session_id
            create_args: dict[str, Any] = {
                "meta": meta,
                "checkout": _checkout_args,
            }
            logger.info(
                "budget.check: create_checkout user=%s items=%d budget=%d¢",
                user_id, len(line_items), total_budget_cents,
            )
            sc, http_code = _mcp_rpc(client, "create_checkout", create_args)
            logger.debug(
                "budget.check: create_checkout response HTTP %d",
                http_code, extra={"resp": _redact(sc)},
            )

            # D3 / gap #10: SUCCESS only if status in {"incomplete","complete"}
            # AND non-empty id AND total_cents present.
            # Any other status ("unavailable", "error", "cancelled", etc.) or
            # empty id or missing/zero total_cents is a HARD FAILURE — return
            # veto/cannot_price, NEVER check_ok.
            merchant_create_status: str = sc.get("status", "")
            _SUCCESS_STATUSES = {"incomplete", "complete"}
            checkout_id: str = sc.get("id", "")
            raw_total = sc.get("total_cents")  # None if absent
            total_cents: int = int(raw_total) if raw_total is not None else 0

            if (
                merchant_create_status not in _SUCCESS_STATUSES
                or not checkout_id
                or raw_total is None
                or total_cents <= 0
            ):
                fail_reason = sc.get("reason", "") or merchant_create_status or "create_failed"
                logger.warning(
                    "budget.check: create_checkout HARD FAILURE — "
                    "status=%r id=%r total_cents=%r reason=%r user=%s",
                    merchant_create_status, checkout_id, raw_total, fail_reason, user_id,
                )
                # M2 — a hard merchant failure (sold out / unavailable / error /
                # malformed response) is NOT a budget veto: nothing was priced,
                # so there is no ceiling to tighten. Emit cannot_price/unavailable
                # so the orchestrator's dedicated honest-terminal branch fires
                # instead of the generic price-veto re-plan loop.
                return _cannot_price_result(
                    checkout_id=checkout_id,
                    total_cents=total_cents,
                    merchant_status=merchant_create_status,
                    veto_reason=fail_reason,
                    idempotency_key=idempotency_key,
                )

            # Python-side pre-veto: if the computed total already exceeds the
            # user's budget, signal veto immediately.  The authoritative veto
            # is checkout.go at complete_checkout time; this is a fast-path
            # signal to avoid the commit attempt.
            if total_cents > total_budget_cents:
                logger.warning(
                    "budget.check: PRE-VETO user=%s total=%d¢ > budget=%d¢ "
                    "checkout_id=%s",
                    user_id, total_cents, total_budget_cents, checkout_id,
                )
                return _veto_result(
                    checkout_id=checkout_id,
                    total_cents=total_cents,
                    veto_reason="price_exceeds_budget",
                    budget_ceiling_cents=total_budget_cents,
                    hard_max_cents=None,
                    idempotency_key=idempotency_key,
                )

            logger.info(
                "budget.check: CHECK_OK user=%s total=%d¢ checkout_id=%s",
                user_id, total_cents, checkout_id,
            )
            return {
                "decision": "check_ok",
                "checkout_id": checkout_id,
                "total_cents": total_cents,
                "currency": "USD",
                "provenance": "merchant",
                "idempotency_key": idempotency_key,
            }

    async def _commit_merchant(
        self,
        *,
        user_id: str,
        checkout_id: str,
        buyer_consent: bool,
        ap2_mandate: dict | None,
        autonomy_level: str,
        idempotency_key: str | None,
        session_total_cents: int = 0,
        task: dict,
    ) -> dict[str, Any]:
        """
        SEV-1a COMMIT phase: complete_checkout only.

        Threads idempotency_key into the merchant call so checkout.go can
        return the same booking_ref on repeat invocations.

        MED fix: session_total_cents is the total from the prior create_checkout
        (budget.check result), supplied by the orchestrator via the payload.  It
        is used as the fallback in _map_complete_response so that
        requires_consent/requires_mandate responses that omit total_cents still
        surface the real amount rather than a misleading 0.
        """
        meta = {"autonomy_level": autonomy_level}
        client_kwargs: dict[str, Any] = {"timeout": _MERCHANT_TIMEOUT}
        if self._merchant_transport is not None:
            client_kwargs["transport"] = self._merchant_transport

        with httpx.Client(**client_kwargs) as client:
            # VULN-003: consent must be set via update_checkout (server-side state)
            # BEFORE calling complete_checkout — never in the complete_checkout body.
            # The Go merchant (checkout.go:377) now ignores c.BuyerConsent from the
            # request body and reads ONLY s.BuyerConsent (set by update_checkout).
            # On failure (non-200 from update_checkout), log a warning and proceed:
            # the Go merchant will then return requires_consent, failing securely.
            if buyer_consent:
                _upd_args: dict[str, Any] = {
                    "meta": meta,
                    # #161 — carry the canonical merchant end-user id so checkout.go's
                    # END-USER OWNERSHIP check (checkoutSession.UserID) can verify this
                    # caller against the identity bound at create_checkout time.
                    "checkout": {"id": checkout_id, "buyer_consent": True, "user_id": user_id},
                }
                _sc_upd, _code_upd = _mcp_rpc(client, "update_checkout", _upd_args)
                if _code_upd not in (200, 201):
                    logger.warning(
                        "budget.commit: update_checkout failed HTTP %d: %s "
                        "— consent not set server-side for checkout %s",
                        _code_upd, _sc_upd, checkout_id,
                    )
                else:
                    logger.info(
                        "budget.commit: update_checkout OK for checkout %s",
                        checkout_id,
                    )

            # D3 / gap #59: do NOT short-circuit to needs_consent with total_cents=0
            # (a misleading placeholder).  Instead, always call complete_checkout and
            # let the merchant return requires_consent with the real session total.
            # This ensures the consent surface always shows the actual amount.
            # NOTE: buyer_consent is intentionally NOT included in the complete_checkout
            # body — consent is server-side only (set above via update_checkout).
            complete_checkout_args: dict[str, Any] = {
                "id": checkout_id,
                # #161 — same rationale as update_checkout above.
                "user_id": user_id,
            }
            if ap2_mandate is not None:
                complete_checkout_args["ap2_mandate"] = ap2_mandate
            if idempotency_key:
                complete_checkout_args["idempotency_key"] = idempotency_key  # SEV-1a

            complete_args: dict[str, Any] = {
                "meta": meta,
                "checkout": complete_checkout_args,
            }
            logger.info(
                "budget.commit: complete_checkout %s buyer_consent=%s "
                "mandate=%s idempotency_key=%s",
                checkout_id, buyer_consent, ap2_mandate is not None, idempotency_key,
            )
            sc2, http_code2 = _mcp_rpc(client, "complete_checkout", complete_args)
            logger.debug(
                "budget.commit: complete_checkout response HTTP %d",
                http_code2, extra={"resp": _redact(sc2)},
            )

            # MED fix: prefer total_cents from the commit response; fall back to
            # session_total_cents (from the prior create_checkout / budget.check)
            # so that requires_consent/requires_mandate with no total_cents field
            # still surface the real amount on the consent surface.
            commit_total: int = int(sc2.get("total_cents") or session_total_cents)

            # Re-use the existing response mapper (same semantics as legacy enforce)
            return self._map_complete_response(
                sc2, http_code2, checkout_id,
                commit_total,
                idempotency_key,
            )

    async def _fund_merchant(
        self,
        *,
        wallet_session_id: str,
        wallet_balance_cents: int,
        user_id: str = "",
    ) -> dict[str, Any]:
        """
        SIMULATED prepaid wallet: create-OR-RESET via the merchant wallet_fund tool.

        `user_id` (#161, optional): the canonical merchant end-user id — binds the
        wallet's end-user ownership dimension (wallet.OwnerUserID in
        ucp-merchant/wallet.go), mirroring the WALLET-001 agent binding, so a
        prod (RequireSignatures) wallet_get is owner-checked on BOTH dimensions.
        Empty string (default) → no binding, same as today (back-compat).

        Returns {decision:"funded"|"error", wallet_session_id, seed_cents,
        balance_cents, simulated, note, provenance:"merchant"}. Never raises on a
        merchant error (best-effort sim/control surface) — returns decision:"error".
        """
        client_kwargs: dict[str, Any] = {"timeout": _MERCHANT_TIMEOUT}
        if self._merchant_transport is not None:
            client_kwargs["transport"] = self._merchant_transport

        with httpx.Client(**client_kwargs) as client:
            fund_args = {
                "wallet_session_id": wallet_session_id,
                "wallet_balance_cents": wallet_balance_cents,
                "user_id": user_id,
            }
            logger.info(
                "budget.fund: wallet_fund session=%s balance=%d¢",
                wallet_session_id, wallet_balance_cents,
            )
            sc, http_code = _mcp_rpc(client, "wallet_fund", fund_args)
            if http_code != 200 or sc.get("status") != "ok":
                return {
                    "decision": "error",
                    "wallet_session_id": wallet_session_id,
                    "reason": sc.get("error", "wallet_fund_failed"),
                    "provenance": "merchant",
                }
            return {
                "decision": "funded",
                "wallet_session_id": wallet_session_id,
                "seed_cents": sc.get("seed_cents"),
                "balance_cents": sc.get("balance_cents"),
                "simulated": sc.get("simulated", True),
                "note": sc.get("note", ""),
                "provenance": "merchant",
            }

    async def _cancel_merchant(
        self,
        *,
        user_id: str,
        checkout_id: str,
        autonomy_level: str,
    ) -> dict[str, Any]:
        """
        VOID phase (§12.1 cascade): cancel_checkout — release a checkout's booking.

        Maps the merchant verdict to a BudgetCancelResult:
          {decision: "cancelled"|"not_owner"|"error", checkout_id,
           released_booking_ref?, reason?, provenance: "merchant"}

        The merchant cancel is idempotent: cancelling an unknown or already-
        cancelled id returns decision "cancelled" (safe success).  Only an
        ownership failure (HTTP 403) maps to "not_owner".
        """
        meta = {"autonomy_level": autonomy_level}
        client_kwargs: dict[str, Any] = {"timeout": _MERCHANT_TIMEOUT}
        if self._merchant_transport is not None:
            client_kwargs["transport"] = self._merchant_transport

        with httpx.Client(**client_kwargs) as client:
            cancel_args = {
                "meta": meta,
                # #161 — same rationale as _commit_merchant above.
                "checkout": {"id": checkout_id, "user_id": user_id},
            }
            logger.info("budget.cancel: cancel_checkout %s user=%s", checkout_id, user_id)
            sc, http_code = _mcp_rpc(client, "cancel_checkout", cancel_args)
            logger.debug(
                "budget.cancel: cancel_checkout response HTTP %d",
                http_code, extra={"resp": _redact(sc)},
            )

            if http_code == 403 or sc.get("reason") == "not_session_owner":
                return {
                    "decision": "not_owner",
                    "checkout_id": checkout_id,
                    "reason": sc.get("reason", "not_session_owner"),
                    "provenance": "merchant",
                }

            if sc.get("cancelled") is True or sc.get("status") == "cancelled":
                out = {
                    "decision": "cancelled",
                    "checkout_id": checkout_id,
                    "released_booking_ref": sc.get("released_booking_ref", ""),
                    "already": sc.get("already"),
                    "provenance": "merchant",
                }
                # SIMULATED prepaid wallet CREDIT (void → refund) — surface the
                # restored balance so the recovery coordinator can emit the credit
                # trace event. Additive: absent when the merchant ran no wallet.
                if sc.get("wallet_credit_cents") is not None:
                    out["wallet_credit_cents"] = sc.get("wallet_credit_cents")
                    out["wallet_balance_cents"] = sc.get("wallet_balance_cents")
                    out["wallet_session_id"] = sc.get("wallet_session_id")
                return out

            return {
                "decision": "error",
                "checkout_id": checkout_id,
                "reason": sc.get("reason") or sc.get("error", "cancel_failed"),
                "provenance": "merchant",
            }

    async def _call_merchant(
        self,
        *,
        user_id: str,
        line_items: list[dict],
        total_budget_cents: int = 0,
        buyer_consent: bool,
        ap2_mandate: dict | None,
        autonomy_level: str,
        idempotency_key: str | None,
        wallet_session_id: str | None = None,
        task: dict,
    ) -> dict[str, Any]:
        """
        Full merchant round-trip (legacy budget.enforce):
          1. create_checkout (all line items as one multi-item session)
          2. complete_checkout (if buyer_consent or ap2_mandate present)

        D3 / gap #60: total_budget_cents forwarded as user_budget_cents to the
        merchant (mirrors _check_merchant SEV-1b) so the user's budget is bound.

        Returns a typed BudgetResult dict.
        """
        meta = {"autonomy_level": autonomy_level}

        client_kwargs: dict[str, Any] = {"timeout": _MERCHANT_TIMEOUT}
        if self._merchant_transport is not None:
            client_kwargs["transport"] = self._merchant_transport

        with httpx.Client(**client_kwargs) as client:
            # --- Step 1: create_checkout ---
            create_checkout_payload: dict[str, Any] = {
                "user_id": user_id,
                "line_items": line_items,
            }
            # D3 / gap #60: bind the user's budget at create time so checkout.go
            # enforces it at complete_checkout rather than using the house ceiling.
            if total_budget_cents > 0:
                create_checkout_payload["user_budget_cents"] = total_budget_cents
            # SIMULATED prepaid wallet binding (legacy path parity with _check_merchant).
            if wallet_session_id:
                create_checkout_payload["wallet_session_id"] = wallet_session_id

            create_args: dict[str, Any] = {
                "meta": meta,
                "checkout": create_checkout_payload,
            }
            logger.info(
                "budget.enforce: create_checkout user=%s items=%d budget=%d¢",
                user_id, len(line_items), total_budget_cents,
            )
            sc, http_code = _mcp_rpc(client, "create_checkout", create_args)
            logger.debug(
                "budget.enforce: create_checkout response HTTP %d",
                http_code, extra={"resp": _redact(sc)},
            )

            # D3 / gap #35: SUCCESS only if status in {"incomplete","complete"}
            # AND non-empty id AND total_cents present.
            # Any other status ("unavailable","error","cancelled",...) or empty id
            # is a HARD FAILURE — return availability-failure, NOT needs_consent.
            legacy_create_status: str = sc.get("status", "")
            _SUCCESS_STATUSES = {"incomplete", "complete"}
            checkout_id: str = sc.get("id", "")
            raw_total_legacy = sc.get("total_cents")  # None if absent
            total_cents: int = int(raw_total_legacy) if raw_total_legacy is not None else 0

            if (
                legacy_create_status not in _SUCCESS_STATUSES
                or not checkout_id
                or raw_total_legacy is None
                or total_cents <= 0
            ):
                fail_reason = (
                    sc.get("reason", "") or legacy_create_status or "create_failed"
                )
                logger.warning(
                    "budget.enforce: create_checkout HARD FAILURE — "
                    "status=%r id=%r total_cents=%r reason=%r user=%s",
                    legacy_create_status, checkout_id, raw_total_legacy,
                    fail_reason, user_id,
                )
                return _veto_result(
                    checkout_id=checkout_id,
                    total_cents=total_cents,
                    veto_reason=fail_reason,
                    budget_ceiling_cents=None,
                    hard_max_cents=None,
                    idempotency_key=idempotency_key,
                )

            # If neither consent nor mandate is present → needs_consent (L2 gate)
            # Return the incomplete checkout so the orchestrator can complete it later.
            # total_cents is the real merchant total (not 0).
            if not buyer_consent and ap2_mandate is None:
                logger.info(
                    "budget.enforce: no consent/mandate — returning needs_consent "
                    "for checkout %s total=%d¢",
                    checkout_id, total_cents,
                )
                return _needs_consent_result(
                    checkout_id=checkout_id,
                    total_cents=total_cents,
                    consent_message=(
                        "buyer_consent required before complete_checkout (HITL gate). "
                        f"Total: {total_cents}¢ USD. "
                        f"Checkout ID: {checkout_id}."
                    ),
                    idempotency_key=idempotency_key,
                )

            # --- Step 2: update_checkout (set server-side consent) + complete_checkout ---
            # VULN-003: consent must be delivered via update_checkout (server-side
            # state), NOT as a field in the complete_checkout body.  The Go merchant
            # ignores c.BuyerConsent from the complete body (checkout.go:377 fix).
            if buyer_consent:
                _upd_args2: dict[str, Any] = {
                    "meta": meta,
                    # #161 — same rationale as _commit_merchant.
                    "checkout": {"id": checkout_id, "buyer_consent": True, "user_id": user_id},
                }
                _sc_upd2, _code_upd2 = _mcp_rpc(client, "update_checkout", _upd_args2)
                if _code_upd2 not in (200, 201):
                    logger.warning(
                        "budget.enforce: update_checkout failed HTTP %d: %s "
                        "— consent not set server-side for checkout %s",
                        _code_upd2, _sc_upd2, checkout_id,
                    )
                else:
                    logger.info(
                        "budget.enforce: update_checkout OK for checkout %s", checkout_id,
                    )

            complete_checkout_args: dict[str, Any] = {
                "id": checkout_id,
                # #161 — same rationale as _commit_merchant.
                "user_id": user_id,
            }
            # NOTE: buyer_consent intentionally excluded from complete body (VULN-003).
            if ap2_mandate is not None:
                complete_checkout_args["ap2_mandate"] = ap2_mandate

            complete_args: dict[str, Any] = {
                "meta": meta,
                "checkout": complete_checkout_args,
            }
            logger.info(
                "budget.enforce: complete_checkout %s buyer_consent=%s mandate=%s",
                checkout_id, buyer_consent, ap2_mandate is not None,
            )
            sc2, http_code2 = _mcp_rpc(client, "complete_checkout", complete_args)
            logger.debug(
                "budget.enforce: complete_checkout response HTTP %d",
                http_code2, extra={"resp": _redact(sc2)},
            )

            return self._map_complete_response(
                sc2, http_code2, checkout_id, total_cents, idempotency_key
            )

    def _map_complete_response(
        self,
        sc: dict[str, Any],
        http_code: int,
        checkout_id: str,
        total_cents: int,
        idempotency_key: str | None,
    ) -> dict[str, Any]:
        """
        Map the merchant's complete_checkout response to a typed BudgetResult.

        Merchant verdict mapping (§3.2 design):
          HTTP 403 + reason "price_exceeds_budget"    → veto
          HTTP 403 + reason "price_exceeds_hard_max"  → veto
          status "requires_consent"                   → needs_consent
          status "requires_mandate"                   → needs_mandate
          status "complete"                           → accept
          status "void" (e.g. "counterparty_insolvent") → unavailable (M3)
          status "error" (e.g. "item_unavailable")     → unavailable (M3)
          status "incomplete" or anything else        → error (raise)

        LOW fix: accept fires ONLY on "complete".  complete_checkout never
        legitimately returns "incomplete" on success — treating it as accept
        would surface a booking that was not actually committed.

        M3 fix: "void"/"error" are DEFINITE merchant-side outcomes (the
        session was voided / the item went unavailable between CHECK and
        COMMIT) — not an ambiguous failure. Mapped to decision "unavailable"
        (mirrors _cannot_price_result's CHECK-time vocabulary) so the caller
        can surface an honest non-reconciliation terminal instead of the
        generic "commit_failed"/needs_reconciliation a raised ValueError would
        otherwise produce.
        """
        # Live total from complete response (merchant re-prices)
        live_total = int(sc.get("total_cents", total_cents))
        merchant_status = sc.get("status", "")

        # --- INSUFFICIENT FUNDS: HTTP 402 from the SIMULATED prepaid wallet gate ---
        # MUST be checked BEFORE the generic 403 branch (a 402 also carries
        # status:"denied"). Distinct from the budget veto: the trip is within the
        # user's budget but exceeds the funded wallet balance → typed terminal.
        if http_code == 402 or sc.get("reason") == "insufficient_funds":
            wallet_balance = sc.get("wallet_balance_cents")
            logger.warning(
                "budget: INSUFFICIENT_FUNDS — merchant 402 total=%d¢ wallet_balance=%s",
                live_total, wallet_balance,
            )
            return _insufficient_funds_result(
                checkout_id=sc.get("id", checkout_id),
                total_cents=live_total,
                wallet_balance_cents=wallet_balance,
                idempotency_key=idempotency_key,
            )

        # --- VETO: HTTP 403 from checkout.go ---
        if http_code == 403 or merchant_status == "denied":
            reason = sc.get("reason", "denied")
            budget_ceiling = sc.get("budget_ceiling_cents")
            hard_max = sc.get("hard_max_cents")
            # Normalize field name differences across reason types
            if budget_ceiling is None and "budget_ceiling_cents" not in sc:
                # price_exceeds_hard_max uses hard_max_cents
                hard_max = sc.get("hard_max_cents")
            logger.warning(
                "budget.enforce: VETO — merchant 403 reason=%s total=%d¢ "
                "ceiling=%s hard_max=%s",
                reason, live_total, budget_ceiling, hard_max,
            )
            return _veto_result(
                checkout_id=sc.get("id", checkout_id),
                total_cents=live_total,
                veto_reason=reason,
                budget_ceiling_cents=budget_ceiling,
                hard_max_cents=hard_max,
                idempotency_key=idempotency_key,
            )

        # --- NEEDS CONSENT (L2 HITL gate not yet met) ---
        if merchant_status == "requires_consent":
            return _needs_consent_result(
                checkout_id=sc.get("id", checkout_id),
                total_cents=live_total,
                consent_message=sc.get(
                    "message", "buyer_consent required (HITL gate)"
                ),
                idempotency_key=idempotency_key,
            )

        # --- NEEDS MANDATE (L3 autonomous gate not met) ---
        if merchant_status == "requires_mandate":
            return _needs_mandate_result(
                checkout_id=sc.get("id", checkout_id),
                total_cents=live_total,
                mandate_message=sc.get(
                    "message", "ap2_mandate required for L3 autonomous checkout"
                ),
                idempotency_key=idempotency_key,
            )

        # --- ACCEPT: complete only (LOW fix: tighten from "complete or incomplete") ---
        # complete_checkout only returns "complete" on success.  An "incomplete"
        # status at complete_checkout time means the booking is NOT committed —
        # treat it as an unexpected/error status, never as accept.
        if merchant_status == "complete":
            booking_ref = sc.get("booking_ref") or None
            logger.info(
                "budget.enforce: ACCEPT — status=%s total=%d¢ booking_ref=%s",
                merchant_status, live_total, booking_ref,
            )
            r = _accept_result(
                checkout_id=sc.get("id", checkout_id),
                total_cents=live_total,
                booking_ref=booking_ref,
                status=merchant_status,
                idempotency_key=idempotency_key,
            )
            # Surface the SIMULATED prepaid wallet draw-down so the orchestrator can
            # emit the side-channel wallet-debit trace event. Additive: absent when
            # the merchant ran no wallet (empty wallet_session_id).
            if sc.get("wallet_session_id"):
                r["wallet_session_id"] = sc.get("wallet_session_id")
                r["wallet_balance_cents"] = sc.get("wallet_balance_cents")
                r["wallet_debit_cents"] = sc.get("wallet_debit_cents")
            return r

        # M3 — a merchant verdict at COMMIT time that is a DEFINITE, never-
        # booked outcome (the session was voided / the item went unavailable
        # between CHECK and COMMIT), not a re-price veto and not an ambiguous
        # failure. Before this fix both fell through to the generic
        # `raise ValueError` below, which _do_commit converts to decision
        # "commit_failed" — documented as "server-side state is AMBIGUOUS,
        # retry to reconcile". A 409 void/unavailable is NOT ambiguous (the
        # merchant told us definitively it will never complete), so advising a
        # same-key retry can never succeed. Mirrors _cannot_price_result's
        # vocabulary ("unavailable"/"cannot_price") — the SAME decision class
        # _do_commit's caller (commit_plan's M2 ladder) already knows how to
        # turn into an honest non-reconciliation terminal.
        if merchant_status == "void":
            reason = sc.get("reason", "counterparty_insolvent")
            logger.warning(
                "budget.enforce: COMMIT-TIME VOID — merchant %d reason=%s total=%d¢",
                http_code, reason, live_total,
            )
            return _cannot_price_result(
                checkout_id=sc.get("id", checkout_id),
                total_cents=live_total,
                merchant_status="unavailable",
                veto_reason=reason,
                idempotency_key=idempotency_key,
            )
        if merchant_status == "error":
            reason = sc.get("reason", "item_unavailable")
            logger.warning(
                "budget.enforce: COMMIT-TIME ERROR — merchant %d reason=%s total=%d¢",
                http_code, reason, live_total,
            )
            return _cannot_price_result(
                checkout_id=sc.get("id", checkout_id),
                total_cents=live_total,
                merchant_status="unavailable",
                veto_reason=reason,
                idempotency_key=idempotency_key,
            )

        # Unknown merchant status — treat as error (includes "incomplete" from
        # complete_checkout, which signals the session was NOT committed).
        raise ValueError(
            f"Unexpected merchant complete_checkout status {merchant_status!r} "
            f"(HTTP {http_code}): {sc}"
        )

    # ------------------------------------------------------------------
    # Input extraction helper
    # ------------------------------------------------------------------

    @staticmethod
    def _extract_payload(message: dict) -> dict | None:
        """Extract the JSON payload from the first data or text part."""
        for part in message.get("parts", []):
            if part.get("kind") == "data":
                data = part.get("data")
                if isinstance(data, dict):
                    return data
                if isinstance(data, str):
                    try:
                        return json.loads(data)
                    except Exception:
                        pass
            elif part.get("kind") == "text":
                text = part.get("text", "")
                try:
                    parsed = json.loads(text)
                    if isinstance(parsed, dict):
                        return parsed
                except Exception:
                    pass
        return None


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
    )
    port = int(os.environ.get("PORT", 9101))
    host = os.environ.get("AGENT_BIND_HOST", "127.0.0.1")

    agent = BudgetAgent(host=host, port=port)
    app = agent.build_app()

    logger.info("Budget agent starting on %s:%d", host, port)
    logger.info("Agent Card: http://%s:%d/.well-known/agent-card.json", host, port)
    logger.info("RPC endpoint: http://%s:%d/", host, port)
    logger.info("Merchant MCP URL: %s", MERCHANT_MCP_URL)

    uvicorn.run(app, host=host, port=port, log_level="info")


if __name__ == "__main__":
    main()
