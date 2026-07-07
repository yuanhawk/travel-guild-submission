#!/usr/bin/env bash
# demo.sh — run the narrated Travel Guild demo (5-beat transcript).
#
# Renders a clean, labeled transcript of:
#   ① intent  ② negotiation + real 403 veto  ③ variance contrast
#   ④ derailment recovery  ⑤ close
#
# The live beats (②, ④) drive the real society against a live local Go UCP
# merchant (see README.md's Quickstart to start it). No LLM key required for
# this default LLM-off run.
#
# Usage:
#   ./demo.sh                 # runs against a merchant on 127.0.0.1:8090
#   MERCHANT_MCP_URL=http://127.0.0.1:8090/api/ucp/mcp ./demo.sh   # explicit
#   DEMO_PACE=0.6 ./demo.sh   # add reading pauses (nice for screen recording)
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "==> Running demo (set MERCHANT_MCP_URL to reach a live merchant; defaults to 127.0.0.1:8090)"
exec env DEMO_PACE="${DEMO_PACE:-0}" python3 "${REPO}/society/demo.py" "$@"
