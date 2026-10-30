// In-browser demo backend: the same API surface as the FastAPI service, with sample data and
// simulated agents. Used only when the UI is built with VITE_DEMO=1 (the interactive preview).
import type {
  Artifact, Check, Gap, Health, Link, Memory, Project, Readiness, Run, TraceEvent, UsageRow, Workspace,
} from './api'

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
    m(['create_artifact'], 'Two org standards apply: tokenization and idempotency keys. No existing PRD.'), t('create_artifact', { type: 'prd', title: prd.title }),
    m(['ask_human'], 'CVV policy for saved cards is not defined anywhere.'), t('ask_human', { question: 'Do we require CVV for saved cards under 18 months old?' }),
    m(['create_artifact', 'create_artifact', 'create_artifact', 'create_artifact']), ...storyArts.map(s => t('create_artifact', { type: 'story', title: s.title })),
    m(['propose_memory']), t('propose_memory', { content: 'One-click checkout is limited to cards saved in the last 18 months.' }),
    m(['finish']), t('finish', { summary: 'PRD and 4 stories drafted; 1 question for the PM; 1 decision proposed.' }), { type: 'stop', reason: 'finished' },
  ]
  Object.assign(req, { turns: 6, input_tokens: 18240, output_tokens: 3920, output: { summary: 'PRD and 4 stories drafted; 1 question for the PM; 1 decision proposed.', created: [prd, ...storyArts].map(brief), questions: ['Do we require CVV for saved cards under 18 months old?'] } })
  fl.trace = [
    m(['search_graph']), t('search_graph', { types: ['prd', 'story'] }), m(['read_artifact', 'read_artifact']), t('read_artifact', { id: prd.id }), t('read_artifact', { id: storyArts[1].id }),
    m(['create_artifact', 'create_artifact'], 'Flow exposes a gap: vault outage during save.'), t('create_artifact', { type: 'flow', title: flow.title }), t('create_artifact', { type: 'flow', title: seqd.title }),
    m(['ask_human']), t('ask_human', { question: 'What should happen if the vault is down while saving a card?' }),
    m(['finish']), t('finish', { summary: 'User flow and sequence drawn; flagged one gap.' }), { type: 'stop', reason: 'finished' },
  ]
  Object.assign(fl, { turns: 5, input_tokens: 14310, output_tokens: 2680, output: { summary: 'User flow and sequence drawn; flagged one gap.', created: [flow, seqd].map(brief), questions: ['What should happen if the vault is down while saving a card?'] } })
  parent.trace = [
    m(['search_graph', 'search_memory'], 'Nothing exists yet for this idea. Plan: requirements, then flow.'), t('search_graph', { types: ['prd', 'story', 'flow'] }), t('search_memory', { query: 'saved cards checkout' }),
    m(['delegate']), t('delegate', { agent: 'requirements', goal: 'Draft PRD and stories' }),
    m(['read_artifact'], 'PRD looks complete; check story coverage of declines before drawing.'), t('read_artifact', { id: prd.id }),
    m(['delegate']), t('delegate', { agent: 'flow', goal: 'Draw the user flow and sequence' }),
    m(['finish']), t('finish', { summary: 'Idea taken to design: PRD, 4 stories, 2 diagrams. Waiting on 2 human answers and review.' }), { type: 'stop', reason: 'finished' },
  ]
  Object.assign(parent, { turns: 5, input_tokens: 9120, output_tokens: 1310, output: { summary: 'Idea taken to design: PRD, 4 stories, 2 diagrams. Waiting on 2 human answers and review.', created: [], questions: [], child_run_ids: [req.id, fl.id] } })

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
  }
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
  if (save) {
    const prd = art(p.id, 'prd', `PRD: ${first}`, `# PRD: ${first}\n\n## Problem\n${ideaText}\n\n## Goals\n- ${first}\n- Measurable improvement for the target users\n\n## Non-goals\n- Anything outside the idea as described\n\n## Users\n- Primary users of ${p.name} (to confirm)\n\n## Requirements\n- Core flow for: ${first.toLowerCase()}\n- Clear errors and recovery paths\n- Settings to manage the feature\n\n## Non-functional requirements\n${nfr}\n\n## Open questions\n- Which users or markets go first?\n- What success metric and target do we commit to?`, statusFor(autonomy), 'agent:requirements')
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
    m(['search_memory', 'search_graph']), t('search_memory', { query: first }), t('search_graph', { types: ['prd', 'story'] }),
    m(['create_artifact'], `${mem.length} memory entries apply; no existing PRD.`), t('create_artifact', { type: 'prd', title: `PRD: ${first}` }),
    m(['create_artifact', 'create_artifact', 'create_artifact']), ...['Core flow', 'Errors and recovery', 'Manage settings'].map(x => t('create_artifact', { type: 'story', title: x })),
    m(['ask_human'], 'No success metric is defined.'), t('ask_human', { question }),
    m(['finish']), t('finish', { summary: `PRD and 3 stories drafted using ${mem.length} memory entries.` }), { type: 'stop', reason: 'finished' },
  ]
  if (!save) r.trace.splice(4, 0, { type: 'tool', tool: 'note', input: 'L0: write tools saved nothing', result: {}, error: false })
  db.signals.push({ kind: 'question', text: question })
  Object.assign(r, {
    turns: 5, input_tokens: 9000 + Math.round(Math.random() * 6000), output_tokens: 2400 + Math.round(Math.random() * 1500),
    context_pack: { items: mem.map(e => ({ kind: 'memory', id: e.id, title: e.kind })), approx_tokens: 900 },
    output: { summary: save ? `PRD and 3 stories drafted using ${mem.length} memory entries.` : 'Suggested a PRD and 3 stories (L0: nothing saved).', created: created.map(brief), questions: [question], prd_id: created[0]?.id ?? null },
  })
  return r
}

function simulateFlow(p: Project, autonomy: string, parent: string | null) {
  const r = runRecord(p.id, 'flow', 'Draw the user flow and sequence', autonomy, parent)
  const stories = db.artifacts.filter(a => a.project_id === p.id && a.type === 'story' && a.status !== 'rejected').slice(-4)
  const prd = db.artifacts.filter(a => a.project_id === p.id && a.type === 'prd').slice(-1)[0]
  if (!stories.length) {
    Object.assign(r, { status: 'failed', error: 'No PRD or stories found to draw from' })
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
  Object.assign(r, { turns: 4, input_tokens: 7000 + Math.round(Math.random() * 4000), output_tokens: 1800 + Math.round(Math.random() * 900), output: { summary: `Drew a user flow and a sequence from ${stories.length} stories.`, created: created.map(brief), questions: [] } })
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
  Object.assign(parent, { turns: 4, input_tokens: 5200, output_tokens: 900, output: { summary: 'Idea taken to design: PRD, stories and two diagrams are waiting for your review.', created: [], questions: [], child_run_ids: [req.id, fl.id] } })
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
}

seed()
