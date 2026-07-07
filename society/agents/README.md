# society/agents — the multi-agent specialist society

Each file is one specialist A2A (Agent2Agent) agent. Every agent publishes an
Agent Card with a single skill and owns exactly one decision. They run as
JSON-RPC 2.0 / A2A servers and are coordinated by the orchestrator (see
`society/orchestration`). Most agents are fully deterministic (var-0,
NO-LLM-NUMBERS); only Destination uses a variance-clamped LLM edge.

## Base / smoke-test
| file | agent | responsibility |
|------|-------|----------------|
| `a2a_agent.py` | A2A base | Reusable A2A v0.3/v1.0 server: Agent Card endpoint + JSON-RPC dispatcher + Task lifecycle (`SkillRejected` → rejected). |
| `echo_agent.py` | Echo | Minimal A2A agent that echoes input text back as an artifact; M0 smoke test. |

## Planning agents (build the plan)
| file | agent | responsibility |
|------|-------|----------------|
| `planner_agent.py` | Planner (`plan.decompose`) | Decomposes a trip into legs; runs multiple-choice-knapsack DP for the budget-optimal hotel combination (falls back to proportional split). |
| `destination_agent.py` | Destination / Local-expert (`destination.assess`) | Variance-clamped LLM ranking of REAL catalog areas for a city's vibe; LLM only reorders within the closed vibe set, never invents. |
| `accommodation_agent.py` | Accommodation (`accommodation.propose`) | Proposes lodging from the real merchant catalog, area-filtered and ranked. Prices are tagged demo estimates, not live rates. |
| `day_planner_agent.py` | Day planner (`activity.plan`) | Deterministic per-leg day/meal plan from the committed OSM POI catalog; never fabricates POIs/hours/prices, degrades to an honest empty plan with provenance. |
| `transport_agent.py` | Transport / Logistics (`transport.feasibility`) | Deterministic inter-leg feasibility from seeded advisory transfer times (air/rail/ferry/road); no live timetables. |

## Gate / safety agents (constrain or veto the plan)
| file | agent | responsibility |
|------|-------|----------------|
| `budget_agent.py` | Budget / Finance (`budget.check` / `budget.commit` / `budget.enforce`) | Two-phase money gate over the UCP merchant; check creates a checkout, commit completes it. Merchant settlement is SIMULATED, AP2 mandate is a real protocol with simulated settlement (no live payment rail). |
| `critic_agent.py` | Critic / Verifier (`itinerary.verify`) | Hard gate before the single human consent; re-derives every fact from merchant backend data and catches missing-leg / budget regressions. |
| `insurance_agent.py` | Insurance (`coverage`) | Exclusion-first coverage check against seeded policy terms; proposes the premium line item, never asserts "covered" without enumerating exclusions. |
| `compliance_agent.py` | Compliance (`eligibility`) | Visa/eVisa entry & lead-time bookability gate by nationality; blocks trips that cannot get a visa in time and proposes a compliant re-sequence + fee line item. |
| `health_agent.py` | Health (`health`) | Vaccination/entry-health gate from CDC-grounded slates; emits the upfront vaccine cost as a budget line item and a mandatory-cert entry gate. |
| `risk_agent.py` | Risk (`risk`) | Off-money-path L1 signal consolidator: seeded cyclone likelihood, median delay, and seismic-resilience planning signals (avoid/buffer/flag windows). |
| `fraud_agent.py` | Fraud / Counterparty-trust (`fraud`) | Deterministic supplier-solvency gate from a seeded solvency table; constrains which counterparties Budget may commit against (pre-empts supplier insolvency). |

All seeded tables, prices, and merchant settlement here are demo/simulated data
for the hackathon submission, not live bookings or real financial transactions.
