# Agent instructions

Guidance for AI coding agents (and humans) working in this repo.

## Build and test
- Install: `make setup`
- Test everything: `make test` (backend pytest + frontend type-check/build)
- Lint: `make lint` (ruff for Python, oxlint for TypeScript)
- Run locally: `make dev-api` and `make dev-web`

## Layout
- `backend/filament/models.py` — data model (graph, memory, runs, approvals, signals, readiness)
- `backend/filament/api/` — FastAPI routers; every route is also exposed in `cli.py`
- `backend/filament/agents/` — one module per agent, registered in `agents/__init__.py`
- `backend/filament/context.py` — context engine (graph walk + memory ranking, token budget)
- `backend/filament/llm.py` — the only place that calls the Anthropic API
- `backend/filament/readiness/` — Launchpad readiness checks (deterministic, no model)
- `frontend/src/` — `api.ts` client, `pages.tsx` views, `App.tsx` shell

## Conventions
- Call the Anthropic Messages API directly through `llm.complete`; no agent frameworks.
- Every agent must work offline: pass an `offline_response` so tests never hit the network.
- Agents write artifacts and memory through `Agent.save_artifact` / `Agent.remember` so
  autonomy levels and provenance are applied consistently.
- New API capability → add the CLI command and the `api.ts` client method in the same change.
- Every change ships with tests in `backend/tests/`.

## Do not touch
- Do not commit `.env`, databases (`*.db`) or API keys.
- Do not bypass approval gates or mark agent-written memory as confirmed in code.
