"""test_llm_enable_thinking_cov3.py — latency guard: every DashScope chat/completions payload must
disable thinking (qwen3.x reasoning models otherwise burn ~300+ reasoning tokens/call → ~110s for a
full negotiate; with enable_thinking:false it's ~6s). This invariant test fails if a new LLM call is
added without the flag, or an existing one drops it.
"""

from __future__ import annotations

import os
import re
import unittest

_SOC = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Every module that builds a DashScope chat body. Two call patterns exist:
#   (a) inline POST to the full ".../compatible-mode/v1/chat/completions" URL, and
#   (b) delegation to utils.model_router.dashscope_chat(...), where model_router builds the
#       "{base}/chat/completions" URL via f-string (so the full literal never appears in the
#       caller). Either way the *caller* owns the request body and therefore the enable_thinking
#       flag — model_router forwards the body verbatim and does NOT inject it. So the invariant
#       must be checked at every body-building call site, not only at the inline-URL ones.
_LLM_MODULES = [
    "utils/intent_parser.py",
    "utils/coverage_gap.py",
    "utils/itinerary_narrator.py",   # LLM-ON demo core — must never regress to thinking-on
    "utils/aftercare_lang.py",
    "utils/followup_parser.py",
    "agents/accommodation_agent.py",
    "agents/compliance_agent.py",
    "agents/destination_agent.py",
    "agents/fraud_agent.py",
    "agents/health_agent.py",
    "agents/insurance_agent.py",
    "agents/risk_agent.py",
]

# model_router is the shared transport: it builds the URL and forwards the caller's body verbatim
# (it never injects enable_thinking), so it is intentionally exempt from the body-flag invariant.
_TRANSPORT_EXEMPT = {"utils/model_router.py"}

_THINK_OFF = re.compile(r'"enable_thinking"\s*:\s*False')
# detects either call pattern: inline full URL, or delegation to dashscope_chat(...)
_MAKES_CALL = re.compile(r'compatible-mode/v1/chat/completions|\bdashscope_chat\s*\(')


class TestEnableThinkingDisabled(unittest.TestCase):
    def test_every_llm_module_disables_thinking(self):
        # Curated invariant: every known LLM body-builder disables thinking. Checked
        # unconditionally — NOT gated on the inline-URL literal, which delegating modules lack.
        missing = []
        for rel in _LLM_MODULES:
            src = open(os.path.join(_SOC, rel), encoding="utf-8").read()
            if not _THINK_OFF.search(src):
                missing.append(rel)
        self.assertEqual(missing, [], f"LLM modules missing enable_thinking:False -> {missing}")

    def test_no_unguarded_chat_completions_appears(self):
        # Auto-discovery belt-and-suspenders: any production module that makes a DashScope chat
        # call (inline URL OR via dashscope_chat) must (1) disable thinking and (2) be enumerated
        # in _LLM_MODULES, so a newly-added call site cannot silently escape the curated invariant.
        listed = set(_LLM_MODULES)
        for root, _dirs, files in os.walk(_SOC):
            if "/tests" in root or "/node_modules" in root or "/.git" in root:
                continue
            for fn in files:
                if not fn.endswith(".py"):
                    continue
                path = os.path.join(root, fn)
                rel = os.path.relpath(path, _SOC)
                if rel in _TRANSPORT_EXEMPT:
                    continue
                src = open(path, encoding="utf-8").read()
                if not _MAKES_CALL.search(src):
                    continue
                if not _THINK_OFF.search(src):
                    self.fail(f"{rel} makes a DashScope chat call without enable_thinking:False")
                if rel not in listed:
                    self.fail(f"{rel} makes a DashScope chat call but is not enumerated in _LLM_MODULES")


if __name__ == "__main__":
    unittest.main(verbosity=2)
