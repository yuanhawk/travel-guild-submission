"""aftercare_lang.py — #100 AFTERCARE: nationality→lang mapping + DASHSCOPE translation.

OFF var-0 (post-booking, like the narrator). Mirrors itinerary_narrator.narrate (L78-127)
for the DASHSCOPE call pattern.

HONESTY:
  - translate_alert returns None on missing key / network / empty → caller shows English
    original with translated:False (never fabricates a translation).
  - en path makes no LLM call (trivial identity, no waste).
  - Never raises.
"""
from __future__ import annotations

import logging
import os
import re

logger = logging.getLogger(__name__)

# Nationality (ISO2 country code) → BCP-47 language tag.
# SG/US/GB/AU and unknown → "en" (English).
NATIONALITY_LANG: dict[str, str] = {
    "DE": "de",
    "FR": "fr",
    "JP": "ja",
    "CN": "zh",
    "TW": "zh",
    "ES": "es",
    "IT": "it",
    "BR": "pt",
    "KR": "ko",
    "TH": "th",
    "ID": "id",
    "MY": "ms",
    "VN": "vi",
    "AR": "es",
    "MX": "es",
    "RU": "ru",
    "NL": "nl",
    "SE": "sv",
    "NO": "no",
    "DK": "da",
    "FI": "fi",
    "PT": "pt",
    "PL": "pl",
    "CZ": "cs",
    "HU": "hu",
    "RO": "ro",
    "TR": "tr",
    "IL": "he",
    "SA": "ar",
    "AE": "ar",
    "EG": "ar",
}

_DASHSCOPE_API_KEY = os.environ.get("DASHSCOPE_API_KEY", "")
_DASHSCOPE_BASE_URL = "https://dashscope-intl.aliyuncs.com/compatible-mode/v1"
_TRANSLATE_MODEL = os.environ.get("SOCIETY_TRANSLATE_MODEL", "qwen-turbo")


# prefs.lang is caller-controlled and flows into the translation LLM's target-language
# slot (translate_alert below). Validate its shape before use so it can only ever be a
# short language(-region) tag, never arbitrary free text.
_BCP47_SHAPE_RE = re.compile(r"^[a-z]{2,3}(-[a-z]{2,4})?$")


def resolve_lang(user: dict | None) -> str:
    """prefs.lang override > NATIONALITY_LANG[nationality] > 'en'. Pure."""
    if not isinstance(user, dict):
        return "en"
    prefs = user.get("prefs") or {}
    if isinstance(prefs, dict) and prefs.get("lang"):
        candidate = str(prefs["lang"]).strip().lower()
        if _BCP47_SHAPE_RE.match(candidate):
            return candidate
        # Malformed/oversized value -- fall through to the nationality-based default
        # rather than pass it on.
    nat = (user.get("nationality") or "").strip().upper()
    return NATIONALITY_LANG.get(nat, "en")


def translate_alert(text: str, target_lang: str, *, _post=None) -> str | None:
    """Translate one alert string into target_lang via DASHSCOPE qwen (enable_thinking
    False, max_tokens ~400, response NOT json_object). System prompt restricts output
    to the translation only.

    Returns the translation string, or None on missing key / network / empty (honest
    fallback — None means 'show the English original'). NEVER raises.
    NEVER fabricates — None is the honest signal to fall back.

    `_post` is an injection seam for tests (callable(url, headers, json) → resp_dict).
    """
    if not text:
        return None
    api_key = os.environ.get("DASHSCOPE_API_KEY", "") or _DASHSCOPE_API_KEY
    if not api_key and _post is None:
        return None  # LLM-off / key not configured → honest English fallback

    try:
        from utils.model_router import dashscope_chat
    except ImportError:
        from model_router import dashscope_chat  # type: ignore[no-redef]

    system_prompt = (
        f"Translate this travel safety alert into {target_lang}. "
        "Output ONLY the translation. Add NO information."
    )
    body = {
        "enable_thinking": False,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": text},
        ],
        "max_tokens": 400,
    }
    try:
        data = dashscope_chat("translate", body, timeout=30.0, _post=_post)
        msg = (data.get("choices") or [{}])[0].get("message") or {}
        content = msg.get("content")
        if not isinstance(content, str) or not content.strip():
            return None
        return content.strip()
    except Exception as exc:  # noqa: BLE001 — translation failure → None (honest fallback)
        logger.warning("translate_alert: failed (%s) → English fallback", exc)
        return None
