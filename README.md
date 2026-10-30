# Filament

**Agentic software delivery for every team, with one shared memory.**

Filament takes software from idea to production with specialist AI agents that work from a
shared project graph and team memory. Product, design, engineering, QA and platform teams all
work in the same place, and every agent output is linked, cited and reviewable.

> Status: early MVP (v0.1). See the [product spec](https://claude.ai/artifact/32bJmZyiJrCbpXsrvfKtSj)
> for the full vision and roadmap.

## What works today

| Capability | What it does |
| --- | --- |
| Project graph | Typed, versioned artifacts (idea, PRD, story, flow, ADR, …) and typed links between them; impact analysis ("what does changing this affect?") |
| Shared memory | Org / team / project facts, decisions and standards with provenance; agent-written entries stay *proposed* until a human confirms |
| Context engine | Builds a budgeted context pack per run from the graph and memory, stored on the run so every output is explainable |
| Agentic tool-use loop | Agents choose their own next step with tools (search graph, read, search memory, create/update/link artifacts, propose memory, ask a human, finish); the platform enforces turn and token budgets, autonomy and delegation limits, and logs every step |
| Orchestrator Agent | Takes a goal ("take this idea to a reviewed design"), inspects the project, delegates to specialists as child runs, skips work that already exists |
| Requirements Agent | Idea → PRD + user stories with Given/When/Then acceptance criteria, linked into the graph |
| Flow Agent | PRD/stories → user flow and sequence diagrams (Mermaid), rendered in the UI |
| Run log | Every model turn and tool call, tokens, context size, compactions and child runs, in the UI and via `fil trace` |
| MCP connectors | Add, edit, test, enable/disable MCP servers (stdio or HTTP) per workspace, team or project; allow-list tools; mark write tools and the autonomy they need; secrets only as `${env:NAME}` references |
| Runners | Choose where agents run: `inline` (API process, default), `worker`, `container`, `kubernetes`. Non-inline runs queue until `fil runner start <name>` picks them up wherever it runs |
| Agent defaults | Per workspace: runner, model, max turns, token and context budgets per agent; any run can override |
| Config as code | Export/apply runners, MCP servers and agent defaults as YAML (`fil config export / apply -f`), all-or-nothing |
| Context control | Old tool results are compacted, results are size-capped (valid JSON), and peak context per run is tracked |
| Grounding | Agents may only cite ids they have seen; unknown citations and unsourced memory proposals are rejected |
| Autonomy levels | L0 suggest · L1 draft · L2 send to review with an approval gate · L3/L4 act and report |
| Ask Filament | Answers from memory and the graph with citations; unanswered questions are logged |
| What we're missing | Unanswered questions and agent context misses clustered into gaps |
| Token usage | Tokens and cost per agent, project, model, person or status |
| Launchpad readiness | Grades any repo (local path or git URL) A–F on 12 agent-readiness checks and generates starter fixes |
| `fil` CLI | Every capability from the terminal |

## Quick start

Requirements: Python 3.11+, [uv](https://docs.astral.sh/uv/), Node 20+.

```bash
make setup          # install backend and frontend dependencies
make dev-api        # API on http://localhost:8000  (docs at /docs)
make dev-web        # UI on http://localhost:5173   (in a second terminal)
```

Without `ANTHROPIC_API_KEY` Filament runs in **offline mode**: agents return deterministic
drafts so you can try every flow. For live agents:

```bash
export ANTHROPIC_API_KEY=sk-ant-...
# optional: model routing and pricing (USD per million tokens) for cost tracking
export FILAMENT_MODEL_DEFAULT=claude-sonnet-5-5
export FILAMENT_PRICING='{"claude-sonnet-5-5": {"input": 3.0, "output": 15.0}}'
```

Or run everything in containers (Postgres included):

```bash
docker compose up --build
```

## CLI

```bash
cd backend && source .venv/bin/activate
fil ready scan github.com/acme/checkout-api      # Launchpad readiness (no server needed)
fil ready fix ./my-repo --write                  # starter AGENTS.md, CODEOWNERS, Makefile, ...

fil ws create "Acme"                              # then: export FIL_WORKSPACE=<id>
fil project create "Checkout v2"
fil memory add "All checkout payments use idempotency keys" --kind standard
fil run orchestrator -p <project-id> --idea "Let shoppers save a card" --autonomy L2 --trace
fil run requirements -p <project-id> --idea "Let shoppers save a card"
fil run flow -p <project-id>
fil trace <run-id>
fil approve <artifact-id>
fil ask "Do checkout payments use idempotency keys?"
fil gaps
fil usage --by agent

fil mcp add github --url https://api.githubcopilot.com/mcp/ \
  --header 'Authorization=${env:GITHUB_TOKEN}' --write create_pull_request --min-autonomy L3
fil mcp list && fil mcp test github
fil runner add build-box --kind worker
fil agent set requirements --runner build-box --model claude-opus-5-5 --max-turns 10
fil runner start build-box            # on the machine where runs should execute
fil config export > filament.yaml && fil config apply -f filament.yaml
```

## Repository layout

```
backend/    FastAPI service, agent runtime, readiness scanner, fil CLI, tests
frontend/   React + Vite workspace UI
docs/       Architecture notes and the OpenAPI contract
```

See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) and [CONTRIBUTING.md](CONTRIBUTING.md).

## License

MIT — see [LICENSE](LICENSE).
