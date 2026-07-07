"""model_router.py — DashScope model tier selection + per-model token tracking.

Provides three pure/state primitives and one HTTP helper:

  pick_model(role)             → str   active model for this call
  record_usage(model, tokens)          locked-persist total_tokens increment
  mark_exhausted(model)                force-rotate on quota 4xx
  dashscope_chat(role, body,   → dict  full DashScope call: pick + post + record
                 *, timeout, _post)

Environment variables:
  SOCIETY_MODEL_PROFILE      "test" | "demo"  (default: "demo")
  SOCIETY_TEST_MODELS        comma-separated list (default: "qwen-flash")
  SOCIETY_DEMO_MODELS        comma-separated list (default: "qwen-plus,qwen-max")
  SOCIETY_FAST_MODELS        cheap/fast tier for latency-sensitive roles (default: "qwen-flash")
  SOCIETY_FAST_ROLES         roles that use the fast tier (default: "narrator,translate")
  SOCIETY_MODEL_TOKEN_CAP    int threshold per model (default: 950_000)
  DASHSCOPE_API_KEY          required for real calls

State files (society root dir, mirrors .seed_done.json convention):
  .token_counts.json   — {"model_name": total_tokens_int, ...}
  .token_counts.lock   — advisory exclusive-lock file (fcntl LOCK_EX)

var-0 / determinism guarantee:
  This module is COMPLETELY INERT on the deterministic (LLM-off) path.
  pick_model / record_usage / dashscope_chat are only reached from inside
  DASHSCOPE_API_KEY-gated branches in the call sites.  No global state is
  read or written at import time.

Backward compat:
  If .token_counts.json is absent (first run), pick_model returns the first
  model in the profile list.  If the state file is unreadable / corrupt,
  pick_model degrades safely to the first model with a logged warning.
"""

from __future__ import annotations

import fcntl
import json
import logging
import os
from typing import Any

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Paths — mirrors .seed_done.json in the society root dir
# ---------------------------------------------------------------------------
_SOCIETY_ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
_COUNTS_FILE = os.path.join(_SOCIETY_ROOT, ".token_counts.json")
_LOCK_FILE = os.path.join(_SOCIETY_ROOT, ".token_counts.lock")
_DASHSCOPE_BASE_URL = "https://dashscope-intl.aliyuncs.com/compatible-mode/v1"

# ---------------------------------------------------------------------------
# Model tiers
# ---------------------------------------------------------------------------
_TOKEN_CAP = int(os.environ.get("SOCIETY_MODEL_TOKEN_CAP", 950_000))

_PROFILE = os.environ.get("SOCIETY_MODEL_PROFILE", "demo").strip().lower()

_TEST_MODELS: list[str] = [
    m.strip()
    for m in os.environ.get("SOCIETY_TEST_MODELS", "qwen-flash").split(",")
    if m.strip()
]
_DEMO_MODELS: list[str] = [
    m.strip()
    for m in os.environ.get("SOCIETY_DEMO_MODELS", "qwen-plus,qwen-max").split(",")
    if m.strip()
]
# Latency-sensitive roles keep a FAST/cheap model in the demo profile: the narrator
# scales max_tokens to ~9000 on big multi-leg itineraries and qwen-plus/qwen-max would
# blow the ~90s timeout (qwen-flash/turbo are several× faster); translate is trivial.
_FAST_MODELS: list[str] = [
    m.strip()
    for m in os.environ.get("SOCIETY_FAST_MODELS", "qwen-flash").split(",")
    if m.strip()
]
_FAST_ROLES: frozenset[str] = frozenset(
    r.strip().lower()
    for r in os.environ.get("SOCIETY_FAST_ROLES", "narrator,translate").split(",")
    if r.strip()
)

_ACTIVE_LIST: list[str] = _TEST_MODELS if _PROFILE == "test" else _DEMO_MODELS


# ---------------------------------------------------------------------------
# State helpers — locked JSON read/write (mirrors seed_city_data_vertex.py)
# ---------------------------------------------------------------------------

def _read_counts() -> dict[str, int]:
    """Read the persisted token-count map.  Returns {} on any read failure."""
    try:
        with open(_COUNTS_FILE, "r", encoding="utf-8") as fh:
            data = json.load(fh)
            if isinstance(data, dict):
                return {k: int(v) for k, v in data.items() if isinstance(v, (int, float))}
    except (FileNotFoundError, json.JSONDecodeError, OSError, ValueError):
        pass
    return {}


def _write_counts(counts: dict[str, int]) -> None:
    """Write the token-count map, creating the file if necessary."""
    os.makedirs(os.path.dirname(_COUNTS_FILE) or ".", exist_ok=True)
    with open(_COUNTS_FILE, "w", encoding="utf-8") as fh:
        json.dump(counts, fh, separators=(",", ":"))


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def _models_for_role(role: str) -> list[str]:
    """Tier list for a role. In the DEMO profile, latency-sensitive roles
    (narrator/translate) use the fast/cheap tier so they don't blow the ~90s
    timeout on big itineraries; every other role uses the demo tier. The TEST
    profile uses one cheap tier for all roles."""
    if _PROFILE == "test":
        return _TEST_MODELS
    if (role or "").strip().lower() in _FAST_ROLES:
        return _FAST_MODELS
    return _DEMO_MODELS


def pick_model(role: str) -> str:
    """Return the active model for this call, honoring per-role tiers.

    Walks the role's tier list and returns the FIRST model whose cumulative
    token count is below _TOKEN_CAP.  If ALL are at or above the cap, returns
    the LAST model and logs a warning (pool exhausted — demo must continue).

    Reads the state file at call time (NOT import time) → var-0 safe.
    """
    active = _models_for_role(role)
    try:
        counts = _read_counts()
    except Exception:  # noqa: BLE001
        logger.warning("model_router: could not read %s — defaulting to first model", _COUNTS_FILE)
        counts = {}

    for model in active:
        if counts.get(model, 0) < _TOKEN_CAP:
            return model

    # All exhausted — warn and return last (must not silence the demo).
    logger.warning(
        "model_router: ALL models for role '%s' (profile '%s') reached token cap %d. "
        "Pool is dry — falling back to last model '%s'. Reset %s to continue.",
        role, _PROFILE, _TOKEN_CAP, active[-1], _COUNTS_FILE,
    )
    return active[-1]


def record_usage(model: str, total_tokens: int) -> None:
    """Atomically increment model's persisted token counter by total_tokens.

    Uses fcntl LOCK_EX (blocking) — mirrors the seed-script lockfile pattern.
    Never raises; logs and returns on any I/O error.
    """
    if total_tokens <= 0:
        return  # nothing to record
    try:
        with open(_LOCK_FILE, "a", encoding="utf-8") as lock_fh:
            fcntl.flock(lock_fh, fcntl.LOCK_EX)
            try:
                counts = _read_counts()
                counts[model] = counts.get(model, 0) + total_tokens
                _write_counts(counts)
                logger.debug(
                    "model_router: recorded %d tokens for %s (total=%d)",
                    total_tokens, model, counts[model],
                )
            finally:
                fcntl.flock(lock_fh, fcntl.LOCK_UN)
    except Exception as exc:  # noqa: BLE001
        logger.warning("model_router: failed to record usage for %s: %s", model, exc)


def mark_exhausted(model: str) -> None:
    """Force a model's counter to the cap so pick_model skips it immediately.

    Call this when a 4xx quota/throttle error is received for `model`, so the
    next call rotates to the next tier member without retrying the exhausted one.
    """
    try:
        with open(_LOCK_FILE, "a", encoding="utf-8") as lock_fh:
            fcntl.flock(lock_fh, fcntl.LOCK_EX)
            try:
                counts = _read_counts()
                counts[model] = max(counts.get(model, 0), _TOKEN_CAP)
                _write_counts(counts)
                logger.warning(
                    "model_router: marked %s as exhausted (counter set to cap=%d)",
                    model, _TOKEN_CAP,
                )
            finally:
                fcntl.flock(lock_fh, fcntl.LOCK_UN)
    except Exception as exc:  # noqa: BLE001
        logger.warning("model_router: failed to mark %s exhausted: %s", model, exc)


def dashscope_chat(
    role: str,
    body: dict[str, Any],
    *,
    timeout: float = 30.0,
    _post=None,  # test injection seam: callable(url, *, headers, json) -> dict
) -> dict[str, Any]:
    """Make a DashScope chat/completions request with auto model selection.

    Picks the active model via pick_model(role), injects it into a copy of
    `body`, posts to DashScope, records token usage, and returns the parsed
    response dict.

    On HTTP 4xx (quota / throttle): calls mark_exhausted(model) THEN re-raises
    the httpx.HTTPStatusError so callers can handle it as usual.

    Args:
        role:    logical role hint passed to pick_model (e.g. "default",
                 "narrator", "translate", "refine").
        body:    request body dict WITHOUT a "model" key; the router injects it.
                 body is NOT mutated — a shallow copy is used.
        timeout: per-request timeout in seconds (default 30.0).
        _post:   optional test injection seam.  When provided, called as
                 _post(url, *, headers, json) → dict in place of httpx.post.
                 record_usage is still called with any "usage" in the result.

    Returns:
        Parsed JSON response dict from DashScope (same structure as the raw
        resp.json() return — callers access data["choices"][0] etc. as before).

    Raises:
        httpx.HTTPStatusError: on HTTP 4xx/5xx (after mark_exhausted on 4xx).
        httpx.RequestError:    on transport/timeout errors.
        Exception:             on JSON parse failures (caller catches as needed).
    """
    model = pick_model(role)
    req_body = dict(body)  # shallow copy — do NOT mutate caller's dict
    req_body["model"] = model

    api_key = os.environ.get("DASHSCOPE_API_KEY", "")
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    url = f"{_DASHSCOPE_BASE_URL}/chat/completions"

    if _post is not None:
        # Test seam: skip real HTTP, use injected callable.
        data: dict[str, Any] = _post(url, headers=headers, json=req_body)
    else:
        import httpx  # local import: real-HTTP path only (keeps the _post test seam httpx-free)
        resp = httpx.post(url, headers=headers, json=req_body, timeout=timeout)
        try:
            resp.raise_for_status()
        except httpx.HTTPStatusError as _exc:
            # Use _exc.response.status_code (the real httpx.Response) rather than
            # resp.status_code so tests that use a minimal mock object still work.
            if _exc.response.status_code < 500:  # 4xx = quota/auth → exhaust
                mark_exhausted(model)
            raise
        data = resp.json()

    # Record usage regardless of whether _post was used.
    total_tokens = 0
    try:
        usage = data.get("usage") or {}
        total_tokens = int(usage.get("total_tokens", 0))
    except (AttributeError, TypeError, ValueError):
        pass
    if total_tokens > 0 and _post is None:
        record_usage(model, total_tokens)

    return data
