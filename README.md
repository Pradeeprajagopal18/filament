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
| Launchpad repos | Onboards any repo (app, backend, API, frontend, infrastructure, config, library, monorepo): detects its type with evidence, runs only the readiness checks that apply, and creates missing pieces as drafts in Filament's graph and memory |
| API docs | Collects OpenAPI / AsyncAPI / gRPC / GraphQL contracts, or infers endpoints from FastAPI, Flask, Django, Express, NestJS, Spring and Go code, with request/response models, auth, error codes and examples, as OpenAPI 3.1. Flags drift between a contract and the code, includes a mock server, and an optional model pass adds descriptions |
| UI mocks | Finds pages (React Router, Vue, Angular, Next.js, tab-based apps) and renders a wireframe of each from its real markup |
| System map | Links onboarded repos to each other from evidence: HTTP calls matched to the endpoints they hit, config repos, provisioned infrastructure, shared SaaS. Shows who uses each endpoint and calls to endpoints that no longer exist |
| Repo context for agents | Agents get compact repo summaries in their context and `search_repos` / `get_repo` tools, so PRDs and flows cite real endpoints, screens and affected systems |
| Kept current | Repos are rescanned on a schedule (and on GitHub push webhooks). Each scan is diffed against the last: breaking API changes are flagged to the repos that consume them, and Filament's references refresh (approved ones wait for review) |
| MCP gateway | One registry and policy point for every tool agents can call: third-party MCP servers plus servers generated from onboarded repos' APIs (one tool per endpoint, call the real service or simulate from the contract). Tools start disabled until an owner enables them; re-syncs and description edits wait for review, with version history and rollback. Per-tool agent policy, rate limits, autonomy-gated writes, response projection (`fields`), PII/secret redaction, call logs and metrics. Agents switch to progressive discovery above a threshold, and the whole gateway is exposed as one MCP endpoint (Omni) for Claude Code and other clients |
| Component view | Maps what a repo connects to (databases, caches, queues, SaaS, internal services, cloud resources) from manifests, env vars, connection strings, compose, k8s and Terraform, each with file:line evidence |
| Code graph | Every scan builds a graph of the repo's modules, classes, functions and methods and their calls, imports, inheritance and references (Python, JavaScript, TypeScript, Java, Go; tree-sitter, no model), kept in Filament, not in the repo. Calls are resolved through imports, `self`/`this` and the declared types of variables, fields and parameters; each link says whether it was resolved from the code or only matched by name. Rescans parse only changed files. Shows subsystems, hotspots, import cycles and which datastore, queue or SaaS each endpoint reaches and through which function |
| Explain, path, query | Look up any function or class: what it calls, what calls it, the endpoints it serves and the services it reaches; find how two symbols connect. Ambiguous names return the candidates instead of a guess. Available to agents, the CLI and the Omni MCP endpoint |
| Change impact | From changed files, a diff or a GitHub PR: the symbols changed, code affected, endpoints touched, other repos that call them, and a risk level; also checks two change sets for overlap |
| Design notes | `WHY:`, `NOTE:`, `DECISION:` and similar comments are proposed to project memory, linked to the ADRs they cite |
| Data model | Tables, fields and relationships from SQL migrations, SQLAlchemy, Django and Prisma models, as an ER diagram |
| Delivery pipeline | Each work item (a GitHub issue or one added by hand) moves through Plan → Design → Build → Test → Review → Deploy → Operate. An agent works each stage and a person signs off at the checkpoints: product owner (scope), tech lead (design), engineer (PR), release manager (production release), on-call (incidents). A stage can't start until the previous checkpoint is signed off |
| Triage Agent | Scores issue readiness (problem, acceptance criteria, scope, where in the code, repro steps, duplicates) and asks authors for what's missing |
| Coding Agent | Hands the task to Claude Code, Codex or Cursor with a brief grounded in the code graph: where to start, endpoints and the repos that call them, conventions from shared memory, test commands, branch name. The coding agent reads it over MCP (`filament_get_task`) or from `fil delivery handoff`; its PR links itself |
| QA Agent | Test plan from acceptance criteria, touched endpoints and changed functions; reads CI and sends failures back to the coding agent until they pass, up to a limit, then asks a person |
| Review Agent | First-pass PR scorecard: secrets, endpoints other repos call, hotspots, missing tests, migrations, debug output, size. Posts on the PR only at autonomy L3+ |
| Release Agent | Rollout behind a flag in steps sized to risk, with rollback criteria tied to SLOs and the teams to tell. Production releases always need a release manager |
| SRE Agent | Watches SLO burn rates; on a breach during a rollout it rolls back, opens an incident for on-call and files a follow-up issue that goes back to triage |
| `fil` CLI | Every capability from the terminal |

## Quick start

Requirements: Python 3.11+, [uv](https://docs.astral.sh/uv/), Node 20+.

```bash
make setup          # install backend and frontend dependencies
make dev-api        # API on http://localhost:8000  (docs at /docs)
make dev-web        # UI on http://localhost:5173   (in a second terminal)
# Port 8000 taken? Use another one for both: make dev-api API_PORT=8010 / make dev-web API_PORT=8010
```

Without `ANTHROPIC_API_KEY` Filament runs in **offline mode**: agents return deterministic
drafts so you can try every flow. For live agents:

```bash
export ANTHROPIC_API_KEY=sk-ant-...
# optional: model routing and pricing (USD per million tokens) for cost tracking
export FILAMENT_MODEL_DEFAULT=claude-sonnet-5-5
export FILAMENT_PRICING='{"claude-sonnet-5-5": {"input": 3.0, "output": 15.0}}'
```

Or run everything in containers (Postgres included). Settings go in a `.env` file next to
`docker-compose.yml`, which compose passes to the API container (git ignores it):

```bash
cp .env.example .env          # add ANTHROPIC_API_KEY, FILAMENT_GIT_TOKEN, proxy settings
docker compose up --build
```

Any `FILAMENT_*` setting can go in `.env`. For a one-off, `docker compose run -e NAME=value api …`
or plain `docker run --env-file .env -e NAME=value filament-api` work too. Behind a corporate
proxy, Docker passes `HTTP_PROXY`/`HTTPS_PROXY` to the build automatically; for an internal
package mirror, set `PIP_INDEX_URL` in `.env` before `docker compose build`.

### Private and organization repos

Public repos need nothing. For private repos, pick one:

- **A token**: set `FILAMENT_GIT_TOKEN` to a fine-grained token with read access to the
  org's repos (Contents and Metadata; add Issues, Pull requests and Checks for the delivery
  pipeline). If the org uses single sign-on, authorize the token for the org. For GitHub
  Enterprise Server, add your host: `FILAMENT_GITHUB_HOSTS='["github.com","github.yourcompany.com"]'`.
  The token is only sent to those hosts.
- **SSH**: onboard `git@github.com:org/repo.git`; your SSH key and agent are used.
- **A local clone**: onboard the folder path; Filament reads it as it is.

git never prompts for a password: if credentials are missing the scan stops at once and says
what to set.

## CLI

```bash
cd backend && source .venv/bin/activate
fil ready scan github.com/acme/checkout-api      # Launchpad readiness (no server needed)
fil repo add github.com/acme/checkout-api        # onboard: type, readiness, API, UI, components
fil repo generate <id>                           # create missing items as drafts in Filament
fil repo openapi <id> -o openapi.yaml            # API contract (checked in or inferred)
fil repo map                                     # how repos connect
fil repo impact <id>                             # who depends on this repo
fil repo rescan <id> | --due                     # rescan and show what changed
fil repo schedule <id> 24                        # rescan interval in hours (0 = manual)
fil delivery board <project-id>                  # work items and stage states
fil delivery advance <item-id>                   # run the next stage's agent
fil delivery handoff <task-id> --prompt          # claude "$(fil delivery handoff <task-id> --prompt)"
fil delivery slo add <project-id> "Checkout availability" --objective 99.9
fil code summary <repo-id>                       # subsystems, hotspots, cycles, design notes
fil code explain <repo-id> place_order           # calls, callers, endpoints, services reached
fil code path <repo-id> place_order save_order   # how two symbols connect
fil code impact <repo-id> --git-diff main        # or: app/events.py | --pr <url>
fil code conflicts <repo-id> --change a=app/x.py --change b=app/x.py,app/y.py
fil mcp search "cart"                             # gateway registry (Code Mode)
fil mcp enable api-checkout-api get_cart         # tools start disabled
fil mcp call api-checkout-api get_cart --arg cart_id=c1 --fields id,total -o cart.json
fil mcp metrics                                  # calls, errors, latency, bytes trimmed
claude mcp add --transport http filament http://localhost:8000/api/workspaces/<id>/mcp/omni
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
