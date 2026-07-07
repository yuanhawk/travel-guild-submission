# society/orchestration

The HTTP/SSE entrypoint and the negotiation conductor that drives the multi-agent
travel-planning pipeline. The server streams agent trace events to the dashboard
over Server-Sent Events; the orchestrator runs the deterministic negotiation loop.

| File | Purpose |
| --- | --- |
| `server.py` | Starlette + uvicorn SSE server for the real-time JS kanban dashboard. Serialises `negotiate()` calls on a single shared orchestrator (threading.Lock; the orchestrator is not concurrency-safe) and forwards trace events onto an asyncio queue via `call_soon_threadsafe`. Merges demo-user profile into the trip request at the I/O boundary, before the deterministic core. |
| `orchestrator.py` | `TravelOrchestrator` — the negotiation conductor. Per-leg destination assessment → accommodation candidate gathering → DP budget allocator picks the globally budget-optimal hotel combination → Budget enforce / Critic gate / Transport gate → consent → book, with a bounded re-plan loop (MAX_ROUNDS=3) and honest `cannot_satisfy` on exhaustion. Lodging prices are demo estimates; merchant settlement is SIMULATED. |
| `store.py` | `DashboardStore` — Phase-1 local SQLite persistence (swap-seam for PolarDB/Tair) for held plans, booked trips, demo users and saved items. Strictly OFF the deterministic var-0 path: written only after a result is final, read only by the HTTP layer. All money is integer cents. |
| `demo_users.py` | Five pre-seeded demo users + persona presets for the dashboard. Judges log in AS a fixed user (no signup — NOT a full IAM); each carries persona, nationality, display currency and a SIMULATED $5,000 wallet. Persona is a preset bundle that pre-fills empty selections, not a scoring weight. |
| `a2a_client.py` | Minimal A2A (Agent-to-Agent, v0.3 wire) client helper: `send`/`send_raw` POST `message/send` and poll `tasks/get` to a terminal Task state; `get_card` fetches the Agent Card JSON. |

_Note: AP2 mandates and any wallet/merchant settlement surfaced through these
components are a mandate protocol with SIMULATED settlement — not a real payment rail._
