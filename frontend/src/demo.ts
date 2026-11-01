// In-browser demo backend: the same API surface as the FastAPI service, with sample data and
// simulated agents. Used only when the UI is built with VITE_DEMO=1 (the interactive preview).
import type {
  AgentSetting, Artifact, Check, Gap, Health, Link, McpServer, McpServerInput, Memory, Project, Readiness, Run,
  Impact, RepoAnalysis, RepoDetail, RepoScan, Runner, ScanChanges, SystemMap, TraceEvent, UsageRow, Workspace,
} from './api'
import demoRepos from './demo-repos.json'

// Real analyzer output for four sample repos (backend/scripts/demo_repos.py).
interface RepoSample {
  analysis: RepoAnalysis; conventions: string[]; openapi_yaml: string | null
  drafts: Record<string, { title: string; content: string; data: Record<string, unknown> }>
  scans: { trigger: string; status: string; grade: string; score: number; hours_ago: number; changes: ScanChanges }[]
  diagrams: string[]
}
const DIAGRAM_TYPE: Record<string, string> = { request_flows: 'flow', user_flow: 'flow', component_view: 'component_view' }
type DemoSystem = SystemMap & { consumers: Record<string, Record<string, { repo_id: string; repo: string; file: string; line: number }[]>> }
const DEMO_DATA = demoRepos as unknown as { repos: RepoSample[]; system: DemoSystem }
const SAMPLES = DEMO_DATA.repos
type DemoRepo = RepoDetail & { sample: number; scans: RepoScan[] }

let seq = 1000
const id = () => (seq++).toString(36).padStart(6, 'x') + Math.random().toString(36).slice(2, 8)
const now = () => new Date().toISOString()
const sleep = (ms: number) => new Promise(r => setTimeout(r, ms))
const words = (s: string) => new Set((s.toLowerCase().match(/[a-z0-9]{3,}/g) ?? []).filter(w => !STOP.has(w)))
const STOP = new Set(['the', 'and', 'for', 'with', 'how', 'what', 'why', 'who', 'does', 'our', 'are', 'this', 'that', 'from', 'use', 'can', 'did', 'was', 'which', 'when', 'where', 'have', 'into'])
const overlap = (a: Set<string>, b: Set<string>) => [...a].filter(w => b.has(w)).length

interface Signal { text: string; kind: 'question' | 'no_answer' | 'context_miss' }

const db = {
  workspaces: [] as Workspace[],
  projects: [] as Project[],
  artifacts: [] as Artifact[],
  links: [] as (Link & { project_id: string })[],
  runs: [] as (Run & { project_id: string })[],
  memory: [] as (Memory & { ws: string })[],
  signals: [] as Signal[],
  readiness: [] as Readiness[],
  mcp: [] as McpServer[],
  runners: [] as Runner[],
  agentSettings: {} as Record<string, Partial<AgentSetting> & { runner?: string }>,
  repos: [] as DemoRepo[],
}

function pickSample(ref: string): number {
  const r = ref.toLowerCase()
  const exact = SAMPLES.findIndex(s => s.analysis.repo === r || r.endsWith(s.analysis.name))
  if (exact >= 0) return exact
  if (/infra|terraform|platform|iac|k8s|helm/.test(r)) return SAMPLES.findIndex(s => s.analysis.profile.kind_names.includes('infrastructure'))
  if (/config|flags|settings|env/.test(r)) return SAMPLES.findIndex(s => s.analysis.profile.kind_names.includes('config'))
  if (/web|ui|app|front|site|store|portal/.test(r)) return SAMPLES.findIndex(s => s.analysis.profile.kind_names.includes('frontend'))
  return 0
}

function onboard(ws: string, ref: string): DemoRepo {
  const existing = db.repos.find(r => r.workspace_id === ws && r.ref === ref)
  if (existing) { existing.scanned_at = now(); return existing }
  const sample = pickSample(ref)
  const analysis: RepoAnalysis = JSON.parse(JSON.stringify(SAMPLES[sample].analysis))
  const name = ref.replace(/\.git$/, '').split('/').filter(Boolean).pop() ?? analysis.name
  analysis.repo = ref
  if (name !== analysis.name) {
    analysis.components.mermaid = analysis.components.mermaid.replaceAll(analysis.name, name)
    analysis.name = name
  }
  const project: Project = { id: id(), workspace_id: ws, name, description: `Repository ${ref} (${analysis.profile.kind_names.join(', ')})` }
  db.projects.push(project)
  const seeded = ref === SAMPLES[sample].analysis.repo
  const rid = id()
  const hoursAgo = (h: number) => new Date(Date.now() - h * 3600_000).toISOString()
  const scans: RepoScan[] = seeded
    ? SAMPLES[sample].scans.map(s => ({ id: id(), repo_id: rid, commit: null, error: null, ...s, scanned_at: hoursAgo(s.hours_ago) }))
    : [{ id: id(), repo_id: rid, trigger: 'onboard', status: 'ok', grade: analysis.readiness.grade, score: analysis.readiness.score, commit: null, error: null, scanned_at: now(), changes: { first_scan: true, summary: 'First scan', breaking: [], counts: {} } }]
  const r: DemoRepo = {
    id: rid, workspace_id: ws, project_id: project.id, ref, name, kinds: analysis.profile.kind_names,
    score: analysis.readiness.score, grade: analysis.readiness.grade, generated: {}, scanned_at: scans[0].scanned_at, analysis, sample,
    scan_interval_hours: 24, next_scan_at: new Date(new Date(scans[0].scanned_at).getTime() + 24 * 3600_000).toISOString(),
    last_error: null, commit: null, scans,
  }
  // Diagrams drawn from the code, kept in the repo's project like the server does.
  for (const g of SAMPLES[sample].diagrams ?? []) {
    const d = SAMPLES[sample].drafts[g]
    if (!d) continue
    const a = art(project.id, DIAGRAM_TYPE[g] ?? 'flow', d.title.replaceAll(SAMPLES[sample].analysis.name, name), d.content.replaceAll(SAMPLES[sample].analysis.name, name), 'draft', 'agent:repo-onboarding')
    a.data = { ...d.data, repo: ref, generator: g, in_repo: false }
    ;(r.generated as Record<string, string>)[g] = a.id
  }
  db.repos.unshift(r)
  return r
}

function stripSample(r: DemoRepo): RepoDetail {
  const { sample: _sample, scans: _scans, ...rest } = r
  void _sample; void _scans
  return clone(rest)
}

// The precomputed system map uses repo names as ids; map them onto this workspace's repos.
function demoSystem(ws: string): DemoSystem {
  const byName = new Map(db.repos.filter(r => r.workspace_id === ws).map(r => [r.name, r.id]))
  const sid = (x: string) => (x.startsWith('x:') ? x : byName.get(x) ?? '')
  const s = DEMO_DATA.system
  const nodes = s.nodes.filter(n => n.kind !== 'repo' || byName.has(n.id)).map(n => ({ ...n, id: sid(n.id) }))
  const keep = new Set(nodes.map(n => n.id))
  const edges = s.edges.map(e => ({ ...e, source: sid(e.source), target: sid(e.target) })).filter(e => keep.has(e.source) && keep.has(e.target))
  const consumers: DemoSystem['consumers'] = {}
  for (const [name, eps] of Object.entries(s.consumers)) {
    if (!byName.has(name)) continue
    consumers[byName.get(name)!] = Object.fromEntries(Object.entries(eps).map(([k, us]) => [k, us.map(u => ({ ...u, repo_id: sid(u.repo_id) }))]))
  }
  const unresolved = s.unresolved.filter(u => byName.has(u.repo) && byName.has(u.target)).map(u => ({ ...u, repo_id: sid(u.repo_id), target_id: sid(u.target_id) }))
  return { nodes, edges, mermaid: s.mermaid, unresolved, consumers }
}

function demoImpact(rid: string): Impact {
  const r = db.repos.find(x => x.id === rid)
  if (!r) throw new Error('404: Repo not found')
  const s = demoSystem(r.workspace_id)
  const names = Object.fromEntries(s.nodes.map(n => [n.id, n.label]))
  return {
    dependents: s.edges.filter(e => e.target === rid && !e.source.startsWith('x:')).map(e => ({ repo_id: e.source, repo: names[e.source], kind: e.kind, endpoints: e.endpoints, evidence: e.evidence })),
    dependencies: s.edges.filter(e => e.source === rid).map(e => ({ id: e.target, name: names[e.target], kind: e.kind, endpoints: e.endpoints })),
    endpoint_consumers: s.consumers[rid] ?? {},
    unresolved_calls: s.unresolved.filter(u => u.repo_id === rid || u.target_id === rid),
  }
}

function matchEndpoint(r: DemoRepo, method: string, path: string) {
  return r.analysis.api.endpoints.find(e => (e.method === method || e.method === 'ANY') &&
    new RegExp('^' + e.path.replace(/[.*+?^$()|[\]\\]/g, '\\$&').replace(/\{[^}]+\}/g, '[^/]+') + '/?$').test(path))
}

// Repos relevant to an idea, for the simulated agents' context (same idea as the server).
function relevantRepos(text: string) {
  const q = words(text)
  return db.repos.map(r => {
    const a = r.analysis
    const hay = words([r.name, ...a.api.endpoints.map(e => `${e.path} ${e.summary}`), ...a.ui.pages.map(p => `${p.path} ${p.component}`)].join(' ').replace(/[/{}_-]/g, ' '))
    return { r, score: overlap(q, hay) }
  }).filter(x => x.score >= 2).sort((a, b) => b.score - a.score).slice(0, 2).map(x => x.r)
}

const AGENTS: Record<string, string> = {
  requirements: 'Turns an idea or opportunity into a PRD and user stories with acceptance criteria.',
  flow: 'Draws user flows and sequence diagrams (Mermaid) from PRDs and stories.',
  orchestrator: 'Plans a goal across specialist agents, delegates, reviews and iterates.',
}
const DEFAULTS = { model: 'claude-sonnet-5-5', max_turns: 12, token_budget: 200000, context_budget: 12000 }

// Simulated tool catalogues for well-known MCP servers (the real server lists tools on connect).
const KNOWN_TOOLS: Record<string, [string, string][]> = {
  github: [['search_issues', 'Search issues and pull requests'], ['get_file_contents', 'Read a file from a repository'], ['list_commits', 'List recent commits'], ['create_pull_request', 'Open a pull request'], ['create_issue', 'Open an issue']],
  jira: [['search_issues', 'Search Jira with JQL'], ['get_issue', 'Read an issue'], ['create_issue', 'Create a Jira issue'], ['transition_issue', 'Move an issue to another status']],
  slack: [['search_messages', 'Search Slack messages'], ['post_message', 'Post a message to a channel']],
  sentry: [['list_issues', 'List recent errors'], ['get_issue_events', 'Read events for an error']],
}

function agentSetting(agent: string): AgentSetting {
  const s = db.agentSettings[agent] ?? {}
  const def = db.runners.find(r => r.is_default)
  const overridden = Object.keys(s).filter(k => (s as Record<string, unknown>)[k] != null)
  return {
    agent, description: AGENTS[agent] ?? '', runner: s.runner ?? def?.name ?? 'inline',
    model: s.model ?? DEFAULTS.model, max_turns: s.max_turns ?? DEFAULTS.max_turns,
    token_budget: s.token_budget ?? DEFAULTS.token_budget, context_budget: s.context_budget ?? DEFAULTS.context_budget, overridden,
  }
}

function runConfig(agent: string) {
  const a = agentSetting(agent)
  const r = db.runners.find(x => x.name === a.runner)
  return { runner: a.runner, runner_kind: r?.kind ?? 'inline', model: a.model, max_turns: a.max_turns, token_budget: a.token_budget, context_budget: a.context_budget }
}

function mcpTest(s: McpServer) {
  s.last_tested_at = now()
  const key = Object.keys(KNOWN_TOOLS).find(k => s.name.includes(k) || (s.url ?? '').includes(k) || s.args.join(' ').includes(k))
  if (!key) {
    s.last_test_ok = false
    s.last_test_error = 'Preview: only github, jira, slack and sentry servers are simulated here. The real server connects to any MCP server.'
    s.tools_cache = []
    return
  }
  s.last_test_ok = true; s.last_test_error = null
  s.tools_cache = KNOWN_TOOLS[key].map(([name, description]) => ({ name, description }))
}

const SECRET_KEY = /token|secret|password|passwd|api[_-]?key|authorization|credential/i
function checkSecrets(values: Record<string, string>, kind: string) {
  for (const [k, v] of Object.entries(values)) {
    if (SECRET_KEY.test(k) && !/^\$\{env:[A-Za-z_][A-Za-z0-9_]*\}$/.test(v)) {
      throw new Error(`422: ${kind}.${k} looks like a secret: store it as a reference such as \${env:${k.toUpperCase().replace(/[^A-Z0-9_]/g, '_')}}, not the value`)
    }
  }
}

// -- seed data ------------------------------------------------------------------------------
function art(project_id: string, type: string, title: string, content: string, status: string, author: string): Artifact {
  const a: Artifact = { id: id(), project_id, type, title, content, data: {}, status, version: 1, author, run_id: null, updated_at: now() }
  db.artifacts.push(a)
  return a
}
function link(project_id: string, s: Artifact, t: Artifact, kind: string) {
  db.links.push({ id: id(), project_id, source_id: s.id, target_id: t.id, kind })
}

function seed() {
  const ws: Workspace = { id: 'acme', name: 'Acme Engineering' }
  db.workspaces.push(ws)
  const p: Project = { id: 'checkout', workspace_id: ws.id, name: 'Checkout v2', description: 'Faster, safer checkout for returning shoppers' }
  const p2: Project = { id: 'onboarding', workspace_id: ws.id, name: 'Merchant onboarding', description: 'Self-serve onboarding for new merchants' }
  db.projects.push(p, p2)

  db.runners.push({ id: id(), name: 'k8s-agents', kind: 'kubernetes', description: 'Agent pods in the platform cluster', labels: ['prod'], config: { namespace: 'agents' }, is_default: false, max_concurrency: 8, timeout_s: 900, last_seen_at: now(), queued: 0 })
  db.agentSettings.requirements = { model: 'claude-opus-5-5', max_turns: 10 }
  for (const s of [...SAMPLES].reverse()) onboard(ws.id, s.analysis.repo)
  db.agentSettings.flow = { runner: 'k8s-agents' }
  const mk = (x: Partial<McpServer> & { name: string }): McpServer => {
    const s: McpServer = { id: id(), description: '', scope: 'workspace', scope_ref: null, transport: 'http', command: null, args: [], url: null, env: {}, headers: {}, enabled: true, allowed_tools: [], write_tools: [], min_autonomy_for_writes: 'L2', timeout_s: 30, tools_cache: [], last_test_ok: null, last_test_error: null, last_tested_at: null, ...x }
    mcpTest(s)
    return s
  }
  db.mcp.push(
    mk({ name: 'github', description: 'Code, issues and pull requests', url: 'https://api.githubcopilot.com/mcp/', headers: { Authorization: '${env:GITHUB_TOKEN}' }, write_tools: ['create_pull_request', 'create_issue'], min_autonomy_for_writes: 'L3' }),
    mk({ name: 'jira', description: 'Tickets for the payments team', transport: 'stdio', command: 'npx', args: ['-y', 'mcp-atlassian-jira'], env: { JIRA_API_TOKEN: '${env:JIRA_API_TOKEN}' }, allowed_tools: ['search_issues', 'get_issue', 'create_issue'], write_tools: ['create_issue'] }),
    mk({ name: 'sentry', description: 'Production errors', url: 'https://mcp.sentry.dev/mcp', headers: { Authorization: '${env:SENTRY_TOKEN}' }, enabled: false }),
  )

  const mem: [string, string, string, string][] = [
    ['org', 'standard', 'Card data is tokenized; services never store raw card numbers (PCI DSS).', 'confirmed'],
    ['org', 'standard', 'All public APIs have an OpenAPI spec checked in next to the code.', 'confirmed'],
    ['org', 'standard', 'Every payment write uses an idempotency key.', 'confirmed'],
    ['team', 'decision', 'Payments team uses Stripe as the card vault; no in-house vault.', 'confirmed'],
    ['project', 'decision', 'One-click checkout is limited to cards saved in the last 18 months.', 'proposed'],
    ['org', 'procedure', 'Production deploys need a QA-signed release gate.', 'confirmed'],
  ]
  for (const [scope, kind, content, status] of mem) {
    db.memory.push({ id: id(), ws: ws.id, scope, kind, content, status, author: status === 'proposed' ? 'agent:requirements' : 'priya (payments lead)' })
  }

  const idea = art(p.id, 'idea', 'Let shoppers save a card for one-click checkout',
    'Returning shoppers re-enter card details on every order. Let them save a card and pay in one click.', 'approved', 'pradeep')
  const prd = art(p.id, 'prd', 'PRD: One-click checkout with saved cards', [
    '# PRD: One-click checkout with saved cards', '',
    '## Problem', 'Returning shoppers re-type card details on every order; 31% of mobile checkouts are abandoned at the payment step.', '',
    '## Goals', '- Returning shoppers pay in one click with a saved card', '- Cut payment-step abandonment on mobile', '',
    '## Non-goals', '- Wallets (Apple Pay, Google Pay) — separate project', '- Storing raw card data (prohibited by standard)', '',
    '## Users', '- Signed-in returning shoppers', '- Support agents handling card issues', '',
    '## Requirements', '- Save a card at checkout via the vault (tokenized)', '- Show saved cards with brand and last four digits', '- Pay with a saved card in one click, with CVV re-entry only when risk rules require it', '- Remove a saved card from account settings', '',
    '## Non-functional requirements', '- Tokenized card data only (org standard)', '- Idempotency key on every payment write (org standard)', '- p95 payment confirmation under 2 s', '',
    '## Open questions', '- Do we require CVV for saved cards under 18 months old?', '- Which markets need explicit consent wording?',
  ].join('\n'), 'approved', 'agent:requirements')
  link(p.id, prd, idea, 'derives_from')
  const stories: [string, string][] = [
    ['Save a card at checkout', 'As a returning shopper, I want to save my card when I pay, so that next time is faster.\n\n### Acceptance criteria\n- Given I am signed in When I tick "Save card" and pay Then the card is stored as a vault token\n- Given the vault is unavailable When I pay Then payment still succeeds and the card is not saved'],
    ['Pay with a saved card in one click', 'As a returning shopper, I want to pay with a saved card in one click, so that checkout takes seconds.\n\n### Acceptance criteria\n- Given a saved card When I tap Pay Then the order is placed with an idempotency key\n- Given risk rules flag the order When I tap Pay Then I am asked for CVV'],
    ['Manage saved cards', 'As a shopper, I want to remove saved cards, so that I stay in control of my data.\n\n### Acceptance criteria\n- Given saved cards When I remove one Then it disappears and the vault token is deleted'],
    ['Handle declined saved cards', 'As a shopper, I want a clear message when a saved card is declined, so that I can pick another.\n\n### Acceptance criteria\n- Given a decline When paying in one click Then I see the reason and can choose another card'],
  ]
  const storyArts = stories.map(([t, c], i) => {
    const s = art(p.id, 'story', t, c, i < 2 ? 'approved' : 'in_review', 'agent:requirements')
    link(p.id, s, prd, 'implements')
    return s
  })
  const flow = art(p.id, 'flow', 'User flow: one-click checkout', [
    '```mermaid', 'flowchart TD', '  A(["Shopper opens checkout"]) --> B{"Saved card?"}',
    '  B -- no --> C["Enter card"] --> D{"Save card?"}', '  D -- yes --> E["Tokenize in vault"] --> F["Place order"]', '  D -- no --> F',
    '  B -- yes --> G{"Risk check"}', '  G -- low --> F', '  G -- high --> H["Ask for CVV"] --> F',
    '  F --> I{"Approved?"}', '  I -- yes --> J(["Order confirmed"])', '  I -- no --> K["Show decline reason"] --> B', '```', '',
    'Covers the happy path, the risk step-up and declines. Gap found: what happens when the vault is down during save? (asked the PM)',
  ].join('\n'), 'in_review', 'agent:flow')
  const seqd = art(p.id, 'flow', 'Sequence: pay with a saved card', [
    '```mermaid', 'sequenceDiagram', '  actor Shopper', '  participant Web', '  participant Checkout as Checkout API', '  participant Risk', '  participant Vault as Stripe vault',
    '  Shopper->>Web: Tap Pay', '  Web->>Checkout: POST /orders (idempotency key)', '  Checkout->>Risk: score(order)', '  Risk-->>Checkout: low', '  Checkout->>Vault: charge(token)', '  Vault-->>Checkout: approved', '  Checkout-->>Web: 201 order confirmed', '  Web-->>Shopper: Confirmation', '```',
  ].join('\n'), 'in_review', 'agent:flow')
  for (const d of [flow, seqd]) for (const s of [prd, ...storyArts]) link(p.id, d, s, 'visualizes')
  const adr = art(p.id, 'adr', 'ADR-7: Use the Stripe vault for saved cards', '# ADR-7: Use the Stripe vault\n\n**Decision:** store saved cards as Stripe customer payment methods.\n\n**Why:** keeps us out of PCI scope for storage; matches the team decision on vaults.\n\n**Consequences:** card portability depends on Stripe export.', 'approved', 'priya (payments lead)')
  link(p.id, adr, prd, 'relates_to')

  // A past orchestrated run, so the run log has something to show.
  const parent = runRecord(p.id, 'orchestrator', 'Take this idea to a reviewed design', 'L2', null)
  const req = runRecord(p.id, 'requirements', 'Draft PRD and stories', 'L2', parent.id)
  const fl = runRecord(p.id, 'flow', 'Draw the user flow and sequence', 'L2', parent.id)
  req.trace = [
    m(['search_memory', 'search_graph']), t('search_memory', { query: 'saved card one-click checkout' }), t('search_graph', { types: ['prd', 'story'] }),
    m(['mcp__jira__search_issues'], 'Check Jira for related tickets before writing.'), t('mcp__jira__search_issues', { jql: 'project = PAY AND text ~ "saved card"' }),
    { type: 'compact', results: 3, before: 31420, after: 9860 },
    m(['create_artifact'], 'Two org standards apply: tokenization and idempotency keys. PAY-212 already asks for saved cards; no existing PRD.'), t('create_artifact', { type: 'prd', title: prd.title }),
    m(['ask_human'], 'CVV policy for saved cards is not defined anywhere.'), t('ask_human', { question: 'Do we require CVV for saved cards under 18 months old?' }),
    m(['create_artifact', 'create_artifact', 'create_artifact', 'create_artifact']), ...storyArts.map(s => t('create_artifact', { type: 'story', title: s.title })),
    m(['propose_memory']), t('propose_memory', { content: 'One-click checkout is limited to cards saved in the last 18 months.' }),
    m(['finish']), t('finish', { summary: 'PRD and 4 stories drafted; 1 question for the PM; 1 decision proposed.' }), { type: 'stop', reason: 'finished' },
  ]
  Object.assign(req, { peak_context_tokens: 31420, turns: 7, input_tokens: 18240, output_tokens: 3920, output: { summary: 'PRD and 4 stories drafted; 1 question for the PM; 1 decision proposed.', created: [prd, ...storyArts].map(brief), questions: ['Do we require CVV for saved cards under 18 months old?'] } })
  fl.trace = [
    m(['search_graph']), t('search_graph', { types: ['prd', 'story'] }), m(['read_artifact', 'read_artifact']), t('read_artifact', { id: prd.id }), t('read_artifact', { id: storyArts[1].id }),
    m(['create_artifact', 'create_artifact'], 'Flow exposes a gap: vault outage during save.'), t('create_artifact', { type: 'flow', title: flow.title }), t('create_artifact', { type: 'flow', title: seqd.title }),
    m(['ask_human']), t('ask_human', { question: 'What should happen if the vault is down while saving a card?' }),
    m(['finish']), t('finish', { summary: 'User flow and sequence drawn; flagged one gap.' }), { type: 'stop', reason: 'finished' },
  ]
  Object.assign(fl, { peak_context_tokens: 12800, turns: 5, input_tokens: 14310, output_tokens: 2680, output: { summary: 'User flow and sequence drawn; flagged one gap.', created: [flow, seqd].map(brief), questions: ['What should happen if the vault is down while saving a card?'] } })
  parent.trace = [
    m(['search_graph', 'search_memory'], 'Nothing exists yet for this idea. Plan: requirements, then flow.'), t('search_graph', { types: ['prd', 'story', 'flow'] }), t('search_memory', { query: 'saved cards checkout' }),
    m(['delegate']), t('delegate', { agent: 'requirements', goal: 'Draft PRD and stories' }),
    m(['read_artifact'], 'PRD looks complete; check story coverage of declines before drawing.'), t('read_artifact', { id: prd.id }),
    m(['delegate']), t('delegate', { agent: 'flow', goal: 'Draw the user flow and sequence' }),
    m(['finish']), t('finish', { summary: 'Idea taken to design: PRD, 4 stories, 2 diagrams. Waiting on 2 human answers and review.' }), { type: 'stop', reason: 'finished' },
  ]
  Object.assign(parent, { peak_context_tokens: 6100, turns: 5, input_tokens: 9120, output_tokens: 1310, output: { summary: 'Idea taken to design: PRD, 4 stories, 2 diagrams. Waiting on 2 human answers and review.', created: [], questions: [], child_run_ids: [req.id, fl.id] } })

  db.signals.push(
    { kind: 'no_answer', text: 'How do we rotate the Kafka credentials for payments?' },
    { kind: 'no_answer', text: 'Where is the Kafka runbook for consumer lag?' },
    { kind: 'no_answer', text: 'Who owns the Kafka cluster upgrade?' },
    { kind: 'context_miss', text: '[flow] vault outage behaviour not documented' },
    { kind: 'no_answer', text: 'What is the vault outage fallback for saved cards?' },
    { kind: 'no_answer', text: 'Is there a load test baseline for checkout?' },
  )

  for (const repo of ['github.com/acme/checkout-api', 'github.com/acme/web-storefront', 'github.com/acme/ledger-svc']) {
    db.readiness.push(simulateScan(repo))
  }
}

function brief(a: Artifact) { return { id: a.id, type: a.type, title: a.title } }
function m(tools: string[], text = ''): TraceEvent { return { type: 'model', tool_calls: tools, text } }
function t(tool: string, input: unknown): TraceEvent { return { type: 'tool', tool, input, result: { ok: true }, error: false } }

function runRecord(project_id: string, agent: string, goal: string, autonomy: string, parent: string | null) {
  const r: Run & { project_id: string } = {
    id: id(), project_id, parent_run_id: parent, agent, goal, status: 'succeeded', autonomy, trace: [], turns: 0,
    output: {}, context_pack: { items: [], approx_tokens: 0 }, model: 'claude-sonnet-5-5', input_tokens: 0,
    output_tokens: 0, cost_usd: null, error: null, started_at: now(),
    config: runConfig(agent), peak_context_tokens: 0, claimed_by: null,
  }
  if (r.config.runner_kind !== 'inline') r.claimed_by = `${r.config.runner}-1`
  db.runs.unshift(r)
  return r
}

// -- simulated agents --------------------------------------------------------------------------
function statusFor(autonomy: string) { return autonomy === 'L1' ? 'draft' : autonomy === 'L2' ? 'in_review' : 'approved' }

function relevantMemory(text: string) {
  const q = words(text)
  return db.memory.filter(e => e.status !== 'retired' && (e.kind === 'standard' || overlap(q, words(e.content)) > 0))
}

function simulateRequirements(p: Project, ideaText: string, autonomy: string, parent: string | null) {
  const r = runRecord(p.id, 'requirements', 'Draft PRD and stories', autonomy, parent)
  const first = ideaText.trim().split('\n')[0].slice(0, 90)
  const mem = relevantMemory(ideaText)
  const save = autonomy !== 'L0'
  const created: Artifact[] = []
  let idea: Artifact | null = null
  if (save) idea = art(p.id, 'idea', first, ideaText, 'approved', 'you')
  const nfr = mem.filter(e => e.kind === 'standard').map(e => `- ${e.content}`).join('\n') || '- Meets the team\'s adopted standards'
  const repos = relevantRepos(ideaText)
  const q = words(ideaText)
  const affected = repos.map(rp => {
    const eps = rp.analysis.api.endpoints.filter(e => overlap(q, words(`${e.path.replace(/[/{}_-]/g, ' ')} ${e.summary}`)) > 0).slice(0, 5)
    const pages = rp.analysis.ui.pages.filter(pg => overlap(q, words(`${pg.path} ${pg.component}`)) > 0).slice(0, 4)
    const users = demoImpact(rp.id).dependents.map(d => `${d.repo} (${d.kind})`)
    return [`- **${rp.name}** (${rp.kinds.join(', ')})`,
      eps.length ? `  - Endpoints: ${eps.map(e => `\`${e.method} ${e.path}\``).join(', ')}` : '',
      pages.length ? `  - Screens: ${pages.map(pg => `\`${pg.path}\``).join(', ')}` : '',
      users.length ? `  - Used by: ${users.join('; ')}` : ''].filter(Boolean).join('\n')
  }).join('\n') || '- None of the onboarded repos match this idea (to confirm)'
  if (save) {
    const prd = art(p.id, 'prd', `PRD: ${first}`, `# PRD: ${first}\n\n## Problem\n${ideaText}\n\n## Goals\n- ${first}\n- Measurable improvement for the target users\n\n## Non-goals\n- Anything outside the idea as described\n\n## Users\n- Primary users of ${p.name} (to confirm)\n\n## Requirements\n- Core flow for: ${first.toLowerCase()}\n- Clear errors and recovery paths\n- Settings to manage the feature\n\n## Affected systems\n${affected}\n\n## Non-functional requirements\n${nfr}\n\n## Open questions\n- Which users or markets go first?\n- What success metric and target do we commit to?`, statusFor(autonomy), 'agent:requirements')
    if (idea) link(p.id, prd, idea, 'derives_from')
    created.push(prd)
    for (const [title, want] of [[`Core flow: ${first}`, first.toLowerCase()], ['Errors and recovery', 'clear errors and a way to recover'], ['Manage settings', 'to manage this feature in settings']]) {
      const s = art(p.id, 'story', title, `As a user, I want ${want}, so that I get the outcome I came for.\n\n### Acceptance criteria\n- Given a signed-in user When they use the feature Then it completes\n- Given a failure When it happens Then a clear message and next step are shown`, statusFor(autonomy), 'agent:requirements')
      link(p.id, s, prd, 'implements')
      created.push(s)
    }
  }
  const question = 'Which success metric and target should this commit to?'
  r.trace = [
    m(['search_memory', 'search_graph', 'search_repos']), t('search_memory', { query: first }), t('search_graph', { types: ['prd', 'story'] }), t('search_repos', { query: first }),
    ...(repos.length ? [m(repos.map(() => 'get_repo'), `${repos.length} onboarded repo${repos.length > 1 ? 's' : ''} look relevant.`), ...repos.map(rp => t('get_repo', { repo: rp.name, detail: 'endpoints' }))] : []),
    m(['create_artifact'], `${mem.length} memory entries apply; no existing PRD.`), t('create_artifact', { type: 'prd', title: `PRD: ${first}` }),
    m(['create_artifact', 'create_artifact', 'create_artifact']), ...['Core flow', 'Errors and recovery', 'Manage settings'].map(x => t('create_artifact', { type: 'story', title: x })),
    m(['ask_human'], 'No success metric is defined.'), t('ask_human', { question }),
    m(['finish']), t('finish', { summary: `PRD and 3 stories drafted using ${mem.length} memory entries.` }), { type: 'stop', reason: 'finished' },
  ]
  if (!save) r.trace.splice(4, 0, { type: 'tool', tool: 'note', input: 'L0: write tools saved nothing', result: {}, error: false })
  db.signals.push({ kind: 'question', text: question })
  Object.assign(r, {
    peak_context_tokens: 8000 + Math.round(Math.random() * 4000), turns: 5, input_tokens: 9000 + Math.round(Math.random() * 6000), output_tokens: 2400 + Math.round(Math.random() * 1500),
    context_pack: { items: [...repos.map(rp => ({ kind: 'repo', id: rp.id, title: `repo ${rp.name}` })), ...mem.map(e => ({ kind: 'memory', id: e.id, title: e.kind }))], approx_tokens: 900 + repos.length * 500 },
    output: { summary: save ? `PRD and 3 stories drafted using ${mem.length} memory entries.` : 'Suggested a PRD and 3 stories (L0: nothing saved).', created: created.map(brief), questions: [question], prd_id: created[0]?.id ?? null },
  })
  return r
}

function simulateFlow(p: Project, autonomy: string, parent: string | null) {
  const r = runRecord(p.id, 'flow', 'Draw the user flow and sequence', autonomy, parent)
  const stories = db.artifacts.filter(a => a.project_id === p.id && a.type === 'story' && a.status !== 'rejected').slice(-4)
  const prd = db.artifacts.filter(a => a.project_id === p.id && a.type === 'prd').slice(-1)[0]
  if (!stories.length) {
    // No requirements: point at the diagrams drawn from the project's code, or ask for a PRD.
    const existing = db.artifacts.filter(a => a.project_id === p.id && (a.type === 'flow' || a.type === 'component_view'))
    const summary = existing.length
      ? `No PRD or stories to diagram yet. This project already has ${existing.length} diagram(s) drawn from its code: ${existing.slice(0, 3).map(e => `'${e.title}'`).join(', ')}. Add an idea or PRD to get feature flows.`
      : 'Nothing to diagram yet: asked for requirements.'
    r.trace = [m(['search_graph']), t('search_graph', { types: ['prd', 'story'] }), m(['search_graph']), t('search_graph', { types: ['flow', 'component_view'] }),
      ...(existing.length ? [] : [m(['ask_human']), t('ask_human', { question: `${p.name} has no PRD or stories yet. What feature should the flow cover?` })]),
      m(['finish']), t('finish', { summary }), { type: 'stop', reason: 'finished' }]
    Object.assign(r, { turns: 2, input_tokens: 1800, output_tokens: 300, output: { summary, created: [], questions: [] } })
    return r
  }
  const lab = (s: string) => s.replace(/["[\](){}<>|:;]/g, '').slice(0, 48)
  const lines = ['flowchart TD', '  A(["Start"]) --> B{"Signed in?"}', '  B -- no --> L["Sign in"] --> C', '  B -- yes --> C']
  stories.forEach((s, i) => lines.push(`  ${i === 0 ? 'C' : 'S' + i}["${lab(s.title)}"] --> ${i === stories.length - 1 ? 'D' : 'S' + (i + 1)}`))
  lines.push('  D{"Succeeded?"}', '  D -- yes --> E(["Done"])', '  D -- no --> F["Show error and next step"] --> C')
  const created: Artifact[] = []
  if (autonomy !== 'L0') {
    const f = art(p.id, 'flow', `User flow: ${prd ? prd.title.replace('PRD: ', '') : p.name}`, '```mermaid\n' + lines.join('\n') + '\n```', statusFor(autonomy), 'agent:flow')
    const sq = art(p.id, 'flow', 'Sequence: main path', ['```mermaid', 'sequenceDiagram', '  actor User', '  participant Web', '  participant API', '  participant DB', '  User->>Web: Start', '  Web->>API: request', '  API->>DB: read / write', '  DB-->>API: ok', '  API-->>Web: result', '  Web-->>User: Done', '```'].join('\n'), statusFor(autonomy), 'agent:flow')
    for (const d of [f, sq]) for (const s of [...stories, ...(prd ? [prd] : [])]) link(p.id, d, s, 'visualizes')
    created.push(f, sq)
  }
  r.trace = [
    m(['search_graph']), t('search_graph', { types: ['prd', 'story'] }), m(['read_artifact']), t('read_artifact', { id: prd?.id }),
    m(['create_artifact', 'create_artifact']), t('create_artifact', { type: 'flow', title: 'User flow' }), t('create_artifact', { type: 'flow', title: 'Sequence' }),
    m(['finish']), t('finish', { summary: `Drew a user flow and a sequence from ${stories.length} stories.` }), { type: 'stop', reason: 'finished' },
  ]
  Object.assign(r, { peak_context_tokens: 6000 + Math.round(Math.random() * 3000), turns: 4, input_tokens: 7000 + Math.round(Math.random() * 4000), output_tokens: 1800 + Math.round(Math.random() * 900), output: { summary: `Drew a user flow and a sequence from ${stories.length} stories.`, created: created.map(brief), questions: [] } })
  return r
}

function simulateOrchestrator(p: Project, ideaText: string, autonomy: string) {
  const parent = runRecord(p.id, 'orchestrator', 'Take this idea to a reviewed design', autonomy, null)
  const req = simulateRequirements(p, ideaText, autonomy, parent.id)
  const fl = simulateFlow(p, autonomy, parent.id)
  // children were unshifted after the parent; keep parent first in the list
  db.runs = [parent, ...db.runs.filter(r => r.id !== parent.id)]
  parent.trace = [
    m(['search_graph', 'search_memory'], 'Plan: requirements first, then flow.'), t('search_graph', { types: ['prd', 'story', 'flow'] }), t('search_memory', { query: ideaText.slice(0, 60) }),
    m(['delegate']), t('delegate', { agent: 'requirements', goal: 'Draft PRD and stories' }),
    m(['delegate'], 'Stories cover the core flow and errors; ready to draw.'), t('delegate', { agent: 'flow', goal: 'Draw the user flow and sequence' }),
    m(['finish']), t('finish', { summary: 'Idea taken to design. Outputs are waiting for human review.' }), { type: 'stop', reason: 'finished' },
  ]
  Object.assign(parent, { peak_context_tokens: 4200, turns: 4, input_tokens: 5200, output_tokens: 900, output: { summary: 'Idea taken to design: PRD, stories and two diagrams are waiting for your review.', created: [], questions: [], child_run_ids: [req.id, fl.id] } })
  return parent
}

// -- readiness (simulated) -------------------------------------------------------------------
const CHECKS: [string, string, number, string][] = [
  ['readme', 'README explains the project', 10, 'Add a README covering purpose, setup, how to run and how to test.'],
  ['agent_instructions', 'Agent instructions file', 15, 'Add AGENTS.md with build/test commands, layout and conventions.'],
  ['codeowners', 'Code owners defined', 8, 'Add .github/CODEOWNERS.'],
  ['tests', 'Automated tests present', 15, 'Add a test suite agents can run.'],
  ['ci', 'CI pipeline configured', 10, 'Add a CI workflow that runs on every pull request.'],
  ['one_command', 'Build and test in one command', 10, 'Add a Makefile with build and test targets.'],
  ['lint', 'Lint and format config', 7, 'Add linter and formatter config.'],
  ['api_specs', 'API contracts checked in', 5, 'Check in OpenAPI or proto specs.'],
  ['dev_env', 'Reproducible dev environment', 8, 'Add a Dockerfile or devcontainer.'],
  ['docs', 'Contributor and architecture docs', 7, 'Add CONTRIBUTING.md and ARCHITECTURE.md.'],
  ['lockfile', 'Dependencies pinned', 5, 'Commit a lockfile.'],
  ['no_secrets_files', 'No .env files committed', 5, 'Remove committed .env files and rotate secrets.'],
]

function simulateScan(repo: string): Readiness {
  let h = 0
  for (const c of repo) h = (h * 31 + c.charCodeAt(0)) >>> 0
  const checks: Check[] = CHECKS.map(([cid, title, weight, fix], i) => {
    const passed = ((h >> i) & 3) !== 0 || cid === 'readme'
    return { id: cid, title, weight, fix, passed, detail: passed ? 'Found (simulated)' : 'Not found (simulated)' }
  })
  const total = checks.reduce((s, c) => s + c.weight, 0)
  const score = Math.round((1000 * checks.filter(c => c.passed).reduce((s, c) => s + c.weight, 0)) / total) / 10
  const grade = score >= 90 ? 'A' : score >= 75 ? 'B' : score >= 60 ? 'C' : score >= 40 ? 'D' : 'F'
  return { id: id(), repo, score, grade, checks, created_at: now() }
}

// -- API surface ---------------------------------------------------------------------------
const project = (pid: string) => {
  const p = db.projects.find(x => x.id === pid)
  if (!p) throw new Error('404: project not found')
  return p
}
const clone = <T,>(v: T): T => JSON.parse(JSON.stringify(v))

export const demoApi = {
  health: async (): Promise<Health> => ({ status: 'ok', offline: false, model: 'claude-sonnet-5-5 (simulated)', version: '0.1.0-demo' }),
  workspaces: async () => clone(db.workspaces),
  createWorkspace: async (name: string) => { const w = { id: id(), name }; db.workspaces.push(w); return w },
  projects: async (ws: string) => clone(db.projects.filter(p => p.workspace_id === ws)),
  createProject: async (ws: string, name: string, description: string) => {
    const p = { id: id(), workspace_id: ws, name, description }; db.projects.push(p); return clone(p)
  },
  artifacts: async (p: string) => clone(db.artifacts.filter(a => a.project_id === p)),
  graph: async (p: string) => clone({ nodes: db.artifacts.filter(a => a.project_id === p), edges: db.links.filter(l => l.project_id === p) }),
  impact: async (a: string) => {
    const seen = new Set([a]); let frontier = [a]
    for (let hop = 0; hop < 3; hop++) {
      const next: string[] = []
      for (const l of db.links) for (const [x, y] of [[l.source_id, l.target_id], [l.target_id, l.source_id]]) {
        if (frontier.includes(x) && !seen.has(y)) { seen.add(y); next.push(y) }
      }
      frontier = next
    }
    seen.delete(a)
    return clone({ affected: db.artifacts.filter(x => seen.has(x.id)) })
  },
  decide: async (aid: string, decision: 'approved' | 'rejected') => {
    const a = db.artifacts.find(x => x.id === aid)!
    a.status = decision; a.updated_at = now()
    return clone(a)
  },
  runs: async (p: string) => clone(db.runs.filter(r => r.project_id === p)),
  startRun: async (pid: string, agent: string, _goal: string, input: Record<string, unknown>, autonomy: string) => {
    const p = project(pid)
    await sleep(agent === 'orchestrator' ? 2200 : 1400)
    const idea = String(input.idea ?? '')
    const r = agent === 'orchestrator' ? simulateOrchestrator(p, idea, autonomy)
      : agent === 'requirements' ? simulateRequirements(p, idea, autonomy, null)
        : simulateFlow(p, autonomy, null)
    return clone(r)
  },
  memory: async (ws: string) => clone(db.memory.filter(e => e.ws === ws)),
  addMemory: async (ws: string, scope: string, kind: string, content: string) => {
    const e = { id: id(), ws, scope, kind, content, status: 'confirmed', author: 'you' }
    db.memory.unshift(e); return clone(e)
  },
  memoryAction: async (mid: string, action: 'confirm' | 'retire') => {
    const e = db.memory.find(x => x.id === mid)!
    e.status = action === 'confirm' ? 'confirmed' : 'retired'
    return clone(e)
  },
  ask: async (_ws: string, question: string) => {
    await sleep(600)
    const q = words(question)
    const min = q.size > 2 ? 2 : 1
    const hits = [
      ...db.artifacts.map(a => ({ s: overlap(q, words(a.title + ' ' + a.content)), ref: `artifact:${a.id}`, title: a.title })),
      ...db.memory.filter(e => e.status !== 'retired').map(e => ({ s: overlap(q, words(e.content)) + 1, ref: `memory:${e.id}`, title: e.content })),
    ].filter(h => h.s >= min).sort((x, y) => y.s - x.s).slice(0, 3)
    db.signals.push({ kind: hits.length ? 'question' : 'no_answer', text: question })
    if (!hits.length) return { answer: 'Nothing in Filament answers this yet. It has been logged as a gap.', citations: [], answered: false }
    return { answer: `From what the team has recorded: ${hits.map(h => h.title).join(' · ')}`, citations: hits.map(h => h.ref), answered: true }
  },
  usage: async (_ws: string, by: string): Promise<UsageRow[]> => {
    const key = (r: Run & { project_id: string }) => by === 'project' ? (db.projects.find(p => p.id === r.project_id)?.name ?? r.project_id)
      : by === 'model' ? (r.model ?? '—') : by === 'person' ? (r.parent_run_id ? 'agent:orchestrator' : 'you') : by === 'status' ? r.status : r.agent
    const rows = new Map<string, UsageRow>()
    for (const r of db.runs) {
      const k = key(r)
      const row = rows.get(k) ?? { key: k, runs: 0, input_tokens: 0, output_tokens: 0, cost_usd: 0 }
      row.runs++; row.input_tokens += r.input_tokens; row.output_tokens += r.output_tokens
      row.cost_usd = Math.round(((row.cost_usd ?? 0) + r.input_tokens / 1e6 * 3 + r.output_tokens / 1e6 * 15) * 1e4) / 1e4
      rows.set(k, row)
    }
    return [...rows.values()].sort((a, b) => b.input_tokens + b.output_tokens - a.input_tokens - a.output_tokens)
  },
  gaps: async (): Promise<Gap[]> => {
    const missing = db.signals.filter(s => s.kind !== 'question')
    const freq = new Map<string, number>()
    for (const s of missing) for (const w of words(s.text)) freq.set(w, (freq.get(w) ?? 0) + 1)
    const clusters = new Map<string, string[]>()
    for (const s of missing) {
      const ws = [...words(s.text)]
      const topic = ws.sort((a, b) => (freq.get(b) ?? 0) - (freq.get(a) ?? 0) || a.localeCompare(b))[0] ?? 'other'
      clusters.set(topic, [...(clusters.get(topic) ?? []), s.text])
    }
    return [...clusters.entries()].map(([topic, ex]) => ({ topic, count: ex.length, examples: ex.slice(0, 5) })).sort((a, b) => b.count - a.count)
  },
  scan: async (repo: string) => { await sleep(900); const r = simulateScan(repo); db.readiness.unshift(r); return clone(r) },
  readiness: async () => clone(db.readiness),
  repos: async (ws: string) => db.repos.filter(r => r.workspace_id === ws).map(stripSample),
  onboardRepo: async (ws: string, ref: string) => { await sleep(1200); return stripSample(onboard(ws, ref)) },
  repo: async (rid: string) => {
    const r = db.repos.find(x => x.id === rid); if (!r) throw new Error('404: Repo not found')
    return stripSample(r)
  },
  rescanRepo: async (rid: string) => {
    await sleep(800)
    const r = db.repos.find(x => x.id === rid); if (!r) throw new Error('404: Repo not found')
    r.scanned_at = now()
    r.next_scan_at = r.scan_interval_hours ? new Date(Date.now() + r.scan_interval_hours * 3600_000).toISOString() : null
    const refs = Object.keys(r.generated)
    r.scans.unshift({ id: id(), repo_id: rid, trigger: 'manual', status: 'ok', grade: r.grade, score: r.score, commit: null, error: null, scanned_at: r.scanned_at,
      changes: { summary: 'No changes', breaking: [], counts: {}, references: { unchanged: refs } } })
    return stripSample(r)
  },
  repoScans: async (rid: string) => clone(db.repos.find(x => x.id === rid)?.scans ?? []),
  setRepoSchedule: async (rid: string, hours: number) => {
    const r = db.repos.find(x => x.id === rid); if (!r) throw new Error('404: Repo not found')
    r.scan_interval_hours = hours
    r.next_scan_at = hours ? new Date(new Date(r.scanned_at).getTime() + hours * 3600_000).toISOString() : null
    return stripSample(r)
  },
  systemMap: async (ws: string): Promise<SystemMap> => {
    const { consumers: _c, ...rest } = demoSystem(ws)
    void _c
    return clone(rest)
  },
  repoImpact: async (rid: string) => clone(demoImpact(rid)),
  acceptReference: async (): Promise<Artifact> => { throw new Error('409: No pending update for this reference') },
  enrichApi: async (): Promise<RepoDetail> => { throw new Error('The AI description pass runs on the server with an API key') },
  mockCall: async (rid: string, method: string, path: string) => {
    await sleep(250)
    const r = db.repos.find(x => x.id === rid)
    const ep = r && matchEndpoint(r, method, path)
    if (!ep) return { status: 404, body: { detail: `No ${method} ${path} in the contract` } }
    const ok = Object.keys(ep.responses ?? {}).sort().find(c => c.startsWith('2')) ?? '200'
    return { status: Number(ok), body: ok === '204' ? null : ep.example_response ?? { ok: true } }
  },
  generateRepo: async (rid: string, items: string[]) => {
    await sleep(900)
    const r = db.repos.find(x => x.id === rid); if (!r) throw new Error('404: Repo not found')
    const sample = SAMPLES[r.sample]
    const failing = r.analysis.readiness.checks.filter(c => c.status === 'fail' && c.generate).map(c => c.generate!)
    for (const item of (items.length ? items : [...new Set(failing)])) {
      const draft = sample.drafts[item]
      if (!draft) continue
      const content = draft.content.replaceAll(sample.analysis.name, r.name)
      const data = { ...draft.data, repo: r.ref, generator: item, in_repo: false }
      const prev = r.generated[item] ? db.artifacts.find(a => a.id === r.generated[item]) : undefined
      if (prev) { prev.content = content; prev.data = data; prev.version += 1; prev.status = 'draft'; continue }
      const a = art(r.project_id, item === 'api_contract' ? 'api_contract' : 'doc', draft.title.replaceAll(sample.analysis.name, r.name), content, 'draft', 'agent:repo-onboarding')
      a.data = data
      r.generated[item] = a.id
    }
    for (const fact of sample.conventions) {
      const content = fact.replaceAll(sample.analysis.name, r.name)
      if (!db.memory.some(m => m.content === content)) {
        db.memory.push({ id: id(), ws: r.workspace_id, scope: 'project', kind: 'fact', content, status: 'proposed', author: 'agent:repo-onboarding' })
      }
    }
    return stripSample(r)
  },
  repoOpenapi: async (rid: string) => {
    const r = db.repos.find(x => x.id === rid)
    const y = r && SAMPLES[r.sample].openapi_yaml
    if (!y) throw new Error('404: No API endpoints found in this repo')
    return y
  },
  artifact: async (aid: string) => {
    const a = db.artifacts.find(x => x.id === aid); if (!a) throw new Error('404: Artifact not found')
    return clone(a)
  },
  mcpServers: async () => clone(db.mcp),
  addMcpServer: async (_ws: string, body: McpServerInput) => {
    await sleep(700)
    if (!/^[a-z0-9][a-z0-9_-]{0,39}$/.test(body.name)) throw new Error('422: name must be lowercase letters, digits, - or _')
    if (db.mcp.some(s => s.name === body.name && s.scope === (body.scope ?? 'workspace'))) throw new Error(`409: An MCP server named '${body.name}' already exists in this scope`)
    checkSecrets(body.env ?? {}, 'env'); checkSecrets(body.headers ?? {}, 'headers')
    const s: McpServer = { id: id(), description: '', scope: 'workspace', scope_ref: null, transport: 'stdio', command: null, args: [], url: null, env: {}, headers: {}, enabled: true, allowed_tools: [], write_tools: [], min_autonomy_for_writes: 'L2', timeout_s: 30, tools_cache: [], last_test_ok: null, last_test_error: null, last_tested_at: null, ...body } as McpServer
    mcpTest(s); db.mcp.push(s); return clone(s)
  },
  updateMcpServer: async (sid: string, body: Partial<McpServerInput>) => {
    const s = db.mcp.find(x => x.id === sid)!
    checkSecrets(body.env ?? {}, 'env'); checkSecrets(body.headers ?? {}, 'headers')
    Object.assign(s, Object.fromEntries(Object.entries(body).filter(([, v]) => v !== undefined))); return clone(s)
  },
  testMcpServer: async (sid: string) => { await sleep(600); const s = db.mcp.find(x => x.id === sid)!; mcpTest(s); return clone(s) },
  deleteMcpServer: async (sid: string) => { db.mcp = db.mcp.filter(x => x.id !== sid) },
  runners: async () => clone(db.runners),
  addRunner: async (_ws: string, body: Partial<Runner> & { name: string }) => {
    if (db.runners.some(r => r.name === body.name)) throw new Error(`409: A runner named '${body.name}' already exists`)
    const r: Runner = { id: id(), kind: 'worker', description: '', labels: [], config: {}, is_default: false, max_concurrency: 4, timeout_s: 900, last_seen_at: null, queued: 0, ...body }
    db.runners.push(r); return clone(r)
  },
  updateRunner: async (rid: string, body: Partial<Runner>) => {
    if (body.is_default) db.runners.forEach(r => { r.is_default = false })
    const r = db.runners.find(x => x.id === rid)!; Object.assign(r, body); return clone(r)
  },
  deleteRunner: async (rid: string) => {
    const r = db.runners.find(x => x.id === rid)
    db.runners = db.runners.filter(x => x.id !== rid)
    for (const s of Object.values(db.agentSettings)) if (s.runner === r?.name) delete s.runner
  },
  agentSettings: async () => Object.keys(AGENTS).map(agentSetting),
  setAgentSetting: async (_ws: string, agent: string, body: { runner?: string | null; model?: string | null; max_turns?: number | null; token_budget?: number | null; context_budget?: number | null }) => {
    db.agentSettings[agent] = Object.fromEntries(Object.entries({ ...body, runner: body.runner === 'inline' ? undefined : body.runner }).filter(([, v]) => v != null))
    return agentSetting(agent)
  },
  exportConfig: async () => {
    const lines = ['runners:']
    for (const r of db.runners) lines.push(`  - name: ${r.name}`, `    kind: ${r.kind}`, ...(r.is_default ? ['    default: true'] : []))
    lines.push('mcp_servers:')
    for (const s of db.mcp) {
      lines.push(`  - name: ${s.name}`, `    transport: ${s.transport}`, s.transport === 'http' ? `    url: ${s.url}` : `    command: ${s.command}`)
      if (s.args.length) lines.push(`    args: [${s.args.join(', ')}]`)
      for (const [label, values] of [['env', s.env], ['headers', s.headers]] as const) {
        if (Object.keys(values).length) lines.push(`    ${label}:`, ...Object.entries(values).map(([k, v]) => `      ${k}: "${v}"`))
      }
      if (s.allowed_tools.length) lines.push(`    allowed_tools: [${s.allowed_tools.join(', ')}]`)
      if (s.write_tools.length) lines.push(`    write_tools: [${s.write_tools.join(', ')}]`)
      lines.push(`    min_autonomy_for_writes: ${s.min_autonomy_for_writes}`, `    enabled: ${s.enabled}`)
    }
    lines.push('agents:')
    for (const [agent, s] of Object.entries(db.agentSettings)) {
      const entries = Object.entries(s).filter(([, v]) => v != null)
      if (entries.length) lines.push(`  ${agent}:`, ...entries.map(([k, v]) => `    ${k}: ${v}`))
    }
    return lines.join('\n') + '\n'
  },
  applyConfig: async () => {
    throw new Error('Preview: applying YAML runs on the real Filament server (fil config apply -f filament.yaml). Edit settings in the tables below to try it here.')
  },
}

seed()
