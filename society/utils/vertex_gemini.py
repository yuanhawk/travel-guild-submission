"""vertex_gemini.py — Vertex AI Gemini (gemini-2.5-flash-lite, cheapest) GROUNDED client for OFFLINE
data seeding ONLY.

Build-time tool: enriches the SIMULATED demo catalog (more real, operating lodgings for thin cities)
and resolves ambiguous business statuses, grounded in live Google Search. It is NOT part of the
runtime agent stack — runtime stays Qwen + deterministic (var-0). Every seeded row is provenance-
tagged (gemini-2.5-flash-lite-grounded + as_of) so the source is honest and auditable.

Auth: the service-account key at GOOGLE_APPLICATION_CREDENTIALS (project read from the key). NEVER
raises — returns None on any failure so a seeding run degrades gracefully.
"""

from __future__ import annotations

import json
import os
import re as _re
from typing import Any

import httpx

_VERTEX_LOC_RE = _re.compile(r'^[a-z][a-z0-9-]{1,30}$')  # e.g. us-central1, europe-west4
_KEY = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS", "")
_LOCATION = os.environ.get("VERTEX_LOCATION", "us-central1")
if not _VERTEX_LOC_RE.match(_LOCATION):
    raise ValueError(f"Invalid VERTEX_LOCATION {_LOCATION!r}: must match ^[a-z][a-z0-9-]{{1,30}}$")
_MODEL = os.environ.get("VERTEX_MODEL", "gemini-2.5-flash-lite")
_SCOPES = ["https://www.googleapis.com/auth/cloud-platform"]

_creds = None  # cached service-account credentials (refreshed on demand)


def available() -> bool:
    return bool(_KEY) and os.path.exists(_KEY)


def _project() -> str | None:
    try:
        return json.load(open(_KEY, encoding="utf-8")).get("project_id")
    except Exception:
        return None


def _token() -> str | None:
    global _creds
    try:
        from google.oauth2 import service_account
        if _creds is None:
            _creds = service_account.Credentials.from_service_account_file(_KEY, scopes=_SCOPES)
        if not _creds.valid:
            import google.auth.transport.requests  # only needed for refresh; requests pkg required
            _creds.refresh(google.auth.transport.requests.Request())
        return _creds.token
    except Exception:
        return None


def generate_grounded(prompt: str, grounded: bool = True) -> str | None:
    """generateContent, optionally grounded in Google Search. Returns the model text, or None on any
    failure. NEVER raises. Offline-seeding use only.

    grounded=True adds live Google-Search grounding (fresh, NOT-stale data, but billed per grounded
    prompt — the cost driver). grounded=False uses model knowledge only (≈token cost — cheap, for the
    long-tail where freshness matters less)."""
    if not available():
        return None
    proj = _project()
    tok = _token()
    if not (proj and tok):
        return None
    url = (f"https://{_LOCATION}-aiplatform.googleapis.com/v1/projects/{proj}"
           f"/locations/{_LOCATION}/publishers/google/models/{_MODEL}:generateContent")
    body: dict[str, Any] = {
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
    }
    if grounded:
        body["tools"] = [{"googleSearch": {}}]
    try:
        r = httpx.post(url, headers={"Authorization": "Bearer " + tok,
                                     "Content-Type": "application/json"}, json=body, timeout=90)
        d = r.json()
        if "error" in d:
            return None
        return d["candidates"][0]["content"]["parts"][0]["text"]
    except Exception:
        return None


def generate_json(prompt: str, grounded: bool = True) -> dict | list | None:
    """Generation returning parsed JSON (strips ```json fences). grounded=False skips Google-Search
    grounding (cheap, for the long-tail). None on any failure."""
    txt = generate_grounded(prompt, grounded=grounded)
    if not txt:
        return None
    s = txt.strip()
    if s.startswith("```"):
        s = s.split("```", 2)[1] if "```" in s[3:] else s.lstrip("`")
        s = s[4:] if s.lower().startswith("json") else s
        s = s.rsplit("```", 1)[0] if "```" in s else s
    # also tolerate prose around a JSON object/array
    for lo, hi in (("{", "}"), ("[", "]")):
        if lo in s and hi in s:
            cand = s[s.index(lo):s.rindex(hi) + 1]
            try:
                return json.loads(cand)
            except Exception:
                continue
    return None
