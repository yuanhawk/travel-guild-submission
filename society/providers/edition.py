"""
providers.edition — edition selection + dynamic prod-provider loading.

ONE place reads the ``TG_EDITION`` env knob and decides which OVERLAY provider
implementation to hand back. Default edition is ``uat`` (the public submission),
which always resolves to the in-repo SeededProvider so the seeded path is
byte-identical to today.

  TG_EDITION=uat   → in-repo SeededProvider (default)
  TG_EDITION=prod  → LiveProvider dynamically imported from the PRIVATE prod
                     module named by ``TG_PROVIDER_MODULE`` (default
                     "tg_prod.providers"); if that module is absent or does not
                     expose the expected factory, we FALL BACK to SeededProvider
                     and log a warning. The prod module is intentionally NOT in
                     this public repo — the fallback is the correct behaviour here.

VAR-0 NOTE
----------
Nothing in this module is on the deterministic plan path. It only chooses which
DISPLAY-ONLY overlay provider to construct. The default (uat → SeededProvider)
performs no live work, so importing/using this module cannot perturb the seeded
digest, day_plans, or any cache key.
"""

from __future__ import annotations

import importlib
import logging
import os
from typing import Any

logger = logging.getLogger(__name__)

# Edition env knob (NAMES only — never a secret).
_EDITION_ENV = "TG_EDITION"
_DEFAULT_EDITION = "uat"

# The dotted module path the prod edition exposes its provider factories from.
# Overridable so the private repo can name its module whatever it likes. The
# module is ABSENT in this public repo; that is correct and triggers fallback.
_PROVIDER_MODULE_ENV = "TG_PROVIDER_MODULE"
_DEFAULT_PROVIDER_MODULE = "tg_prod.providers"


def current_edition() -> str:
    """Return the active edition tag, lower-cased. Default ``uat``.

    Any value other than "prod" is treated as "uat" (fail-safe to seeded).
    """
    val = (os.environ.get(_EDITION_ENV) or "").strip().lower()
    return val or _DEFAULT_EDITION


def is_prod() -> bool:
    """True iff the edition is explicitly ``prod``."""
    return current_edition() == "prod"


def _prod_module_name() -> str:
    return (os.environ.get(_PROVIDER_MODULE_ENV) or "").strip() or _DEFAULT_PROVIDER_MODULE


def load_prod_factory(factory_name: str) -> Any | None:
    """Dynamically import the prod provider module and return ``factory_name``.

    Returns None (with a warning) if the edition is not prod, the module is
    absent (the public-repo case), or the attribute is missing. Never raises —
    the caller falls back to the SeededProvider.
    """
    if not is_prod():
        return None
    mod_name = _prod_module_name()
    try:
        mod = importlib.import_module(mod_name)
    except Exception as exc:  # noqa: BLE001 — absent prod module is the normal public-repo case
        logger.warning(
            "providers.edition: TG_EDITION=prod but prod module %r not importable "
            "(%s) — falling back to SeededProvider.", mod_name, exc,
        )
        return None
    factory = getattr(mod, factory_name, None)
    if factory is None:
        logger.warning(
            "providers.edition: prod module %r has no %r — falling back to "
            "SeededProvider.", mod_name, factory_name,
        )
    return factory
