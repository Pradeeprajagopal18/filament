# Architecture

Filament v0.1 is a single FastAPI service, a React UI and a CLI over one HTTP API.
The full target architecture is in the product spec; this note describes what exists in code.

```mermaid
flowchart TB
  subgraph Surfaces
    UI[React workspace]
    CLI[fil CLI]
    API_Clients[REST clients]
  end
  subgraph Service["FastAPI service"]
    Routes[API routers]
    Runner[Agent runner]
    Ctx[Context engine]
    LLM[llm.complete]
    Ready[Readiness scanner]
  end
  DB[(SQLite / Postgres)]
  Claude[Anthropic Messages API]

  UI --> Routes
  CLI --> Routes
  API_Clients --> Routes
  Routes --> Runner
  Runner --> Ctx
  Ctx --> DB
  Runner --> LLM --> Claude
  Runner --> DB
  Routes --> Ready
  Routes --> DB
```

## Data model (`backend/filament/models.py`)

| Table | Purpose |
| --- | --- |
| `workspaces`, `teams`, `projects` | Tenancy and ownership |
| `artifacts` | Typed, versioned graph nodes with status (draft → in_review → approved / rejected) |
| `links` | Typed edges (`implements`, `derives_from`, `visualizes`, …) |
| `memory` | Facts, decisions, standards, procedures with scope, provenance and status |
| `runs` | Every agent execution: input, output, context pack, model, tokens, cost, error |
| `approvals` | Human gates on artifacts |
| `signals` | Questions, unanswered questions and context misses for gap detection |
| `readiness_reports` | Launchpad results per repo |

## How a run works

1. `POST /api/projects/{id}/runs` creates a `Run` with an agent name, goal, input and autonomy level.
2. The **context engine** gathers target artifacts, their neighbours within two hops, and
   ranked memory visible to the project, within a token budget. The pack is saved on the run.
3. The **agent loop** (`agents/base.py`) sends the goal, context and tool definitions to the
   Messages API. The model replies with tool calls; the platform executes them
   (`agents/tools.py`) and returns results, until the model calls `finish` or a guardrail
   stops it (max turns, token budget). Each tool call runs in a savepoint, so a bad call is
   reported back to the model without corrupting the run.
4. Write tools apply the autonomy level: L0 saves nothing, L1 saves drafts, L2 sends to review
   with an approval, L3+ saves as approved. Agent memory is always *proposed*.
5. The **Orchestrator** has a `delegate` tool that runs specialists as child runs (depth 1),
   inheriting its autonomy. Its plan comes from the model, not from code.
6. Every model turn and tool call is stored in `runs.trace`, with tokens and cost. A failed run
   rolls back its writes but keeps its audit trail.

## Extending

- **New agent:** subclass `Agent`, set `system` (how it should work) and `tools`, implement
  `task` (the opening message), `offline_script` (a deterministic run for tests) and optionally
  `result`; register it in `agents/__init__.py`, add a test.
- **New tool:** add a handler and `Tool` entry in `agents/tools.py`; write tools must go through
  `Agent.save_artifact` / `Agent.remember`.
- **New readiness check:** add it in `readiness/scanner.py::run_checks` with a weight and a fix.

## Next (from the roadmap)

Durable background runs (workflow engine), pgvector semantic memory, code index and context
graph, connectors (GitHub, Jira, Slack) via MCP, test/release/ops agents, SSO and policies.
