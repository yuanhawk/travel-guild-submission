# scripts/

Frontend build/dev helper scripts for the Travel Guild web app. (See the repo root `README.md` for the app overview; this is a per-file index.)

| File | Purpose |
| --- | --- |
| `contract-check.mjs` | Drift gate (Node, ESM). Curls the live backend (this repo's root) and asserts it still serves the exact fields `src/lib/api.ts` / `stream.ts` / `planStream.ts` depend on, since the two halves of this repo are coupled only by the HTTP/SSE contract. Validators are shared so the same assertions run against live responses AND golden offline fixtures. Run via `npm run contract-check` (live + fixtures) or `node scripts/contract-check.mjs --offline` (fixtures/types only, CI-safe). Exit 0 = in sync, exit 1 = drift (prints the missing/renamed field). |
