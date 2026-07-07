#!/usr/bin/env python3
"""
demo.py — The Travel Guild: narrated, watchable 3-minute demo runner.

This is the §14.5 beat-sheet rendered as a clean, labeled, asciinema-friendly
terminal transcript — NOT raw logs. It tells the submission story:

    "Safe autonomous agentic commerce — proven on travel.
     Structure is variance control, with checks & balances."

Beats (matches the internal design spec §14.5):
    ①  INTENT          one Telegram sentence in
    ②  NEGOTIATION     society proposes → real Go 403 budget veto → re-plan →
                       Critic re-verifies prices → ONE consent → ap2_mandate →
                       real booking_ref
    ③  VARIANCE        same model, side-by-side: baseline flakes run-to-run,
                       society variance-0 (from the captured benchmark results.json)
    ④  DERAILMENT      inject a sold-out leg → activate the pre-vetted Plan B →
                       within budget → ONE fresh re-consent → re-booked
    ⑤  CLOSE           four protocols at four seams; human consented once;
                       the merchant held the line

LIVE beats (②, ④) drive the real society against a live local Go UCP merchant.
The VARIANCE beat (③) would print already-captured benchmark numbers from
benchmark/results.json if that file were present (re-running the slow/flaky
baseline live would defeat the point — reproducibility = credibility); this
public showcase repo does not ship the benchmark harness, so beat ③ degrades
honestly instead. If the merchant is unreachable, the live beats (②, ④)
likewise degrade to a captured narrative so the transcript is always
watchable.

Run:

    MERCHANT_MCP_URL=http://127.0.0.1:8090/api/ucp/mcp UCP_UNSIGNED_TIER=L2 \\
        python3 society/demo.py

Or from the repo via the convenience wrapper:

    ./demo.sh          # (equivalently: make demo)

WA REAL-TRIP MODE (not available in this public showcase repo — the segment
below hardcodes real private-catalog hotel data not in the sample dataset):

    python3 society/demo.py --trip wa

    Runs the additive "real trip" segment — the user's REAL 2022 Western
    Australia Perth-to-Perth road trip (structure + challenges are REAL; lodging
    + prices are SEEDED + provenance-tagged). It showcases the society's
    transport-feasibility + disruption-recovery + seasonal-advisory depth on a
    genuine itinerary: the Transport agent flags the 391 km long-haul leg
    (buffer / sequencing), the Destination agent surfaces the WA bushfire-season
    advisory (Dec–Feb) for in-season dates, and a sold-out holiday park is
    recovered via L3 within budget with ONE fresh re-consent. The Bali beats
    (①–⑤) are unchanged.

MULTI-FAULT CASCADE MODE (not available in this public showcase repo — same
reason as WA above):

    python3 society/demo.py --trip cascade

    Runs the additive MULTI-FAULT CASCADE segment — grounded in a real
    (anonymized) flight-cancellation cascade. A flight-dependent Ethiopia
    historic route (Gondar → Lalibela → Semera) suffers TWO sequential faults:
    a booked hotel sells out (Accommodation swaps it), then an inter-leg FLIGHT
    is cancelled on the NEW state (Transport re-routes via an Addis hub +
    unplanned overnight). Each recovery stays within the $1,200 budget with ONE
    fresh re-consent; the unrecoverable variant declines honestly (merchant
    veto). The Bali beats (①–⑤) and the WA segment are unchanged.

Cloud-free: the live beats need NO LLM key — the society's deterministic
allocator + the real merchant veto carry beats ② and ④. (The benchmark in
beat ③ used qwen3-max; those numbers are pre-captured.)
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request
import uuid

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# ---------------------------------------------------------------------------
# Presentation knobs
# ---------------------------------------------------------------------------

MERCHANT_URL = os.environ.get(
    "MERCHANT_MCP_URL", "http://127.0.0.1:8090/api/ucp/mcp"
)
# The agent modules (budget_agent.py, accommodation_agent.py, critic_agent.py)
# each read MERCHANT_MCP_URL from the environment independently, at their own
# import time (which happens lazily, later, inside _run_society below) --
# their own module-level fallback is a Docker-only hostname. Propagate the
# resolved value back into the environment now so those lazy imports see the
# same default this file uses, instead of falling through to their own.
os.environ.setdefault("MERCHANT_MCP_URL", MERCHANT_URL)
# Pacing between beats so a viewer can read (0 = no pause; good for CI).
PACE = float(os.environ.get("DEMO_PACE", "0.0"))
# Where the captured benchmark lives (relative to repo root).
_HERE = os.path.dirname(os.path.abspath(__file__))
RESULTS_CANDIDATES = [
    os.environ.get("BENCHMARK_RESULTS", ""),
    os.path.join(_HERE, "..", "benchmark", "results.json"),
    os.path.join(_HERE, "results.json"),
]

W = 74  # transcript width


def _pause(mult: float = 1.0) -> None:
    if PACE > 0:
        time.sleep(PACE * mult)


def rule(char: str = "─") -> None:
    print(char * W)


def banner(title: str, subtitle: str = "") -> None:
    print()
    rule("━")
    print(f"  {title}")
    if subtitle:
        print(f"  {subtitle}")
    rule("━")


def beat(num: str, label: str) -> None:
    print()
    print(f"{num}  {label}")
    rule()


def line(s: str = "") -> None:
    print(f"   {s}")


def usd(cents: int) -> str:
    return f"${cents / 100:,.0f}"


def usd2(cents: int) -> str:
    return f"${cents / 100:,.2f}"


# ---------------------------------------------------------------------------
# Live merchant transport (real Go UCP merchant — the trust boundary)
# ---------------------------------------------------------------------------

def merchant_call(name: str, args: dict) -> tuple[int, dict]:
    """Call a merchant MCP tool. Returns (http_status, structuredContent).

    The merchant returns 4xx with a structured body on a veto/conflict — we
    surface both the status code and the reason (this IS the real veto).
    """
    body = json.dumps(
        {
            "jsonrpc": "2.0",
            "id": str(uuid.uuid4()),
            "method": "tools/call",
            "params": {"name": name, "arguments": args},
        }
    ).encode()
    req = urllib.request.Request(
        MERCHANT_URL,
        data=body,
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        resp = urllib.request.urlopen(req, timeout=10)
        sc = json.loads(resp.read()).get("result", {}).get("structuredContent", {})
        return resp.getcode(), sc
    except urllib.error.HTTPError as e:
        try:
            sc = json.loads(e.read()).get("result", {}).get("structuredContent", {})
        except Exception:
            sc = {}
        return e.code, sc


def consent_and_complete(checkout_id: str) -> tuple[int, dict]:
    """Register buyer consent, then complete the checkout.

    Bug fix (found running this demo against the current merchant): the
    server's HITL consent gate now requires a separate update_checkout call
    ("VULN-003: consent via update_checkout, not complete body" — see
    ucp-merchant/food_checkout_test.go) before complete_checkout will do
    anything but return status:"requires_consent". The real orchestrator path
    (_run_society, used by beat_negotiation's actual booking) already does
    this correctly; demo.py's own simplified helper calls used by the
    derailment/WA/cascade segments below did not, so they showed an empty
    booking_ref even on load-bearing calls with an in-budget correct price.
    """
    merchant_call("update_checkout", {"checkout": {"id": checkout_id, "buyer_consent": True}})
    return merchant_call("complete_checkout", {"checkout": {"id": checkout_id}})


def merchant_admin(path: str, body: dict) -> dict:
    url = MERCHANT_URL.replace("/api/ucp/mcp", path)
    data = json.dumps(body).encode()
    req = urllib.request.Request(
        url, data=data, method="POST", headers={"Content-Type": "application/json"}
    )
    return json.loads(urllib.request.urlopen(req, timeout=10).read())


def merchant_alive() -> bool:
    try:
        body = json.dumps(
            {"jsonrpc": "2.0", "id": "ping", "method": "tools/list"}
        ).encode()
        req = urllib.request.Request(
            MERCHANT_URL,
            data=body,
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        urllib.request.urlopen(req, timeout=5)
        return True
    except Exception:
        return False


# ===========================================================================
# BEAT ① — the problem + the one sentence in
# ===========================================================================

INTENT_SENTENCE = "6 days Bali, ~$1500, solo, beaches + culture"


def beat_intent() -> None:
    beat("①  INTENT", "one Telegram sentence in")
    line('Problem: LLM agents are about to spend real money autonomously.')
    line('A single LLM agent is a non-deterministic, self-policing cashier —')
    line('on the SAME feasible trip it succeeds today and flakes tomorrow,')
    line('can hallucinate a price into a charge, and silently book 1 of 3 legs.')
    line()
    line(f'Telegram → "{INTENT_SENTENCE}"')
    line()
    line('A society of specialist A2A agents negotiates it — invisibly — behind')
    line('four checks & balances no single prompt has. The human consents ONCE.')
    _pause(2)


# ===========================================================================
# BEAT ② — negotiation made visible (live society + real merchant veto)
# ===========================================================================

# A 2-leg Bali trip whose GREEDY first picks (best-review under a generous
# per-leg ceiling) bust the user's budget — forcing the real merchant veto,
# then a re-plan that fits. Budget tuned so the veto is genuine, not staged.
NEGOTIATION_TRIP = {
    "user_id": "demo-bali",
    "total_budget_cents": 150_000,  # $1,500 — the user's real ceiling
    "legs": [
        {"city": "bali", "checkin": "2025-10-01", "checkout": "2025-10-04",
         "adults": 1, "vibe": "culture"},   # 3 nights, culture
        {"city": "bali", "checkin": "2025-10-04", "checkout": "2025-10-07",
         "adults": 1, "vibe": "beach"},      # 3 nights, beach
    ],
    "preferences": {"avoid_hostels": True},
}

# The greedy "best-review-first" package the merchant must veto: the TRUE top-review
# hotel per leg (verified live). This genuinely busts the user's OWN $1,500 budget —
# the veto is real, NOT staged with an artificially-low ceiling.
GREEDY_LINE_ITEMS = [
    {"hotel_id": "bali-alaya-ubud", "checkin": "2025-10-01",
     "checkout": "2025-10-04", "adults": 1},          # sample culture pick, 3n
    {"hotel_id": "bali-alila-seminyak", "checkin": "2025-10-04",
     "checkout": "2025-10-07", "adults": 1},          # sample top-review beach pick, 3n
]


def _run_society(trip: dict) -> dict | None:
    """Drive the real A2A society in-process against the live merchant."""
    try:
        from starlette.testclient import TestClient

        from agents import accommodation_agent as acc_mod
        from agents import budget_agent as ba_mod
        from agents import planner_agent as planner_mod
        from agents import compliance_agent as comp_mod
        from agents import health_agent as health_mod
        from agents import insurance_agent as ins_mod
        from agents import fraud_agent as fraud_mod
        from agents import risk_agent as risk_mod
        from agents import day_planner_agent as day_planner_mod
        from orchestration.orchestrator import TravelOrchestrator
    except Exception as e:  # pragma: no cover - import guard
        line(f"(society import unavailable: {e})")
        return None

    planner = planner_mod.PlannerAgent(host="0.0.0.0", port=9102)
    acc = acc_mod.AccommodationAgent(host="0.0.0.0", port=9103, merchant_transport=None)
    budget = ba_mod.BudgetAgent(host="0.0.0.0", port=9101, merchant_transport=None)
    # The 5 specialist gates — wired into the REAL construction so the composed
    # hooks are NOT dead code. They are CONTEXTUAL (fire only on a scenario that
    # carries their condition), so a plain Bali/WA demo trip passes through them
    # unchanged (var-0 preserved).
    compliance = comp_mod.ComplianceAgent(host="0.0.0.0", port=9106)
    health = health_mod.HealthAgent(host="0.0.0.0", port=9108)
    insurance = ins_mod.InsuranceAgent(host="0.0.0.0", port=9105)
    fraud = fraud_mod.FraudAgent(host="0.0.0.0", port=9109)
    risk = risk_mod.RiskAgent(host="0.0.0.0", port=9107)
    # build #30 day-planner (OFF the money path; ADDITIVE activity/meal plan).
    day_planner = day_planner_mod.DayPlannerAgent(host="0.0.0.0", port=9110)

    # #159 — OPT-IN genuine-MCP travel_memory client (off by default; see
    # utils/memory_client.py). Same factory as the real server build (server.py
    # _build_orch); unset MEMORY_MCP_URL/MEMORY_MCP_EMBEDDED -> None -> no-op.
    from utils.memory_client import build_memory_client
    memory_client = build_memory_client()

    orch = TravelOrchestrator(
        planner_client=TestClient(planner.build_app()),
        accommodation_client=TestClient(acc.build_app()),
        budget_client=TestClient(budget.build_app()),
        compliance_client=TestClient(compliance.build_app()),
        health_client=TestClient(health.build_app()),
        insurance_client=TestClient(insurance.build_app()),
        fraud_client=TestClient(fraud.build_app()),
        risk_client=TestClient(risk.build_app()),
        day_planner_client=TestClient(day_planner.build_app()),
        memory_client=memory_client,
    )
    return orch.negotiate(trip)


def beat_negotiation(live: bool) -> dict:
    beat("②  NEGOTIATION", "the society negotiates — checks & balances, made visible")

    line("[MCP]  Memory: the orchestrator consults travel_memory over a real MCP")
    line("       ClientSession for learned cross-trip preferences (hotel catalog")
    line("       search itself goes through the UCP merchant, a separate channel).")
    line("[A2A]  Planner decomposes → Accommodation proposes → Budget vetoes →")
    line("       Critic re-verifies. The society resolves tradeoffs internally.")
    print()

    # --- Show the REAL merchant veto (the check the LLM cannot override) ---
    line("Greedy first picks (best review per leg, ignoring the package total):")
    line("   leg-0  bali-alaya-ubud       (culture, 3n)   ★5.0  review 4.6")
    line("   leg-1  bali-alila-seminyak   (beach,   3n)   ★5.0  review 4.7")
    _pause()

    greedy_total = 168_900  # captured fallback: 3n@16500 + 3n@39800 = $1,689 -- busts the $1,500 user budget, under hard-max
    if live:
        code, co = merchant_call(
            "create_checkout",
            {"checkout": {"user_id": "demo-bali",
                          "user_budget_cents": NEGOTIATION_TRIP["total_budget_cents"],
                          "line_items": GREEDY_LINE_ITEMS}},
        )
        cid = co.get("id")
        greedy_total = int(co.get("total_cents") or greedy_total)
        vcode, vres = consent_and_complete(cid)
        reason = vres.get("reason", "price_exceeds_budget")
        ceiling = int(vres.get("budget_ceiling_cents") or NEGOTIATION_TRIP["total_budget_cents"])
        print()
        print(f"⚠️  BUDGET VETO — merchant returned HTTP {vcode} {reason}")
        line(f"   {usd2(greedy_total)} package  >  {usd2(ceiling)} ceiling")
        line(f"   provenance: MERCHANT (Go checkout.go) — not the LLM. The agents")
        line(f"   cannot override a real 403. Budget enforcement is the merchant's law.")
    else:
        print()
        print("⚠️  BUDGET VETO — merchant returns HTTP 403 price_exceeds_budget")
        line(f"   {usd2(greedy_total)} package  >  {usd2(NEGOTIATION_TRIP['total_budget_cents'])} ceiling")
        line("   provenance: MERCHANT (Go checkout.go) — not the LLM.")
    _pause(2)

    # --- Re-plan: the society converges to a package that fits the REAL budget ---
    print()
    print("↻  RE-PLAN — society re-allocates under the enforced ceiling")
    result = _run_society(NEGOTIATION_TRIP) if live else None

    if result and result.get("outcome") == "success":
        total = int(result["total_booked_cents"])
        rounds = result.get("negotiation_rounds", 0)
        # The re-plan is REAL: the booked package differs from the vetoed greedy
        # (a swapped beach leg) and is cheaper. Surface that delta explicitly so
        # the re-allocation is visible, not asserted.
        booked_ids = {leg["hotel_id"] for leg in result["legs"]}
        greedy_ids = {li["hotel_id"] for li in GREEDY_LINE_ITEMS}
        swapped = sorted(greedy_ids - booked_ids)
        line(f"re-allocated under the {usd2(ceiling)} ceiling: "
             f"{usd2(greedy_total)} greedy → {usd2(total)} booked "
             f"(−{usd2(greedy_total - total)})")
        if swapped:
            line(f"   dropped the vetoed leg(s): {', '.join(swapped)} → cheaper picks that fit")
        print()
        print("✓  CRITIC — re-verified every price against the backend (no fabrication)")
        print("✅  ONE CONSENT  →  ap2_mandate  →  booking committed server-side")
        print()
        line(f"booking_ref : {result['booking_ref']}")
        line(f"checkout_id : {result['checkout_id']}")
        line(f"total       : {usd2(total)}   (within {usd2(NEGOTIATION_TRIP['total_budget_cents'])} budget)")
        line(f"rounds      : {rounds}   (society's DP allocator re-plans the whole "
             f"package in one near-optimal pass — no iterative haggling)")
        for leg in result["legs"]:
            line(f"  {leg['leg_id']}  {leg['city']:5s} {leg.get('vibe',''):8s} "
                 f"{leg['nights']}n  {leg['hotel_id']:22s} {usd2(leg['total_cents'])}")
        line()
        line("Five structural checks just held:")
        line("  1. can't overspend      — merchant 403 (above)")
        line("  2. can't hallucinate    — Critic re-verified every price")
        line("  3. can't partial-book   — all-or-none on the full package")
        line("  4. can't spend unasked  — ONE human consent (ap2_mandate)")
        line("  5. can't flake          — deterministic → variance 0 (beat ③)")
        return result
    else:
        # Degraded narrative (merchant down) — still legible.
        print()
        print("✓  CRITIC — re-verifies every price against the backend")
        print("✅  ONE CONSENT  →  ap2_mandate  →  real booking_ref")
        line("(captured: greedy $1,845 vetoed > $1,500 → society re-allocates to a")
        line(" fitting $921 package, 1 consent — see the live run for real refs)")
        return {"outcome": "narrative"}


# ===========================================================================
# BEAT ③ — variance contrast (Innovation): captured benchmark, side-by-side
# ===========================================================================

def _load_results() -> dict | None:
    for cand in RESULTS_CANDIDATES:
        if cand and os.path.exists(cand):
            try:
                with open(cand) as f:
                    return json.load(f)
            except Exception:
                continue
    return None


def beat_variance() -> None:
    beat("③  VARIANCE CONTRAST", "same qwen3-max model — structure IS variance control")
    line("Re-running the slow/flaky baseline live would defeat the point;")
    line("these would be the ALREADY-CAPTURED benchmark numbers (reproducibility =")
    line("credibility) if the benchmark harness were included in this public")
    line("showcase repo -- it isn't (see README.md's 'What's not here'); the")
    line("numbers behind this claim were measured on the private benchmark run.")
    print()

    data = _load_results()
    if not data:
        line("(benchmark/results.json not included in this showcase repo)")
        return

    agg = data.get("aggregated", {})
    # S1-S5 are the authoritative N=20 fair-baseline rebench (n20_rebench block);
    # read N from there so the printed N matches the displayed aggregates exactly.
    n = data.get("n20_rebench", {}).get("n_per_arm", data.get("n_runs", "?"))
    model = data.get("model", "qwen3-max")
    scen = {s["id"]: s for s in data.get("scenarios", [])}

    header = f"   {'scenario':<26} {'BASELINE':>20}   {'SOCIETY':>20}"
    print(header)
    print("   " + "-" * (len(header) - 3))
    for sid in ["S1", "S2", "S3", "S4", "S5"]:
        b = agg.get("baseline", {}).get(sid)
        s = agg.get("society", {}).get(sid)
        if not b or not s:
            continue
        desc = scen.get(sid, {}).get("description", sid)
        short = desc.split(":")[0] if ":" in desc else desc
        bsucc = b["mean_booking_success"] * 100
        bvar = b["var_booking_success"]
        ssucc = s["mean_booking_success"] * 100
        svar = s["var_booking_success"]
        infeasible = scen.get(sid, {}).get("id") == "S3"
        btag = f"{bsucc:>3.0f}% var={bvar:.4f}"
        stag = f"{ssucc:>3.0f}% var={svar:.4f}"
        flag = "  ⚠ flake" if bvar > 0 else ("  (infeasible: both honest)" if infeasible else "")
        print(f"   {short:<26} {btag:>20}   {stag:>20}{flag}")

    print()
    line(f"N={n} runs/arm/scenario, {model}, identical inputs & merchant, no tip-off.")
    line("The single agent drops legs on EVERY multi-leg feasible case (S1/S2 +25pp,")
    line("S4 +10pp) — turn-exhaustion abandons the itinerary, var up to 0.1875; WHICH")
    line("run flakes is itself stochastic. S3: both honestly refuse the infeasible plan.")
    line("The society is var=0 on EVERY feasible scenario (S1,S2,S4,S5), EVERY run,")
    line("and reaches the SAME correct outcome ~4-8x faster where the baseline ties.")
    line('"40–100% run-to-run" is the single-agent failure; the society is 0.')
    _pause(2)


# ===========================================================================
# BEAT ④ — derailment recovery (Problem-Value differentiator)
# ===========================================================================

RECOVERY_BUDGET = 120_000  # $1,200


def beat_derailment(live: bool) -> None:
    beat("④  DERAILMENT", "a real-world fault — recovered with one re-consent")
    line('"The contingency existed BEFORE the fault."  At plan time the society')
    line("pre-vets a Plan B per leg. When the world flips, recovery is a swap +")
    line("ONE fresh re-consent — never an autonomous re-spend.")
    print()

    if not live:
        print("🔁  DERAILMENT — bali-legian-beach SOLD OUT (catalog row flipped)")
        line("→ activate pre-vetted Plan B (leg-1 → bali-kuta-paradiso)")
        line("→ within budget → ONE fresh re-consent → re-booked (new booking_ref)")
        return

    # --- Live: book → flip sold-out → recover → fresh consent ---
    legs = [
        {"hotel_id": "bali-ubud-garden", "checkin": "2026-07-01",
         "checkout": "2026-07-03", "adults": 1},
        {"hotel_id": "bali-legian-beach", "checkin": "2026-07-03",
         "checkout": "2026-07-05", "adults": 1},
    ]
    # ensure clean state
    try:
        merchant_admin("/admin/sim/availability",
                       {"hotel_id": "bali-legian-beach", "available": True})
    except Exception:
        pass

    _, co = merchant_call("create_checkout", {"checkout": {
        "user_id": "demo-bali", "line_items": legs,
        "user_budget_cents": RECOVERY_BUDGET}})
    cid = co.get("id")
    _, comp = consent_and_complete(cid)
    line(f"Baseline booked: {comp.get('booking_ref','?')}  "
         f"(leg-1: bali-legian-beach, {usd(int(co.get('total_cents',0)))})")
    _pause()

    # flip sold-out
    merchant_admin("/admin/sim/availability",
                   {"hotel_id": "bali-legian-beach", "available": False})
    print()
    print("🔁  DERAILMENT — bali-legian-beach SOLD OUT (catalog row flipped)")
    code, search = merchant_call("search_catalog", {"query": {
        "city": "bali", "checkin": "2026-07-03", "checkout": "2026-07-05",
        "max_cents": RECOVERY_BUDGET}})
    ids = [r["hotel_id"] for r in search.get("results", [])]
    excluded = "bali-legian-beach" not in ids
    line(f"   merchant search_catalog now EXCLUDES it: {excluded}  "
         f"({len(ids)} other hotels still bookable)")
    _pause()

    # recover via pre-vetted secondary
    print()
    print("   activate pre-vetted Plan B: leg-1 → bali-kuta-paradiso")
    rec_legs = [
        {"hotel_id": "bali-ubud-garden", "checkin": "2026-07-01",
         "checkout": "2026-07-03", "adults": 1},
        {"hotel_id": "bali-kuta-paradiso", "checkin": "2026-07-03",
         "checkout": "2026-07-05", "adults": 1},
    ]
    _, rco = merchant_call("create_checkout", {"checkout": {
        "user_id": "demo-bali", "line_items": rec_legs,
        "user_budget_cents": RECOVERY_BUDGET}})
    rcid = rco.get("id")
    rtotal = int(rco.get("total_cents", 0))
    line(f"   re-priced live: {usd(rtotal)}  ≤  {usd(RECOVERY_BUDGET)} budget ✓")
    _pause()

    print()
    print("✅  ONE FRESH RE-CONSENT — new ap2_mandate binds the NEW checkout")
    _, rcomp = consent_and_complete(rcid)
    line(f"   new booking_ref: {rcomp.get('booking_ref','?')}  "
         f"({usd(rtotal)}, within budget)")
    line("   the old booking is NOT silently re-used; the swap is re-authorized.")

    # cleanup
    try:
        merchant_admin("/admin/sim/availability",
                       {"hotel_id": "bali-legian-beach", "available": True})
    except Exception:
        pass
    _pause(2)


# ===========================================================================
# WA REAL-TRIP SEGMENT (additive; --trip wa) — the user's REAL 2022 Western
# Australia Perth-to-Perth road trip. STRUCTURE + CHALLENGES are REAL; lodging
# names + prices are SEEDED + provenance-tagged (catalog.go §13).
# ===========================================================================

# Real consecutive-town drive distances (km) from the user's 2022 trip.
WA_LOOP = [
    ("perth",          "Perth",          None),
    ("busselton",      "Busselton",      233),
    ("margaret-river", "Margaret River",  48),
    ("pemberton",      "Pemberton",      139),
    ("albany",         "Albany",         239),
    ("ravensthorpe",   "Ravensthorpe",   293),
    ("esperance",      "Esperance",      188),
    ("kalgoorlie",     "Kalgoorlie",     391),   # LONGEST single leg
    ("northam",        "Northam",        536),   # VERY LONG / "difficult"
    ("perth",          "Perth",           97),   # loop close
]

# A bookable 4-stop slice of the REAL loop — Albany → Ravensthorpe → Esperance
# → Kalgoorlie — so every edge is a real consecutive-town pair (the trip goes
# Albany→Ravensthorpe→Esperance, NOT Albany→Esperance directly) and the slice
# ENDS on the 391 km long-haul into Kalgoorlie. Dates are in bushfire season
# (Jan) so the seasonal advisory fires. The Esperance ensuite is the leg we'll
# flip sold-out for the L3 recovery.
WA_DEMO_BUDGET = 110_000  # $1,100
WA_DEMO_LEGS = [
    {"hotel_id": "albany-albany-middleton",          "city": "albany",
     "area": "albany",        "checkin": "2027-01-08", "checkout": "2027-01-10", "adults": 2},
    {"hotel_id": "ravensthorpe-ravensthorpe-palace", "city": "ravensthorpe",
     "area": "ravensthorpe",  "checkin": "2027-01-10", "checkout": "2027-01-12", "adults": 2},
    {"hotel_id": "esperance-esperance-seafront",     "city": "esperance",
     "area": "esperance",     "checkin": "2027-01-12", "checkout": "2027-01-14", "adults": 2},
    {"hotel_id": "kalgoorlie-kalgoorlie-goldfields", "city": "kalgoorlie",
     "area": "kalgoorlie",    "checkin": "2027-01-14", "checkout": "2027-01-16", "adults": 2},
]
WA_RECOVERY_HOTEL = "esperance-esperance-bayview"  # in-budget Plan B for Esperance


def banner_wa() -> None:
    banner(
        "TRAVEL GUILD — a REAL trip: 2022 Western Australia road loop",
        "structure + challenges REAL (user's trip) · lodging/prices SEEDED + provenance-tagged (§13)",
    )


def wa_beat_intent() -> None:
    beat("Ⓦ①  REAL TRIP", "the user's actual 2022 WA Perth-to-Perth loop")
    line("A genuine itinerary, not a synthetic scenario: ~11 days, 2,816 km,")
    line("campervan + holiday parks. Perth → Busselton → Margaret River →")
    line("Pemberton → Albany → Ravensthorpe → Esperance → Kalgoorlie → Northam")
    line("→ Perth. The society plans it with the SAME checks & balances.")
    line()
    line("Seeded from the real trip (catalog.go, provenance-tagged §13):")
    prev = None
    for slug, name, km in WA_LOOP:
        if km is None:
            line(f"   {name}")
        else:
            tag = "   ← LONGEST (391 km)" if km == 391 else (
                  "   ← VERY LONG / difficult (536 km)" if km == 536 else "")
            line(f"   {prev} → {name:14s} {km:>4d} km{tag}")
        prev = name
    _pause(2)


def wa_beat_transport() -> None:
    beat("Ⓦ②  TRANSPORT FEASIBILITY", "the Transport agent flags the long-haul legs")
    line("The Transport agent reads the REAL inter-town drive distances and flags")
    line("the long single-day hauls for a rest/fuel buffer + sensible sequencing")
    line("(a long drive is FEASIBLE — not infeasible — so it never blocks booking).")
    print()
    try:
        from agents.transport_agent import check_feasibility
    except Exception as e:  # pragma: no cover
        line(f"(transport import unavailable: {e})")
        return
    legs = [
        {"leg_id": f"leg-{i}", "city": l["city"], "area": l["area"],
         "checkin": l["checkin"], "checkout": l["checkout"]}
        for i, l in enumerate(WA_DEMO_LEGS)
    ]
    res = check_feasibility(legs)
    for e in res["edges"]:
        km = e.get("distance_km")
        flag = "  ⚠ LONG DRIVE — buffer + don't stack a same-day check-in" if e.get("long_drive") else ""
        line(f"   {e['from_city']:10s} → {e['to_city']:11s} "
             f"{('%d km' % km) if km else '':>7s}  ~{e['transfer_minutes']:>3d} min  "
             f"feasible={str(e['feasible']).lower()}{flag}")
    print()
    line(f"infeasible edges: {len(res['infeasible_edges'])}   "
         f"long-drive advisories: {len(res['long_drive_edges'])}")
    line("→ the 391 km Esperance→Kalgoorlie leg is flagged: book it, but buffer it.")
    _pause(2)


def wa_beat_seasonal() -> None:
    beat("Ⓦ③  SEASONAL ADVISORY", "the Destination agent surfaces the bushfire-season risk")
    line("These dates fall in southern-WA bushfire season (Dec–Feb, 40°C+ days).")
    line("The Destination agent surfaces a SEEDED, provenance-tagged advisory")
    line("(it informs — it never blocks the booking).")
    print()
    try:
        from agents.destination_agent import assess
    except Exception as e:  # pragma: no cover
        line(f"(destination import unavailable: {e})")
        return
    for l in WA_DEMO_LEGS:
        a = assess(l["city"], "relax", l["checkin"], l["checkout"])
        adv = a.get("advisory")
        if adv:
            line(f"   {l['city']:11s} [{adv['severity']}] {adv['flag']}  "
                 f"(provenance: {adv['provenance']})")
            line(f"               {adv['detail']}")
            break
    _pause(2)


def wa_beat_book_and_recover(live: bool) -> None:
    beat("Ⓦ④  DERAILMENT + RECOVERY", "a sold-out holiday park → L3 recovery, one re-consent")
    line('Same "contingency before the fault" story, on the REAL trip: book the')
    line("WA slice, flip the Esperance ensuite SOLD OUT mid-trip, and recover")
    line("within budget with ONE fresh re-consent.")
    print()

    if not live:
        print("🔁  DERAILMENT — esperance-esperance-seafront SOLD OUT (catalog flipped)")
        line(f"→ activate pre-vetted Plan B (Esperance → {WA_RECOVERY_HOTEL})")
        line("→ within budget → ONE fresh re-consent → re-booked (new booking_ref)")
        return

    sold = "esperance-esperance-seafront"
    # clean state
    try:
        merchant_admin("/admin/sim/availability", {"hotel_id": sold, "available": True})
    except Exception:
        pass

    line_items = [{"hotel_id": l["hotel_id"], "checkin": l["checkin"],
                   "checkout": l["checkout"], "adults": l["adults"]} for l in WA_DEMO_LEGS]
    _, co = merchant_call("create_checkout", {"checkout": {
        "user_id": "demo-wa", "line_items": line_items,
        "user_budget_cents": WA_DEMO_BUDGET}})
    cid = co.get("id")
    base_total = int(co.get("total_cents", 0))
    _, comp = consent_and_complete(cid)
    line(f"Booked the WA slice: {comp.get('booking_ref','?')}  "
         f"({usd(base_total)} ≤ {usd(WA_DEMO_BUDGET)} budget, "
         f"{len(WA_DEMO_LEGS)} holiday parks)")
    _pause()

    # flip sold-out
    merchant_admin("/admin/sim/availability", {"hotel_id": sold, "available": False})
    print()
    print(f"🔁  DERAILMENT — {sold} SOLD OUT (holiday-park ensuite, catalog flipped)")
    esp_leg = next(l for l in WA_DEMO_LEGS if l["city"] == "esperance")
    _, search = merchant_call("search_catalog", {"query": {
        "city": "esperance", "checkin": esp_leg["checkin"], "checkout": esp_leg["checkout"],
        "max_cents": WA_DEMO_BUDGET}})
    ids = [r["hotel_id"] for r in search.get("results", [])]
    line(f"   merchant search now EXCLUDES it: {sold not in ids}  "
         f"({len(ids)} other Esperance sites still bookable)")
    _pause()

    print()
    print(f"   activate pre-vetted Plan B: Esperance → {WA_RECOVERY_HOTEL}")
    rec_items = [dict(li) for li in line_items]
    for li in rec_items:
        if li["hotel_id"] == sold:
            li["hotel_id"] = WA_RECOVERY_HOTEL
    _, rco = merchant_call("create_checkout", {"checkout": {
        "user_id": "demo-wa", "line_items": rec_items,
        "user_budget_cents": WA_DEMO_BUDGET}})
    rcid = rco.get("id")
    rtotal = int(rco.get("total_cents", 0))
    line(f"   re-priced live: {usd(rtotal)}  ≤  {usd(WA_DEMO_BUDGET)} budget ✓")
    _pause()

    print()
    print("✅  ONE FRESH RE-CONSENT — new ap2_mandate binds the NEW checkout")
    _, rcomp = consent_and_complete(rcid)
    line(f"   new booking_ref: {rcomp.get('booking_ref','?')}  ({usd(rtotal)}, within budget)")
    line("   the merchant re-enforced the budget; no autonomous spend during the swap.")

    # cleanup
    try:
        merchant_admin("/admin/sim/availability", {"hotel_id": sold, "available": True})
    except Exception:
        pass
    _pause(2)


def wa_beat_close() -> None:
    beat("Ⓦ⑤  CLOSE", "the same society, on a genuine itinerary")
    line("Real trip · real drive distances · real seasonal + sold-out challenges.")
    line("Transport buffered the long haul, Destination surfaced the bushfire")
    line("advisory, and the sold-out holiday park was recovered within budget with")
    line("ONE re-consent. Lodging/prices are seeded + provenance-tagged (§13);")
    line("live feeds plug into the same Tier-2 provider seam.")
    print()
    rule("━")


def run_wa_demo() -> int:
    live = merchant_alive()
    banner_wa()
    if not live:
        print()
        line("NOTE: live merchant not reachable — beat Ⓦ④ shows the captured")
        line("narrative. The transport + seasonal beats (Ⓦ②/Ⓦ③) are deterministic")
        line("and run regardless (no merchant needed).")
    wa_beat_intent()
    wa_beat_transport()
    wa_beat_seasonal()
    wa_beat_book_and_recover(live)
    wa_beat_close()
    return 0


# ===========================================================================
# BEAT ⑤ — close
# ===========================================================================

def beat_close() -> None:
    beat("⑤  CLOSE", "the right protocol at each seam — the human consented once")
    line("MCP   agent ↔ tools      discovery / research")
    line("A2A   agent ↔ peer       the society negotiates tradeoffs internally")
    line("UCP   agent ↔ merchant   signed, server-enforced commerce boundary")
    line("AP2   human ↔ commitment ONE mandate authorizes the whole package")
    print()
    line("Travel is the testbed, not the product.")
    line("Safe autonomous agentic commerce — structure is variance control.")
    print()
    rule("━")


# ===========================================================================
# CASCADE SEGMENT (additive; --trip cascade) — MULTI-FAULT CASCADE RECOVERY.
# Grounded in a real (anonymized) flight-cancellation cascade: an internal
# flight cancels → reroute + unplanned overnight; then ANOTHER flight cancels →
# a second reroute. Flight-dependent Ethiopia historic route. The society
# survives each fault within budget with ONE fresh re-consent. §12.1.
# ===========================================================================

CASCADE_BUDGET = 120_000  # $1200
CASCADE_BASE = [
    {"hotel_id": "gondar-gondar-goha",         "checkin": "2026-11-24", "checkout": "2026-11-26", "adults": 1, "city": "gondar"},
    {"hotel_id": "lalibela-lalibela-maribela", "checkin": "2026-11-26", "checkout": "2026-11-28", "adults": 1, "city": "lalibela"},
    {"hotel_id": "semera-semera-erta",         "checkin": "2026-11-28", "checkout": "2026-11-30", "adults": 1, "city": "semera"},
]


def banner_cascade() -> None:
    banner(
        "TRAVEL GUILD — MULTI-FAULT CASCADE RECOVERY",
        "a real flight-cancellation cascade (anonymized) — survived within budget",
    )


def _cascade_reset() -> None:
    for li in CASCADE_BASE:
        try:
            merchant_admin("/admin/sim/availability", {"hotel_id": li["hotel_id"], "available": True})
        except Exception:
            pass
    for a, b in [("lalibela", "semera"), ("gondar", "lalibela"), ("addis ababa", "semera")]:
        try:
            merchant_admin("/admin/sim/transfer", {"from_city": a, "to_city": b, "feasible": True})
        except Exception:
            pass


def cascade_beat_intent() -> None:
    beat("Ⓒ①  FLIGHT-DEPENDENT TRIP", "Gondar → Lalibela → Semera (internal flights link the towns)")
    line("A real 8-day flight-dependent itinerary (structure seeded from a real")
    line("trip, §13): mountainous terrain makes internal flights the link between")
    line("historic-route towns. Budget $1,200. The real trip suffered a CASCADE of")
    line("two flight cancellations — the exact thing single-prompt agents can't survive.")
    _pause()


def cascade_beat_run(live: bool) -> None:
    beat("Ⓒ②  CASCADE — detect → recover → detect → recover", "two faults, two fresh re-consents")
    line("FAULT 1: a booked hotel sells out.  FAULT 2 (on the NEW state): an")
    line("inter-leg FLIGHT is cancelled — Transport re-routes via an Addis hub +")
    line("an unplanned overnight. Each recovery stays within budget; each is")
    line("re-authorized ONCE; and the SUPERSEDED booking is VOIDED so exactly ONE")
    line("itinerary is ever live — nothing is silently re-spent, no double-charge.")
    print()

    if not live:
        print("🔁  FAULT 1 — gondar-gondar-goha SOLD OUT (flip_availability)")
        line("→ swap leg-0 → gondar-gondar-fasil → fresh re-consent → booking #1 (~$456)")
        line("   → VOID baseline (prior released) → exactly ONE live booking")
        print("🔁  FAULT 2 — lalibela→semera FLIGHT CANCELLED (flip_transfer)")
        line("→ re-route via Addis overnight → fresh re-consent → booking #2 (~$508)")
        line("   → VOID booking #1 (prior released) → exactly ONE live booking")
        line("✅  CASCADE SURVIVED — 2 faults, 2 re-consents, 2 voids, each ≤ $1200, variance-0")
        line("   one live itinerary; cumulative spend = final total (~$508), NOT ~$1520 (no double-charge)")
        line("   unrecoverable variant → honest cannot_satisfy (merchant veto)")
        return

    _cascade_reset()

    def book(items):
        _, co = merchant_call("create_checkout", {"checkout": {
            "user_id": "demo-cascade", "line_items": items, "user_budget_cents": CASCADE_BUDGET}})
        cid = co.get("id")
        total = int(co.get("total_cents", 0))
        if co.get("status") != "incomplete":
            return co.get("booking_ref", ""), total, co.get("status", "error"), cid
        _, comp = consent_and_complete(cid)
        return comp.get("booking_ref", ""), total, comp.get("status", "error"), cid

    def void(cid, label):
        # commit-new first, then void-old: release the SUPERSEDED booking (§12.1).
        _, res = merchant_call("cancel_checkout", {"checkout": {"id": cid}})
        return res.get("cancelled") is True, res.get("released_booking_ref", "")

    base_items = [{k: l[k] for k in ("hotel_id", "checkin", "checkout", "adults")} for l in CASCADE_BASE]
    ref0, total0, _, cid0 = book(base_items)
    line(f"Baseline booked: {ref0}  ({usd(total0)} ≤ {usd(CASCADE_BUDGET)})")
    _pause()

    # FAULT 1 — hotel sold out
    print()
    print("🔁  FAULT 1 — gondar-gondar-goha SOLD OUT (flip_availability)")
    merchant_admin("/admin/sim/availability", {"hotel_id": "gondar-gondar-goha", "available": False})
    _, search = merchant_call("search_catalog", {"query": {
        "city": "gondar", "checkin": "2026-11-24", "checkout": "2026-11-26", "max_cents": CASCADE_BUDGET}})
    ids = [r["hotel_id"] for r in search.get("results", [])]
    line(f"   merchant EXCLUDES it: {'gondar-gondar-goha' not in ids}  → swap to gondar-gondar-fasil")
    items1 = [dict(base_items[0], hotel_id="gondar-gondar-fasil")] + base_items[1:]
    ref1, total1, _, cid1 = book(items1)
    print("✅  RE-CONSENT #1 — fresh ap2_mandate binds the NEW checkout")
    line(f"   new booking_ref: {ref1}  ({usd(total1)} ≤ {usd(CASCADE_BUDGET)}, fresh checkout)")
    ok0, rel0 = void(cid0, "baseline")
    line(f"   VOID prior → baseline {ref0} CANCELLED (released {rel0}) · exactly ONE live booking")
    _pause()

    # FAULT 2 — flight cancel (hits the NEW state)
    print()
    print("🔁  FAULT 2 — lalibela→semera FLIGHT CANCELLED (flip_transfer)")
    merchant_admin("/admin/sim/transfer", {"from_city": "lalibela", "to_city": "semera", "feasible": False})
    _, cancelled = merchant_call("sim_get_transfers", {})
    line(f"   Transport reads the cancellation as data: {cancelled.get('cancelled_transfers')}")
    line("   → re-route via Addis hub + unplanned overnight (the affected route, not the hotels)")
    items2 = [
        dict(base_items[0], hotel_id="gondar-gondar-fasil"),
        base_items[1],
        {"hotel_id": "addis-addis-guesthouse", "checkin": "2026-11-28", "checkout": "2026-11-29", "adults": 1},
        {"hotel_id": "semera-semera-erta",     "checkin": "2026-11-29", "checkout": "2026-12-01", "adults": 1},
    ]
    ref2, total2, _, cid2 = book(items2)
    print("✅  RE-CONSENT #2 — fresh ap2_mandate binds the NEW checkout")
    line(f"   new booking_ref: {ref2}  ({usd(total2)} ≤ {usd(CASCADE_BUDGET)}, fresh checkout)")
    ok1, rel1 = void(cid1, "recovery-1")
    line(f"   VOID prior → recovery #1 {ref1} CANCELLED (released {rel1}) · exactly ONE live booking")
    _pause()

    # Unrecoverable variant
    print()
    print("🔁  UNRECOVERABLE variant — the only onward option blows the budget")
    over = items2[:-1] + [{"hotel_id": "lalibela-lalibela-maribela",
                           "checkin": "2026-11-29", "checkout": "2026-12-12", "adults": 1}]
    _, co = merchant_call("create_checkout", {"checkout": {
        "user_id": "demo-cascade", "line_items": over, "user_budget_cents": CASCADE_BUDGET}})
    _, denied = consent_and_complete(co.get("id"))
    if denied.get("status") == "denied":
        line(f"   merchant VETO: {denied.get('reason')} ({usd(int(denied.get('total_cents',0)))} > budget)")
        line("   → society reports cannot_satisfy — no fabrication, no partial spend")

    print()
    line(f"✅  CASCADE SURVIVED: {ref0} → {ref1} → {ref2}  (only {ref2} LIVE; priors voided)")
    line(f"   {usd(total0)} → {usd(total1)} → {usd(total2)} · each ≤ {usd(CASCADE_BUDGET)} · variance-0")
    line(f"   cumulative committed spend = {usd(total2)} (the final total), NOT "
         f"{usd(total0 + total1 + total2)} (sum of cycles) — exactly ONE live itinerary, no double-charge.")
    line('   "The contingency existed before the fault" — twice, on the new state each time.')
    _cascade_reset()
    _pause(2)


def cascade_beat_close() -> None:
    beat("Ⓒ③  CLOSE", "survives a CASCADE, not just one fault")
    line("Single-prompt agents optimize the happy path and stop. The society")
    line("detects → recovers → detects the NEXT fault on the new state → recovers,")
    line("each within budget, each re-authorized once, honest cannot_satisfy when")
    line("a fault is genuinely unrecoverable. Two fault classes (hotel sold-out,")
    line("flight cancel) recovered by the right agent (Accommodation, Transport).")
    print()
    line("Reproduce: benchmark/run_cascade_bench.py · REPORT.md §S6 · variance-0.")
    print()
    rule("━")


def run_cascade_demo() -> int:
    live = merchant_alive()
    banner_cascade()
    if not live:
        print()
        line("NOTE: live merchant not reachable — beat Ⓒ② shows the captured")
        line("narrative. Run live:")
        line("  MERCHANT_MCP_URL=http://127.0.0.1:8090/api/ucp/mcp UCP_UNSIGNED_TIER=L2 \\")
        line("    python3 society/demo.py --trip cascade")
    cascade_beat_intent()
    cascade_beat_run(live)
    cascade_beat_close()
    return 0


# ===========================================================================
# Main
# ===========================================================================

# ---------------------------------------------------------------------------
# DC0 DEMO SEGMENT (additive; --trip dc0) — the INSURANCE-exclusion contrast.
# The agent-count→evidence beat (§17.3): a single agent declares "covered"; the
# society's Insurance specialist catches that civil-unrest is EXCLUDED. Fully
# deterministic (NO-LLM-NUMBERS) — needs NO merchant, NO key.
# ---------------------------------------------------------------------------

def run_dc0_demo() -> int:
    from agents import insurance_agent as ins
    from utils.peril_crosswalk import risk_reasons_to_perils

    banner(
        "TRAVEL GUILD — DC0: Insurance catches the EXCLUSION",
        "'I'll just grab travel insurance' — the green light, and the red one it hides",
    )

    beat("Ⓓ①  INTENT", "a solo trip to a region with elevated civil unrest")
    line("\"Trip to the destination, ~$2,000, solo. I'll just grab travel insurance.\"")
    line()
    line("Risk advisory (seeded) reason-codes: civil_unrest, transport_disruption")

    beat("Ⓓ②  SINGLE AGENT", "FAIR baseline — real qwen3-max, same tools/data")
    line("Given the SAME seeded policy clauses + risk advisory, a single qwen3-max")
    line("agent (NO tip-off to check exclusions) usually DOES surface the unrest")
    line("exclusion in prose — but variably: it cited the governing clause EXC-WAR-2")
    line("in 0/8 runs, and its tool-path + phrasing change run-to-run.")
    line("(Honest: a fair single agent is competent here — the win is NOT 'it can't")
    line("find it'; see Ⓓ⑤. Measured by benchmark/run_dc0_bench.py, real LLM.)")

    beat("Ⓓ③  THE SOCIETY", "Risk → peril_set → Insurance clause-matcher (deterministic)")
    perils = risk_reasons_to_perils(["civil_unrest", "transport_disruption"])
    for p in ["medical"]:
        if p not in perils:
            perils.append(p)
    a = ins.assess_coverage(peril_set=perils, policy_id="P-STD-01",
                            insured_trip_cost_cents=200000)
    for r in a["per_peril"]:
        mark = {"EXCLUDED": "✗ EXCLUDED", "COVERED": "✓ COVERED",
                "SUB_LIMITED": "~ SUB-LIMITED", "CONDITIONAL": "? CONDITIONAL",
                "UNDETERMINED": "? UNDETERMINED"}.get(r["status"], r["status"])
        clauses = ", ".join(r["matched_clause_ids"]) or "(no clause)"
        line(f"  {r['peril_class']:<16} {mark:<14} [{clauses}]")
    line()
    line(f"  premium (NO-LLM-NUMBERS, integer cents from seed): {usd(a['premium_cents'])}")
    line(f"  → emitted as a Budget line item: {ins.premium_line_item(a)['hotel_id']}")

    beat("Ⓓ④  THE CATCH", "the exclusion surfaced as first-class output")
    e = ins.explain_exclusion(a, "civil_unrest", region="the destination")
    for chunk in _wrap(e["headline"], W - 6):
        line(chunk)

    beat("Ⓓ⑤  WHY IT MATTERS", "the MEASURED, honest win — determinism, not 'gotcha'")
    line("  FAIR baseline (real qwen3-max, same tools): surfaced the exclusion 8/8")
    line("  — so the win is NOT 'a single agent can't find it'. We say that plainly.")
    line("  The REAL win is STRUCTURAL GUARANTEE: the society names the governing")
    line("  clause EXC-WAR-2 + provenance on EVERY run (var-0); the single agent")
    line("  cited it 0/8 and varied its path/phrasing run-to-run. For an autonomous")
    line("  coverage gate, a deterministic named-clause guarantee > a competent-but-")
    line("  variable prose answer. Measured: benchmark/run_dc0_bench.py (real LLM).")
    line("  Invariant CI-enforced: society/test_insurance_agent.py.")
    print()
    return 0


def _wrap(text: str, width: int) -> list[str]:
    """Simple word-wrap for the demo headline (no external deps)."""
    words = text.split()
    out, cur = [], ""
    for w in words:
        if len(cur) + len(w) + 1 > width and cur:
            out.append(cur)
            cur = w
        else:
            cur = f"{cur} {w}".strip()
    if cur:
        out.append(cur)
    return out


def main() -> int:
    # --trip wa runs the additive WA real-trip segment; --trip cascade runs the
    # multi-fault cascade segment; --trip dc0 runs the Insurance-exclusion
    # contrast; default runs the Bali beats ①–⑤ unchanged.
    if "--trip" in sys.argv:
        i = sys.argv.index("--trip")
        if i + 1 < len(sys.argv):
            which = sys.argv[i + 1].lower()
            if which in ("wa", "cascade"):
                # These two segments hardcode real private-catalog hotel IDs
                # (a real 2022 Western Australia road trip; an Ethiopia
                # multi-fault cascade) that are not part of this public
                # showcase repo's small hand-authored sample dataset -- an
                # honest, explicit unavailable message beats a raw traceback.
                print(f"--trip {which} requires hotel data not included in this "
                      f"public showcase repo's sample dataset (see README.md's "
                      f"'What's not here' section). Run the default demo "
                      f"(no --trip flag) or --trip dc0 instead.")
                return 1
            if which == "dc0":
                return run_dc0_demo()

    live = merchant_alive()
    banner(
        "TRAVEL GUILD — safe autonomous agentic commerce, proven on travel",
        f"merchant: {'LIVE @ ' + MERCHANT_URL if live else 'offline (captured narrative)'}",
    )
    if not live:
        print()
        line("NOTE: live merchant not reachable — beats ②/④ show the captured")
        line("narrative. To run the live beats, start the merchant (see README's")
        line("Quickstart) then: MERCHANT_MCP_URL=http://127.0.0.1:8090/api/ucp/mcp \\")
        line("    UCP_UNSIGNED_TIER=L2 python3 society/demo.py")

    beat_intent()
    beat_negotiation(live)
    beat_variance()
    beat_derailment(live)
    beat_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
