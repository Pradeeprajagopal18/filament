# Agent instructions

Guidance for AI coding agents (and humans) working in this repo.

## Build and test
- Install: `make setup`
- Test everything: `make test` (backend pytest + frontend type-check/build)
- Lint: `make lint` (ruff for Python, oxlint for TypeScript)
- Run locally: `make dev-api` and `make dev-web`

## Layout
- `backend/igniteai/models.py` — data model (graph, memory, runs, approvals, signals, readiness)
- `backend/igniteai/api/` — FastAPI routers; every route is also exposed in `cli.py`
- `backend/igniteai/agents/base.py` — the tool-use loop and guardrails (turns, tokens, autonomy)
- `backend/igniteai/agents/tools.py` — tools agents can call; the only way agents write
- `backend/igniteai/agents/` — one module per agent, registered in `agents/__init__.py`
- `backend/igniteai/context.py` — context engine (graph walk + memory ranking, token budget)
- `backend/igniteai/mcp_gateway.py` — MCP discovery/calls, secret refs, write rules, result caps
- `backend/igniteai/runtime.py` — config resolution (run > agent setting > default), queue, worker
- `backend/igniteai/api/config.py` — MCP servers, runners, agent settings, YAML config
- `backend/igniteai/llm.py` — the only place that calls the Anthropic API
- `backend/igniteai/readiness/` — readiness scan entry point (checkout, scoring, CLI fixes)
- `backend/igniteai/repos/` — repo intelligence, deterministic, no model: `profile.py` (repo kinds), `checks.py` (kind-aware checks), `api_extract.py`, `ui_extract.py`, `deps.py` (component view), `generate.py` (references kept in Ignite), `analyze.py`, `api_enrich.py` (schemas, auth, errors, examples), `consumers.py` (outbound HTTP calls), `system.py` (system map, impact), `diff.py` (scan-to-scan changes), `service.py` (scans, references, scheduler), `context.py` (repo summaries for agents), `codeparse.py` (tree-sitter facts per file), `codeindex.py` (cross-file resolution, communities, per-file cache), `codegraph.py` (summary, service attribution), `codequery.py` (explain, path, impact, conflicts), `code_tools.py` (agent/Omni code tools), `datamodel.py` (ER model), `api_annotations.py` (springdoc/swaggo/@nestjs/swagger/FastAPI annotations, spec generators, live spec URL); `api/code.py` serves the code tools
- `backend/igniteai/mcp_registry.py` — MCP gateway: registry sync (native + generated from repo APIs), review/rollback, invoke (policy, rate limits, projection, redaction, call log), discovery and metrics; `api/gateway.py` serves it plus the Omni MCP endpoint; `agents/gateway_tools.py` is progressive discovery for agents
- `backend/igniteai/delivery/` — delivery pipeline: `service.py` (stages, roles, board, PR linking, SLO checks, release actions), `checks.py` (deterministic triage/handoff/QA/review/release/SLO checks), `github.py`, `webhook.py` (PR + CI events), `omni.py` (task tools for external coding agents); agents in `agents/delivery.py` with tools in `agents/delivery_tools.py`; API in `api/delivery.py`
- `backend/scripts/demo_repos.py` — regenerates `frontend/src/demo-repos.json` for the preview after analyzer changes
- `backend/scripts/demo_delivery.py` — regenerates `frontend/src/demo-delivery.json` (the delivery scenario the preview replays) after delivery changes
- `backend/igniteai/auth.py` (OIDC sign-in, sessions, personal tokens), `access.py` (roles, repo visibility, list filters, checkpoint sign-off, audit), `api/guard.py` (the rule every route goes through), `api/access.py` (auth and People & access endpoints)
- `frontend/src/` — `api.ts` client, `pages.tsx` views, `App.tsx` shell, `access.tsx` (sign-in, People & access, project access, tokens)

## Conventions
- Call the Anthropic Messages API directly through `llm.complete`; no agent frameworks.
- Agents are agentic: they get a goal and tools, and the model decides the steps. Do not
  hard-code a fixed call sequence in an agent; express it in the system prompt instead.
- Guardrails (approval gates, autonomy, budgets, delegation depth) live in the platform, never
  in prompts alone.
- Never store secrets: MCP env/headers hold `${env:NAME}` references, resolved on the runner.
- Keep context lean: tool results must stay small and valid JSON; prefer reading by id.
- Agents cite only ids they have seen (`seen_ids`); do not weaken `check_grounding`.
- Every agent must work offline: implement `offline_script` so tests never hit the network.
- Agents write artifacts and memory through `Agent.save_artifact` / `Agent.remember` so
  autonomy levels and provenance are applied consistently.
- New API capability → add the CLI command and the `api.ts` client method in the same change.
- Every change ships with tests in `backend/tests/`.
- New routes are covered by `api/guard.py` automatically; add them to its ADMIN / MAINTAINER /
  SIGNED_IN sets when the default (viewer reads, contributor writes) is wrong, and filter lists
  with `access.*` helpers. Agent-facing reads of repos or memory go through `access.filter_*`.
- A route that belongs to a user-facing area goes in `features.CATALOG` (a test checks every
  catalog route exists). Sign-in settings are read with `authconfig.cfg()`, not `get_settings()`.

## Do not touch
- Do not commit `.env`, databases (`*.db`) or API keys.
- Do not bypass approval gates or mark agent-written memory as confirmed in code.
