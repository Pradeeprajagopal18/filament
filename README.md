# Ignite

**Agentic software delivery for every team, with one shared memory.**

Ignite takes software from idea to production with specialist AI agents that work from a
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
| Run log | Every model turn and tool call, tokens, context size, compactions and child runs, in the UI and via `ign trace` |
| MCP connectors | Add, edit, test, enable/disable MCP servers (stdio or HTTP) per workspace, team or project; allow-list tools; mark write tools and the autonomy they need; secrets only as `${env:NAME}` references |
| Runners | Choose where agents run: `inline` (API process, default), `worker`, `container`, `kubernetes`. Non-inline runs queue until `ign runner start <name>` picks them up wherever it runs |
| Agent defaults | Per workspace: runner, model, max turns, token and context budgets per agent; any run can override |
| Config as code | Export/apply runners, MCP servers and agent defaults as YAML (`ign config export / apply -f`), all-or-nothing |
| Context control | Old tool results are compacted, results are size-capped (valid JSON), and peak context per run is tracked |
| Grounding | Agents may only cite ids they have seen; unknown citations and unsourced memory proposals are rejected |
| Autonomy levels | L0 suggest · L1 draft · L2 send to review with an approval gate · L3/L4 act and report |
| Ask Ignite | Answers from memory and the graph with citations; unanswered questions are logged |
| What we're missing | Unanswered questions and agent context misses clustered into gaps |
| Token usage | Tokens and cost per agent, project, model, person or status |
| Launchpad repos | Onboards any repo (app, backend, API, frontend, infrastructure, config, library, monorepo): detects its type with evidence, runs only the readiness checks that apply, and creates missing pieces as drafts in Ignite's graph and memory |
| API docs | Collects OpenAPI / AsyncAPI / gRPC / GraphQL contracts, or infers endpoints from FastAPI, Flask, Django, Express, NestJS, Spring and Go code, with request/response models, auth, error codes and examples, as OpenAPI 3.1. Flags drift between a contract and the code, includes a mock server, and an optional model pass adds descriptions |
| UI mocks | Finds pages (React Router, Vue, Angular, Next.js, tab-based apps) and renders a wireframe of each from its real markup |
| System map | Links onboarded repos to each other from evidence: HTTP calls matched to the endpoints they hit, config repos, provisioned infrastructure, shared SaaS. Shows who uses each endpoint and calls to endpoints that no longer exist |
| Repo context for agents | Agents get compact repo summaries in their context and `search_repos` / `get_repo` tools, so PRDs and flows cite real endpoints, screens and affected systems |
| Kept current | Repos are rescanned on a schedule (and on GitHub push webhooks). Each scan is diffed against the last: breaking API changes are flagged to the repos that consume them, and Ignite's references refresh (approved ones wait for review) |
| MCP gateway | One registry and policy point for every tool agents can call: third-party MCP servers plus servers generated from onboarded repos' APIs (one tool per endpoint, call the real service or simulate from the contract). Tools start disabled until an owner enables them; re-syncs and description edits wait for review, with version history and rollback. Per-tool agent policy, rate limits, autonomy-gated writes, response projection (`fields`), PII/secret redaction, call logs and metrics. Agents switch to progressive discovery above a threshold, and the whole gateway is exposed as one MCP endpoint (Omni) for Claude Code and other clients |
| Component view | Maps what a repo connects to (databases, caches, queues, SaaS, internal services, cloud resources) from manifests, env vars, connection strings, compose, k8s and Terraform, each with file:line evidence |
| Code graph | Every scan builds a graph of the repo's modules, classes, functions and methods and their calls, imports, inheritance and references (Python, JavaScript, TypeScript, Java, Go; tree-sitter, no model), kept in Ignite, not in the repo. Calls are resolved through imports, `self`/`this` and the declared types of variables, fields and parameters; each link says whether it was resolved from the code or only matched by name. Rescans parse only changed files. Shows subsystems, hotspots, import cycles and which datastore, queue or SaaS each endpoint reaches and through which function |
| Explain, path, query | Look up any function or class: what it calls, what calls it, the endpoints it serves and the services it reaches; find how two symbols connect. Ambiguous names return the candidates instead of a guess. Available to agents, the CLI and the Omni MCP endpoint |
| Change impact | From changed files, a diff or a GitHub PR: the symbols changed, code affected, endpoints touched, other repos that call them, and a risk level; also checks two change sets for overlap |
| Design notes | `WHY:`, `NOTE:`, `DECISION:` and similar comments are proposed to project memory, linked to the ADRs they cite |
| Data model | Tables, fields and relationships from SQL migrations, SQLAlchemy, Django and Prisma models, as an ER diagram |
| Delivery pipeline | Each work item (a GitHub issue or one added by hand) moves through Plan → Design → Build → Test → Review → Deploy → Operate. An agent works each stage and a person signs off at the checkpoints: product owner (scope), tech lead (design), engineer (PR), release manager (production release), on-call (incidents). A stage can't start until the previous checkpoint is signed off |
| Triage Agent | Scores issue readiness (problem, acceptance criteria, scope, where in the code, repro steps, duplicates) and asks authors for what's missing |
| Coding Agent | Hands the task to Claude Code, Codex or Cursor with a brief grounded in the code graph: where to start, endpoints and the repos that call them, conventions from shared memory, test commands, branch name. The coding agent reads it over MCP (`ignite_get_task`) or from `ign delivery handoff`; its PR links itself |
| QA Agent | Test plan from acceptance criteria, touched endpoints and changed functions; reads CI and sends failures back to the coding agent until they pass, up to a limit, then asks a person |
| Review Agent | First-pass PR scorecard: secrets, endpoints other repos call, hotspots, missing tests, migrations, debug output, size. Posts on the PR only at autonomy L3+ |
| Release Agent | Rollout behind a flag in steps sized to risk, with rollback criteria tied to SLOs and the teams to tell. Production releases always need a release manager |
| SRE Agent | Watches SLO burn rates; on a breach during a rollout it rolls back, opens an incident for on-call and files a follow-up issue that goes back to triage |
| Access control | Sign-in with local accounts (admin / admin to start, changed at first sign-in), LDAP / Active Directory or the company SSO (OIDC), in any mix. Open and restricted projects with viewer/contributor/maintainer/owner roles, teams synced from SSO groups, private repos that mirror GitHub permissions, a system map that shows every repo but hides details of the ones you can't open, checkpoint roles for sign-offs, personal tokens for the CLI and MCP, and an audit log |
| `ign` CLI | Every capability from the terminal |

## Quick start

Requirements: Python 3.11+, [uv](https://docs.astral.sh/uv/), Node 20+.

```bash
make setup          # install backend and frontend dependencies
make dev-api        # API on http://localhost:8000  (docs at /docs)
make dev-web        # UI on http://localhost:5173   (in a second terminal)
# Port 8000 taken? Use another one for both: make dev-api API_PORT=8010 / make dev-web API_PORT=8010
```

Open the UI and sign in as **admin / admin**; you'll be asked to choose a new password. The CLI
signs in with `ign auth login`.

Without `ANTHROPIC_API_KEY` Ignite runs in **offline mode**: agents return deterministic
drafts so you can try every flow. For live agents:

```bash
export ANTHROPIC_API_KEY=sk-ant-...
# optional: model routing and pricing (USD per million tokens) for cost tracking
export IGNITE_MODEL_DEFAULT=claude-sonnet-5-5
export IGNITE_PRICING='{"claude-sonnet-5-5": {"input": 3.0, "output": 15.0}}'
```

Or run everything in containers (Postgres included). Settings go in a `.env` file next to
`docker-compose.yml`, which compose passes to the API container (git ignores it):

```bash
cp .env.example .env          # add ANTHROPIC_API_KEY, IGNITE_GIT_TOKEN, proxy settings
docker compose up --build
```

Any `IGNITE_*` setting can go in `.env`. For a one-off, `docker compose run -e NAME=value api …`
or plain `docker run --env-file .env -e NAME=value ignite-api` work too. Behind a corporate
proxy, Docker passes `HTTP_PROXY`/`HTTPS_PROXY` to the build automatically; for an internal
package mirror, set `PIP_INDEX_URL` in `.env` before `docker compose build`.

### API contracts generated from code

Many services don't check in an OpenAPI file; a generator builds it from annotations. Ignite
reads the same annotations, so the contract matches what the generator publishes:

| Stack | What Ignite reads |
|---|---|
| Java (springdoc, Springfox) | `@Operation`, `@ApiResponse`, `@Parameter`, `@Tag`, `@Hidden` / `@ApiOperation`, `@ApiParam`, `@Api`, `@ApiIgnore` |
| Go (swaggo) | `// @Summary`, `@Description`, `@Tags`, `@Param`, `@Success`, `@Failure`, `@Router`, `@Security`, `@BasePath`, and the structs they name |
| NestJS (`@nestjs/swagger`) | `@ApiOperation`, `@ApiResponse`, `@Api<Status>Response`, `@ApiTags`, `@ApiBearerAuth`, `@ApiExcludeEndpoint` |
| Python (FastAPI, drf-spectacular) | `summary`, `description`, `tags`, `responses`, `deprecated`, `include_in_schema`, docstrings, `@extend_schema` |

The API tab says which generator a repo uses and where it serves the spec (`/v3/api-docs`,
`/openapi.json`, `/swagger/doc.json`, …). To read the generated spec itself, give the repo the URL of a
running instance; every scan then reads it and compares it with the code:

```bash
ign repo spec-url <repo-id> http://localhost:8080/v3/api-docs   # '' to stop
```

### System map for one repo

Pick a repo on the System map (or "Show on system map" from a repo) to see only what it uses and
who integrates with it, one or more hops out. From the CLI:
`ign repo map --focus checkout-api --direction dependents --depth 2`.

### Private and organization repos

Public repos need nothing. For private repos, pick one:

- **A token**: set `IGNITE_GIT_TOKEN` to a fine-grained token with read access to the
  org's repos (Contents and Metadata; add Issues, Pull requests and Checks for the delivery
  pipeline). If the org uses single sign-on, authorize the token for the org. For GitHub
  Enterprise Server, add your host: `IGNITE_GITHUB_HOSTS='["github.com","github.yourcompany.com"]'`.
  The token is only sent to those hosts.
- **SSH**: onboard `git@github.com:org/repo.git`; your SSH key and agent are used.
- **A local clone**: onboard the folder path; Ignite reads it as it is.

git never prompts for a password: if credentials are missing the scan stops at once and says
what to set.

### Signing in

Everyone signs in. A new install starts with one account, **admin / admin**, and asks for a
new password at first sign-in; nothing else works until it's changed. Choose any mix of
sign-in methods with `IGNITE_AUTH_METHODS` (default `["local"]`):

- **local**: accounts kept in Ignite. Admins create them under People & access (or
  `ign access add --local <username>`) and hand over a one-time password; passwords are hashed
  with scrypt, need 12+ characters, and five wrong tries lock the account for 15 minutes.
- **ldap**: the company directory (LDAP or Active Directory). Ignite finds the person with a
  read-only service account and checks the password by binding as them; directory groups map
  to teams.

  ```bash
  IGNITE_LDAP_URL=ldaps://ldap.acme.com:636
  IGNITE_LDAP_BASE_DN=ou=people,dc=acme,dc=com
  IGNITE_LDAP_BIND_DN=cn=ignite-svc,ou=apps,dc=acme,dc=com
  IGNITE_LDAP_BIND_PASSWORD=...
  # optional: IGNITE_LDAP_USER_FILTER='(sAMAccountName={username})', IGNITE_LDAP_CA_FILE=...
  ```
- **oidc**: the company SSO (Okta, Entra ID, Google, Keycloak, ...).

  ```bash
  IGNITE_OIDC_ISSUER=https://acme.okta.com/oauth2/default
  IGNITE_OIDC_CLIENT_ID=...            # redirect URI: <IGNITE_UI_URL>/api/auth/callback
  IGNITE_OIDC_CLIENT_SECRET=...        # if your IdP issues one (PKCE is always used)
  IGNITE_UI_URL=https://ignite.acme.internal
  ```

For example `IGNITE_AUTH_METHODS='["local","ldap","oidc"]'` shows a username/password form
(local accounts first, then the directory) and a "Sign in with SSO" button. People listed in
`IGNITE_ADMIN_EMAILS` become admins when they first sign in through LDAP or SSO. Session
cookies are signed with `IGNITE_SESSION_SECRET` (or a key Ignite generates and keeps in its
data directory). For a laptop with no sign-in at all, set `IGNITE_AUTH_MODE=none`.

### Who can see what

The rules:

- **Workspaces**: everyone who signs in is a member (set `IGNITE_WORKSPACE_DEFAULT_ROLE=none`
  to require an admin to add people). Admins manage connectors, runners, agents, config and
  People & access.
- **Projects** are open (every member can see and contribute) or **restricted** (only their
  members, their team and admins). Roles: viewer, contributor, maintainer, owner. Whoever
  creates a project owns it. Teams map to SSO groups, synced at each sign-in.
- **Private repos mirror GitHub**: you see one if GitHub says you can read it (your GitHub
  username, from an SSO claim via `IGNITE_OIDC_GITHUB_CLAIM` or set by an admin; checked with
  the org token, cached 15 minutes). When GitHub can't tell, a role on the repo's project counts.
  Only people who can read a private repo on GitHub (or admins) can onboard it.
- **The system map** always shows every repo and how they connect, so the picture is whole.
  Repos you can't open are dashed and carry no details (no endpoints, evidence or grade).
- **Checkpoints** need their role (product owner, tech lead, engineer, release manager,
  on-call), assigned under People & access. When nobody holds it, project owners and
  maintainers sign off. Who signed is taken from the session, not the request.
- Things you can't see answer 404, so their existence doesn't leak. Agents act as the person
  who started the run and see only what that person can. Sign-ins, role changes, sign-offs,
  releases and denied requests go to the audit log.

The CLI and MCP clients use a personal token: `ign auth login` (username and password)
prints one, or create one under **My tokens**; set `IGNITE_TOKEN`. Only a hash is stored.

## CLI

```bash
cd backend && source .venv/bin/activate
ign ready scan github.com/acme/checkout-api      # Launchpad readiness (no server needed)
ign repo add github.com/acme/checkout-api        # onboard: type, readiness, API, UI, components
ign repo generate <id>                           # create missing items as drafts in Ignite
ign repo openapi <id> -o openapi.yaml            # API contract (checked in or inferred)
ign repo map                                     # how repos connect
ign repo impact <id>                             # who depends on this repo
ign repo rescan <id> | --due                     # rescan and show what changed
ign repo schedule <id> 24                        # rescan interval in hours (0 = manual)
ign delivery board <project-id>                  # work items and stage states
ign auth login                                   # username + password -> IGNITE_TOKEN
ign auth whoami                                  # who you are (IGNITE_TOKEN=ign_...)
ign access restrict <project-id>                 # members only (--off to open it again)
ign access set <user-id> --github <username>     # mirror their GitHub repo access
ign access checkpoint <user-id> release_manager  # who signs off production releases
ign delivery advance <item-id>                   # run the next stage's agent
ign delivery handoff <task-id> --prompt          # claude "$(ign delivery handoff <task-id> --prompt)"
ign delivery slo add <project-id> "Checkout availability" --objective 99.9
ign code summary <repo-id>                       # subsystems, hotspots, cycles, design notes
ign code explain <repo-id> place_order           # calls, callers, endpoints, services reached
ign code path <repo-id> place_order save_order   # how two symbols connect
ign code impact <repo-id> --git-diff main        # or: app/events.py | --pr <url>
ign code conflicts <repo-id> --change a=app/x.py --change b=app/x.py,app/y.py
ign mcp search "cart"                             # gateway registry (Code Mode)
ign mcp enable api-checkout-api get_cart         # tools start disabled
ign mcp call api-checkout-api get_cart --arg cart_id=c1 --fields id,total -o cart.json
ign mcp metrics                                  # calls, errors, latency, bytes trimmed
claude mcp add --transport http ignite http://localhost:8000/api/workspaces/<id>/mcp/omni
ign ready fix ./my-repo --write                  # starter AGENTS.md, CODEOWNERS, Makefile, ...

ign ws create "Acme"                              # then: export FIL_WORKSPACE=<id>
ign project create "Checkout v2"
ign memory add "All checkout payments use idempotency keys" --kind standard
ign run orchestrator -p <project-id> --idea "Let shoppers save a card" --autonomy L2 --trace
ign run requirements -p <project-id> --idea "Let shoppers save a card"
ign run flow -p <project-id>
ign trace <run-id>
ign approve <artifact-id>
ign ask "Do checkout payments use idempotency keys?"
ign gaps
ign usage --by agent

ign mcp add github --url https://api.githubcopilot.com/mcp/ \
  --header 'Authorization=${env:GITHUB_TOKEN}' --write create_pull_request --min-autonomy L3
ign mcp list && ign mcp test github
ign runner add build-box --kind worker
ign agent set requirements --runner build-box --model claude-opus-5-5 --max-turns 10
ign runner start build-box            # on the machine where runs should execute
ign config export > ignite.yaml && ign config apply -f ignite.yaml
```

## Repository layout

```
backend/    FastAPI service, agent runtime, readiness scanner, ign CLI, tests
frontend/   React + Vite workspace UI
docs/       Architecture notes and the OpenAPI contract
```

See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) and [CONTRIBUTING.md](CONTRIBUTING.md).

## License

MIT — see [LICENSE](LICENSE).
