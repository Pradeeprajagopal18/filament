// Typed client for the Ignite API (same endpoints the CLI uses).

export interface Workspace { id: string; name: string }
export interface Project { id: string; workspace_id: string; name: string; description: string; restricted?: boolean; team_id?: string | null }
export interface Artifact {
  id: string; project_id: string; type: string; title: string; content: string
  data: Record<string, unknown>; status: string; version: number; author: string
  run_id: string | null; updated_at: string
}
export interface Link { id: string; source_id: string; target_id: string; kind: string }
export interface TraceEvent {
  type: 'model' | 'tool' | 'stop' | 'compact'
  text?: string; tool_calls?: string[]; tokens?: [number, number]; context_tokens?: number
  results?: number; before?: number; after?: number
  tool?: string; input?: unknown; result?: unknown; error?: boolean
  reason?: string
}
export interface Run {
  id: string; parent_run_id: string | null; agent: string; goal: string; status: string; autonomy: string
  trace: TraceEvent[]; turns: number
  config: { runner?: string; runner_kind?: string; model?: string; max_turns?: number; token_budget?: number; context_budget?: number }
  peak_context_tokens: number; claimed_by: string | null
  output: Record<string, unknown>; context_pack: { items: { kind: string; id: string; title: string }[]; approx_tokens: number }
  model: string | null; input_tokens: number; output_tokens: number; cost_usd: number | null
  error: string | null; started_at: string
}
export interface Memory {
  id: string; scope: string; kind: string; content: string; status: string; author: string
}
export interface UsageRow { key: string; runs: number; input_tokens: number; output_tokens: number; cost_usd: number | null }
export interface Gap { topic: string; count: number; examples: string[] }
export interface Check {
  id: string; title: string; passed: boolean; weight: number; detail: string; fix: string
  kind?: string; status?: 'pass' | 'fail' | 'skip'; generate?: string | null
}
export interface Readiness { id: string; repo: string; score: number; grade: string; checks: Check[]; created_at: string }
export interface Evidence { file: string; line: number | null; detail: string }
export type JsonSchema = Record<string, unknown>
export interface Endpoint {
  method: string; path: string; file: string | null; line: number | null; handler: string
  framework: string; summary: string; tag: string; description?: string; enriched?: string
  documented?: string; deprecated?: boolean
  params: { name: string; in: string; schema?: JsonSchema; required?: boolean; description?: string }[]
  request?: { content_type: string; schema: JsonSchema }
  responses?: Record<string, { description: string; schema?: JsonSchema }>
  auth?: string; example_request?: unknown; example_response?: unknown
}
export interface ClientCall { method: string; path: string; base: string | null; file: string; line: number }
export interface UiElement { type: string; label: string; repeated?: boolean; children?: UiElement[] }
export interface UiPage { path: string; component: string; file: string | null; router: string; elements: UiElement[]; mock_html: string }
export interface DepNode { id: string; label: string; category: string; evidence: Evidence[] }
export interface RepoAnalysis {
  repo: string; name: string
  profile: { kinds: { kind: string; confidence: number; evidence: string[] }[]; kind_names: string[]; frameworks: string[]; languages: Record<string, number>; packages: string[]; manifests: string[]; file_count: number }
  readiness: { score: number; grade: string; checks: Check[] }
  commands: Record<string, string>
  api: {
    source: 'spec' | 'live' | 'inferred' | 'none'; specs: string[]; endpoints: Endpoint[]; openapi: Record<string, unknown> | null
    schemas?: Record<string, JsonSchema>; auth?: string[]
    generators?: { generator: string; spec_path: string; evidence: string }[]
    documented?: number; undocumented?: string[]; spec_url?: string | null; live_error?: string | null
    drift?: { in_code_not_in_contract: DriftItem[]; in_contract_not_in_code: DriftItem[] } | null
    enrichment?: { model: string; applied: number; at: string }
  }
  client_calls?: ClientCall[]
  code?: CodeSummary
  data_model?: DataModel
  ui: { pages: UiPage[]; components: string[] }
  components: { nodes: DepNode[]; edges: { source: string; target: string; label: string }[]; mermaid: string; summary: Record<string, number> }
}
export interface ServiceUse { service: string; label: string; category: string; via: string; confidence: string; file: string; line: number }
export interface CodeSummary {
  engine: string | null; reason?: string; error?: string; engine_version?: string; warnings?: string[]; languages?: Record<string, number>
  nodes: number; edges: number; symbols: number; files: number; extracted_pct: number; inferred_pct: number
  communities: { id: number; name: string; size: number; files: number; cohesion: number; key_symbols: string[] }[]
  hotspots: { id: string; label: string; file: string; line: number; fan_in: number; fan_out: number }[]
  cycles: string[][]
  rationale: { id: string; tag: string; text: string; file: string; line: number; about: string | null; refs: string[] }[]
  endpoints: Record<string, { node: string; services: ServiceUse[] }>
  built_at?: string | null; commit?: string | null
}
export interface CodeSym { id: string; label: string; kind: string; file: string | null; line: number | null }
export interface CodeGraphView {
  mode: 'subsystems' | 'symbols'; community?: number; name?: string
  nodes: { id: string; label: string; size?: number; community?: number; kind?: string; file?: string; line?: number; callable?: boolean; inside?: boolean }[]
  edges: { source: string; target: string; weight?: number; relation?: string; confidence?: string }[]
}
export interface CodeEdgeRef { relation: string; direction: string; confidence: string; node: CodeSym; at: string | null }
export interface CodeExplain {
  symbol: CodeSym; calls: CodeEdgeRef[]; called_by: CodeEdgeRef[]; other: CodeEdgeRef[]; notes: string[]
  serves_endpoints: string[]; uses_services: ServiceUse[]
}
export type CodeLookup<T> = { ok: true; data: T } | { ok: false; ambiguous?: boolean; candidates?: CodeSym[]; suggestions?: CodeSym[]; detail: string }
export interface CodePath { from: CodeSym; to: CodeSym; found: boolean; confidence?: string; hops: { from: string; relation: string; to: string; confidence: string; forward: boolean; at: string }[] }
export interface ChangeImpact {
  files: string[]; changed: CodeSym[]; affected: (CodeSym & { distance: number })[]; endpoints: string[]
  consumers: { repo: string; endpoints: string[] }[]; communities: number[]; hotspots_touched: string[]
  risk: 'low' | 'medium' | 'high'; summary: string
}
export interface DataModel {
  entities: { name: string; source: string; file: string; line: number; model?: string; fields: { name: string; type: string; pk: boolean; nullable: boolean; fk: string | null }[] }[]
  relations: { from: string; to: string; field: string; kind: string; file: string }[]; mermaid: string
}
export interface DriftItem { method: string; path: string; file: string | null; line: number | null }
export interface RepoSummary {
  id: string; workspace_id: string; project_id: string; ref: string; name: string; kinds: string[]
  score: number; grade: string; generated: Record<string, string>; scanned_at: string
  scan_interval_hours?: number | null; next_scan_at?: string | null; last_error?: string | null; commit?: string | null
  spec_url?: string | null; private?: boolean | null
}
export interface Breaking { endpoint: string; change: string; consumers?: string[] }
export interface ScanChanges {
  first_scan?: boolean; summary: string; breaking: Breaking[]; counts: Record<string, number>
  endpoints_added?: string[]; endpoints_removed?: string[]; endpoints_changed?: { endpoint: string; changes: string[] }[]
  checks_fixed?: string[]; checks_regressed?: string[]; connections_added?: string[]; connections_removed?: string[]
  pages_added?: string[]; pages_removed?: string[]; contract_drift?: number; score_delta?: number
  references?: { refreshed?: string[]; pending_review?: string[]; unchanged?: string[] }
}
export interface RepoScan {
  id: string; repo_id: string; trigger: string; status: string; grade: string; score: number
  commit: string | null; changes: ScanChanges; error: string | null; scanned_at: string
}
export interface MapEdge { source: string; target: string; kind: string; evidence: Evidence[]; endpoints: string[] }
export interface UnresolvedCall { repo_id: string; repo: string; target_id: string; target: string; method: string; path: string; file: string; line: number }
export interface SystemMap {
  nodes: { id: string; label: string; kind: string; kinds?: string[]; grade?: string | null; endpoints?: number; pages?: number; shared_by?: number; focus?: boolean; restricted?: boolean }[]
  edges: MapEdge[]; mermaid: string; unresolved: UnresolvedCall[]
  focus?: { id: string; label: string; direction: MapDirection; depth: number; uses: string[]; used_by: string[] }
}
export type MapDirection = 'both' | 'dependencies' | 'dependents'
export interface MapFocus { focus: string; direction: MapDirection; depth: number }
export interface Impact {
  dependents: { repo_id: string; repo: string; kind: string; endpoints: string[]; evidence: Evidence[] }[]
  dependencies: { id: string; name: string; kind: string; endpoints: string[] }[]
  endpoint_consumers: Record<string, { repo_id: string; repo: string; file: string; line: number }[]>
  unresolved_calls: UnresolvedCall[]
}
export interface RepoDetail extends RepoSummary { analysis: RepoAnalysis }
export interface McpTool { name: string; description: string; input_schema?: unknown }
export interface McpServer {
  id: string; name: string; description: string; scope: string; scope_ref: string | null
  transport: 'stdio' | 'http'; command: string | null; args: string[]; url: string | null
  env: Record<string, string>; headers: Record<string, string>; enabled: boolean
  allowed_tools: string[]; write_tools: string[]; min_autonomy_for_writes: string; timeout_s: number
  tools_cache: McpTool[]; last_test_ok: boolean | null; last_test_error: string | null; last_tested_at: string | null
}
export interface GatewayServer {
  id: string; name: string; kind: 'native' | 'generated'; description: string; enabled: boolean; url: string | null
  transport: string; redact: boolean; source_repo_id: string | null; min_autonomy_for_writes: string
  last_synced_at: string | null; tools: number; enabled_tools: number
  counts: { new: number; active: number; changed: number; removed: number }
  matches: { name: string; description: string; enabled: boolean; write: boolean }[]
}
export interface GatewayTool {
  id: string; server_id: string; name: string; description: string; input_schema: Record<string, unknown>
  enabled: boolean; write: boolean; status: 'new' | 'active' | 'changed' | 'removed'; version: number
  history: { version: number; description: string; input_schema: Record<string, unknown>; at: string; by: string; rolled_back_to?: number }[]
  pending: { description: string; input_schema: Record<string, unknown>; source: string; at: string } | null
  allowed_agents: string[]; rate_limit_per_min: number | null; meta: Record<string, unknown>; updated_at: string
}
export interface GatewayMetrics {
  hours: number; calls: number; errors: number; denied: number; bytes_saved: number; redactions: number
  tools: { server: string; tool: string; calls: number; errors: number; denied: number; simulated: number; p50_ms: number; p95_ms: number; bytes_saved: number; redactions: number; actors: string[] }[]
}
export interface GatewayCall {
  server: string; tool: string; actor: string; status: string; latency_ms: number; bytes_raw: number
  bytes_returned: number; redactions: number; error: string | null; run_id: string | null; at: string
}
export interface InvokeResult { status: number; body: Record<string, unknown> }
export type McpServerInput = Partial<Omit<McpServer, 'id' | 'tools_cache' | 'last_test_ok' | 'last_test_error' | 'last_tested_at'>> & { name: string }
export interface Runner {
  id: string; name: string; kind: string; description: string; labels: string[]; config: Record<string, unknown>
  is_default: boolean; max_concurrency: number; timeout_s: number; last_seen_at: string | null; queued: number
}
export interface AgentSetting {
  agent: string; description: string; runner: string; model: string; max_turns: number
  token_budget: number; context_budget: number; overridden: string[]
}
// -- sign-in and access --------------------------------------------------------------------------
export interface AuthConfig { mode: 'none' | 'login'; methods: ('local' | 'ldap' | 'oidc')[]; password: boolean; sso_url: string | null }
export interface Me {
  mode: 'none' | 'login'; id: string | null; email: string; name: string; is_admin: boolean
  username: string | null; auth_source: 'local' | 'ldap' | 'oidc'; must_change_password: boolean
  github_login: string | null; via: string
  workspaces: { id: string; name: string; role: 'admin' | 'member'; features?: string[] }[]
  checkpoint_roles: { workspace_id: string; project_id: string | null; role: string }[]
}
export interface Person {
  id: string; email: string; name: string; username: string | null; auth_source: 'local' | 'ldap' | 'oidc'
  locked: boolean; must_change_password: boolean; temporary_password?: string; github_login: string | null; is_admin: boolean; active: boolean
  groups: string[]; last_login_at: string | null; workspace_role: 'admin' | 'member' | null
  teams: { team_id: string; role: string; source: string }[]; projects: { project_id: string; role: string }[]
}
export interface Team { id: string; workspace_id: string; name: string; sso_group?: string | null; sso_role?: string }
export interface ProjectAccess {
  project_id: string; restricted: boolean; team: { id: string; name: string; sso_group: string | null } | null
  members: { user_id: string; email: string; name: string; role: string }[]; my_role: string | null
  checkpoints: Record<string, { user_id: string; email: string; name: string }[]>
}
export interface AuthField { value: unknown; source: 'saved' | 'environment'; set?: boolean; resolves?: boolean }
export interface AuthSettings { fields: Record<string, AuthField>; redirect_uri: string; updated_by: string | null; updated_at: string | null; ldap_available: boolean }
export interface TestResult { ok: boolean; message: string }
export interface FeatureRow { key: string; name: string; description: string; mode: 'everyone' | 'some' | 'off'; teams: string[]; groups: string[]; users: string[]; people: number; updated_by: string | null }
export interface AskResult { answer: string; citations: string[]; answered: boolean; sources?: Record<string, { title: string; heading: string; url: string | null; source: string }> }
export type SourceKind = 'confluence' | 'web' | 'upload' | 'repo'
export interface SourceInput {
  name: string; kind: SourceKind; config: Record<string, unknown>; audience: 'everyone' | 'some'; teams: string[]; groups: string[]
  users: string[]; project_id: string | null; agents: string[]; sync_hours: number; enabled: boolean
}
export interface KnowledgeSource extends SourceInput {
  id: string; status: 'new' | 'syncing' | 'ok' | 'error'; last_sync_at: string | null; next_sync_at: string | null; last_error: string | null
  last_report: { added?: number; updated?: number; unchanged?: number; removed?: number; failed?: { title: string; error: string }[]; passages?: number }
  doc_count: number; chunk_count: number; hits: number; created_by: string
}
export interface KnowledgeDoc { id: string; title: string; url: string | null; passages: number; updated_at: string }
export interface Passage { id: string; source: string; source_id: string; title: string; heading: string; url: string | null; text: string; score: number; match: string }
export interface KnowledgeIndexInput { provider: 'none' | 'local' | 'endpoint'; model?: string; model_path?: string | null; endpoint_url?: string | null; api_key_ref?: string | null }
export interface KnowledgeIndexOut extends KnowledgeIndexInput { fastembed: boolean; updated_by?: string; reembedding?: boolean }
export interface KnowledgeOverview { sources: KnowledgeSource[]; index: KnowledgeIndexOut }
export interface CheckpointRoleRow { id: string; user_id: string; email: string; role: string; project_id: string | null }
export interface ApiTokenRow { id: string; name: string; prefix: string; created_at: string; expires_at: string | null; last_used_at: string | null }
export interface AuditRow { at: string; actor: string; action: string; resource: string; detail: Record<string, unknown> }
export const CHECKPOINT_ROLES = ['product_owner', 'tech_lead', 'engineer', 'release_manager', 'on_call'] as const
export const PROJECT_ROLES = ['viewer', 'contributor', 'maintainer', 'owner'] as const

/** Fired when the API says the session is missing or expired, so the shell shows sign-in. */
export const SIGNED_OUT = 'ignite:signed-out'
/** Fired when a temporary password (like the first admin / admin) has to be changed first. */
export const MUST_CHANGE = 'ignite:must-change-password'
function checkAuth(res: Response) {
  if (res.status === 401 && !res.url.endsWith('/auth/login') && !res.url.endsWith('/auth/password')) window.dispatchEvent(new Event(SIGNED_OUT))
  if (res.status === 403) res.clone().json().then(b => { if (b?.detail === 'password_change_required') window.dispatchEvent(new Event(MUST_CHANGE)) }).catch(() => {})
}

export interface Health { status: string; offline: boolean; model: string; version: string }

async function callText(method: string, path: string, body?: string): Promise<string> {
  const res = await fetch(`/api${path}`, { method, headers: body ? { 'Content-Type': 'text/plain' } : undefined, body })
  checkAuth(res)
  if (!res.ok) {
    let detail = res.statusText
    try { detail = (await res.json()).detail ?? detail } catch { /* not JSON */ }
    throw new Error(`${res.status}: ${detail}`)
  }
  return res.text()
}

async function lookup<T>(path: string): Promise<CodeLookup<T>> {
  const res = await fetch(`/api${path}`)
  const body = await res.json()
  checkAuth(res)
  if (res.ok) return { ok: true, data: body as T }
  return { ok: false, ambiguous: body.ambiguous, candidates: body.candidates, suggestions: body.suggestions, detail: body.detail ?? res.statusText }
}

async function call<T>(method: string, path: string, body?: unknown): Promise<T> {
  const res = await fetch(`/api${path}`, {
    method,
    headers: body ? { 'Content-Type': 'application/json' } : undefined,
    body: body ? JSON.stringify(body) : undefined,
  })
  checkAuth(res)
  if (!res.ok) {
    let detail = res.statusText
    try { detail = (await res.json()).detail ?? detail } catch { /* not JSON */ }
    throw new Error(`${res.status}: ${typeof detail === 'string' ? detail : JSON.stringify(detail)}`)
  }
  return (res.status === 204 ? undefined : res.json()) as Promise<T>
}

// -- delivery pipeline --------------------------------------------------------------------------
export interface StageDef { key: string; label: string; work: string; agent: string; agent_label: string; agent_does: string; role: string | null; role_label: string; checkpoint: string }
export interface StageState extends StageDef {
  state: string; detail: string
  artifacts: { id: string; type: string; title: string; status: string }[]
  pending_approvals: { id: string; artifact_id: string; role: string | null }[]
}
export interface WorkItem { id: string; title: string; status: string; url?: string | null; labels: string[]; readiness: number | null; follow_up_of: string | null; stage: string; stages: StageState[] }
export interface SloView { id: string; title: string; objective: number; service?: string | null; burn: Record<string, number | null>; state: string; severity: string | null; reason: string; samples: number }
export interface DeliveryBoard { stages: StageDef[]; items: WorkItem[]; slos: SloView[]; hint?: string }
export interface AdvanceBody { stage?: string | null; autonomy?: string; target?: string; pr_url?: string | null; post?: boolean; requested_by?: string }
export interface Handoff { id: string; title: string; branch: string | null; target: string | null; brief: string; prompt: string; commands: Record<string, string>; attempt: number }
export interface ReleaseOut { id: string; stage: string; steps: { percent: number; status: string }[] }

const httpApi = {
  health: () => call<Health>('GET', '/health'),
  workspaces: () => call<Workspace[]>('GET', '/workspaces'),
  createWorkspace: (name: string) => call<Workspace>('POST', '/workspaces', { name }),
  projects: (ws: string) => call<Project[]>('GET', `/workspaces/${ws}/projects`),
  createProject: (ws: string, name: string, description: string, restricted = false) =>
    call<Project>('POST', `/workspaces/${ws}/projects`, { name, description, restricted }),
  me: () => call<Me>('GET', '/auth/me'),
  authConfig: () => call<AuthConfig>('GET', '/auth/config'),
  passwordLogin: (username: string, password: string) => call<{ ok: boolean; must_change_password: boolean }>('POST', '/auth/login', { username, password }),
  changePassword: (current: string, next: string) => call<{ ok: boolean }>('POST', '/auth/password', { current, new: next }),
  resetPassword: (ws: string, id: string) => call<{ temporary_password: string; note: string }>('POST', `/workspaces/${ws}/people/${id}/reset-password`),
  createLocalAccount: (ws: string, username: string, name: string, email: string, workspace_role: string) =>
    call<Person>('POST', `/workspaces/${ws}/people`, { local_username: username, name, email, workspace_role }),
  logout: () => call<{ ok: boolean }>('POST', '/auth/logout'),
  tokens: () => call<ApiTokenRow[]>('GET', '/auth/tokens'),
  createToken: (name: string, days: number | null) => call<{ id: string; token: string; note: string }>('POST', '/auth/tokens', { name, days }),
  revokeToken: (id: string) => call<void>('DELETE', `/auth/tokens/${id}`),
  people: (ws: string) => call<Person[]>('GET', `/workspaces/${ws}/people`),
  invite: (ws: string, email: string, workspace_role: string, github_login: string | null) =>
    call<Person>('POST', `/workspaces/${ws}/people`, { email, workspace_role, github_login }),
  updatePerson: (ws: string, id: string, body: { workspace_role?: string; github_login?: string; active?: boolean; teams?: { team_id: string; role: string }[] }) =>
    call<Person>('PATCH', `/workspaces/${ws}/people/${id}`, body),
  teams: (ws: string) => call<Team[]>('GET', `/workspaces/${ws}/teams`),
  createTeam: (ws: string, name: string) => call<Team>('POST', `/workspaces/${ws}/teams`, { name }),
  setTeamGroup: (ws: string, team: string, sso_group: string, sso_role: string) =>
    call<Team>('PUT', `/workspaces/${ws}/teams/${team}`, { sso_group, sso_role }),
  projectAccess: (p: string) => call<ProjectAccess>('GET', `/projects/${p}/access`),
  setRestricted: (p: string, restricted: boolean) => call<ProjectAccess>('PATCH', `/projects/${p}/access`, { restricted }),
  setMembers: (p: string, members: { user_id: string; role: string }[]) => call<ProjectAccess>('PUT', `/projects/${p}/members`, members),
  checkpointRoles: (ws: string) => call<CheckpointRoleRow[]>('GET', `/workspaces/${ws}/checkpoint-roles`),
  addCheckpointRole: (ws: string, user_id: string, role: string, project_id: string | null) =>
    call<CheckpointRoleRow>('PUT', `/workspaces/${ws}/checkpoint-roles`, { user_id, role, project_id }),
  removeCheckpointRole: (ws: string, id: string) => call<void>('DELETE', `/workspaces/${ws}/checkpoint-roles/${id}`),
  authSettings: () => call<AuthSettings>('GET', '/auth/settings'),
  saveAuthSettings: (changes: Record<string, unknown>) => call<AuthSettings>('PUT', '/auth/settings', changes),
  testLdap: (settings: Record<string, unknown>, username?: string, password?: string) => call<TestResult>('POST', '/auth/settings/test-ldap', { settings, username, password }),
  testOidc: (settings: Record<string, unknown>) => call<TestResult>('POST', '/auth/settings/test-oidc', { settings }),
  features: (ws: string) => call<FeatureRow[]>('GET', `/workspaces/${ws}/features`),
  setFeature: (ws: string, key: string, body: { mode: string; teams: string[]; groups: string[]; users: string[] }) => call<FeatureRow>('PUT', `/workspaces/${ws}/features/${key}`, body),
  audit: (ws: string) => call<AuditRow[]>('GET', `/workspaces/${ws}/audit?limit=100`),
  artifacts: (p: string) => call<Artifact[]>('GET', `/projects/${p}/artifacts`),
  graph: (p: string) => call<{ nodes: Artifact[]; edges: Link[] }>('GET', `/projects/${p}/graph`),
  impact: (a: string) => call<{ affected: Artifact[] }>('GET', `/artifacts/${a}/impact`),
  decide: (a: string, decision: 'approved' | 'rejected') =>
    call<Artifact>('POST', `/artifacts/${a}/approve`, { decision, decided_by: 'web' }),
  runs: (p: string) => call<Run[]>('GET', `/projects/${p}/runs`),
  startRun: (p: string, agent: string, goal: string, input: Record<string, unknown>, autonomy: string) =>
    call<Run>('POST', `/projects/${p}/runs`, { agent, goal, input, autonomy, requested_by: 'web' }),
  memory: (ws: string) => call<Memory[]>('GET', `/workspaces/${ws}/memory`),
  addMemory: (ws: string, scope: string, kind: string, content: string) =>
    call<Memory>('POST', `/workspaces/${ws}/memory`, { scope, kind, content }),
  memoryAction: (id: string, action: 'confirm' | 'retire') => call<Memory>('POST', `/memory/${id}/${action}`),
  ask: (ws: string, question: string) =>
    call<AskResult>('POST', `/workspaces/${ws}/ask`, { question, asker: 'web' }),
  knowledge: (ws: string) => call<KnowledgeOverview>('GET', `/workspaces/${ws}/knowledge`),
  addSource: (ws: string, body: SourceInput) => call<KnowledgeSource>('POST', `/workspaces/${ws}/knowledge/sources`, body),
  source: (id: string) => call<KnowledgeSource & { docs: KnowledgeDoc[] }>('GET', `/knowledge/${id}`),
  updateSource: (id: string, body: Partial<SourceInput>) => call<KnowledgeSource>('PATCH', `/knowledge/${id}`, body),
  deleteSource: (id: string) => call<void>('DELETE', `/knowledge/${id}`),
  syncSource: (id: string, full = false) => call<{ status: string }>('POST', `/knowledge/${id}/sync?full=${full}`),
  testSource: (id: string) => call<{ ok: boolean; message: string; sample: { title: string; url: string | null }[] }>('POST', `/knowledge/${id}/test`),
  uploadFiles: async (id: string, files: File[]) => {
    const form = new FormData()
    for (const f of files) form.append('files', f)
    const res = await fetch(`/api/knowledge/${id}/files`, { method: 'POST', body: form, credentials: 'same-origin' })
    if (!res.ok) throw new Error(`${res.status}: ${(await res.json().catch(() => ({}))).detail ?? res.statusText}`)
    return res.json() as Promise<{ results: { file: string; title?: string; passages?: number; error?: string }[]; source: KnowledgeSource }>
  },
  deleteDoc: (source: string, doc: string) => call<void>('DELETE', `/knowledge/${source}/docs/${doc}`),
  searchKnowledge: (ws: string, query: string, source_ids?: string[]) => call<{ results: Passage[] }>('POST', `/workspaces/${ws}/knowledge/search`, { query, limit: 8, source_ids }),
  setKnowledgeIndex: (ws: string, body: KnowledgeIndexInput) => call<KnowledgeIndexOut>('PUT', `/workspaces/${ws}/knowledge/index`, body),
  testKnowledgeIndex: (ws: string, body: KnowledgeIndexInput) => call<TestResult>('POST', `/workspaces/${ws}/knowledge/index/test`, body),
  usage: (ws: string, by: string) => call<UsageRow[]>('GET', `/workspaces/${ws}/usage?by=${by}`),
  gaps: (ws: string) => call<Gap[]>('GET', `/workspaces/${ws}/gaps`),
  scan: (repo: string) => call<Readiness>('POST', '/readiness/scan', { repo }),
  readiness: () => call<Readiness[]>('GET', '/readiness'),
  repos: (ws: string) => call<RepoSummary[]>('GET', `/workspaces/${ws}/repos`),
  onboardRepo: (ws: string, repo: string) => call<RepoDetail>('POST', `/workspaces/${ws}/repos`, { repo }),
  repo: (id: string) => call<RepoDetail>('GET', `/repos/${id}`),
  rescanRepo: (id: string) => call<RepoDetail>('POST', `/repos/${id}/rescan`),
  generateRepo: (id: string, items: string[]) => call<RepoDetail>('POST', `/repos/${id}/generate`, { items }),
  repoOpenapi: (id: string) => callText('GET', `/repos/${id}/openapi`),
  artifact: (id: string) => call<Artifact>('GET', `/artifacts/${id}`),
  delivery: (p: string) => call<DeliveryBoard>('GET', `/projects/${p}/delivery`),
  addIssue: (p: string, title: string, body: string) => call<WorkItem>('POST', `/projects/${p}/issues`, { title, body }),
  importIssues: (p: string, repo: string) => call<{ imported: { id: string; title: string }[] }>('POST', `/projects/${p}/issues/import`, { repo }),
  advance: (item: string, body: AdvanceBody) => call<{ run: Run; item: WorkItem }>('POST', `/work-items/${item}/advance`, { requested_by: 'web', ...body }),
  handoff: (task: string) => call<Handoff>('GET', `/tasks/${task}/handoff`),
  linkPr: (task: string, url: string) => call<{ id: string; item: WorkItem | null }>('POST', `/tasks/${task}/pull-request`, { url }),
  addSlo: (p: string, title: string, objective: number) => call<{ id: string }>('POST', `/projects/${p}/slos`, { title, objective }),
  sloCheck: (p: string) => call<{ slos: SloView[]; actions: unknown[] }>('POST', `/projects/${p}/slo-check`),
  releaseAction: (id: string, action: 'promote' | 'rollback', reason = '') => call<ReleaseOut>('POST', `/releases/${id}/${action}`, { by: 'web', reason }),
  resolveIncident: (id: string, note: string) => call<{ id: string; state: string }>('POST', `/incidents/${id}/resolve`, { by: 'web', reason: note }),
  systemMap: (ws: string, f?: MapFocus) => call<SystemMap>('GET', `/workspaces/${ws}/system-map${f ? `?focus=${encodeURIComponent(f.focus)}&direction=${f.direction}&depth=${f.depth}` : ''}`),
  setRepoSpecUrl: (id: string, url: string) => call<RepoDetail>('PATCH', `/repos/${id}`, { spec_url: url }),
  repoImpact: (id: string) => call<Impact>('GET', `/repos/${id}/impact`),
  repoScans: (id: string) => call<RepoScan[]>('GET', `/repos/${id}/scans`),
  setRepoSchedule: (id: string, hours: number) => call<RepoDetail>('PATCH', `/repos/${id}`, { scan_interval_hours: hours }),
  acceptReference: (id: string, item: string) => call<Artifact>('POST', `/repos/${id}/references/${item}/accept`),
  enrichApi: (id: string) => call<RepoDetail>('POST', `/repos/${id}/enrich-api`),
  mockCall: async (id: string, method: string, path: string, body?: unknown) => {
    const res = await fetch(`/api/repos/${id}/mock${path}`, {
      method, headers: body ? { 'Content-Type': 'application/json' } : undefined, body: body ? JSON.stringify(body) : undefined,
    })
    let data: unknown = null
    try { data = await res.json() } catch { /* empty body */ }
    return { status: res.status, body: data }
  },
  repoCodeGraph: (id: string, community?: number) => call<CodeGraphView>('GET', `/repos/${id}/code/graph${community != null ? `?community=${community}` : ''}`),
  codeExplain: (id: string, name: string) => lookup<CodeExplain>(`/repos/${id}/code/explain?name=${encodeURIComponent(name)}`),
  codePath: (id: string, a: string, b: string) => lookup<CodePath>(`/repos/${id}/code/path?a=${encodeURIComponent(a)}&b=${encodeURIComponent(b)}`),
  codeChanges: (id: string, body: { files?: string[]; diff?: string; pr?: string }) => call<ChangeImpact>('POST', `/repos/${id}/code/changes`, body),
  gatewayRegistry: (ws: string, q = '') => call<GatewayServer[]>('GET', `/workspaces/${ws}/mcp/registry?q=${encodeURIComponent(q)}`),
  gatewayTools: (serverId: string) => call<GatewayTool[]>('GET', `/mcp/${serverId}/tools`),
  patchGatewayTool: (id: string, body: Partial<Pick<GatewayTool, 'enabled' | 'write' | 'allowed_agents' | 'rate_limit_per_min'>> & { description?: string }) =>
    call<GatewayTool>('PATCH', `/mcp-tools/${id}`, body),
  approveGatewayTool: (id: string) => call<GatewayTool>('POST', `/mcp-tools/${id}/approve`),
  rejectGatewayTool: (id: string) => call<GatewayTool>('POST', `/mcp-tools/${id}/reject`),
  rollbackGatewayTool: (id: string, version: number) => call<GatewayTool>('POST', `/mcp-tools/${id}/rollback`, { version }),
  syncGatewayServer: (id: string) => call<Record<string, unknown>>('POST', `/mcp/${id}/sync`),
  setGatewayServer: (id: string, body: { url?: string | null; redact?: boolean; enabled?: boolean }) => call<McpServer>('PATCH', `/mcp/${id}`, body),
  gatewayInvoke: async (ws: string, body: { server: string; tool: string; arguments: Record<string, unknown>; fields?: string[] | null; autonomy?: string }): Promise<InvokeResult> => {
    const res = await fetch(`/api/workspaces/${ws}/mcp/invoke`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ ...body, actor: 'web' }) })
    return { status: res.status, body: await res.json() }
  },
  gatewayMetrics: (ws: string) => call<GatewayMetrics>('GET', `/workspaces/${ws}/mcp/metrics`),
  gatewayCalls: (ws: string) => call<GatewayCall[]>('GET', `/workspaces/${ws}/mcp/calls?limit=40`),
  mcpServers: (ws: string) => call<McpServer[]>('GET', `/workspaces/${ws}/mcp`),
  addMcpServer: (ws: string, body: McpServerInput) => call<McpServer>('POST', `/workspaces/${ws}/mcp?test=true`, body),
  updateMcpServer: (id: string, body: Partial<McpServerInput>) => call<McpServer>('PATCH', `/mcp/${id}`, body),
  testMcpServer: (id: string) => call<McpServer>('POST', `/mcp/${id}/test`),
  deleteMcpServer: async (id: string) => { await fetch(`/api/mcp/${id}`, { method: 'DELETE' }) },
  runners: (ws: string) => call<Runner[]>('GET', `/workspaces/${ws}/runners`),
  addRunner: (ws: string, body: Partial<Runner> & { name: string }) => call<Runner>('POST', `/workspaces/${ws}/runners`, body),
  updateRunner: (id: string, body: Partial<Runner>) => call<Runner>('PATCH', `/runners/${id}`, body),
  deleteRunner: async (id: string) => { await fetch(`/api/runners/${id}`, { method: 'DELETE' }) },
  agentSettings: (ws: string) => call<AgentSetting[]>('GET', `/workspaces/${ws}/agent-settings`),
  setAgentSetting: (ws: string, agent: string, body: { runner?: string | null; model?: string | null; max_turns?: number | null; token_budget?: number | null; context_budget?: number | null }) =>
    call<AgentSetting>('PUT', `/workspaces/${ws}/agent-settings/${agent}`, body),
  exportConfig: (ws: string) => callText('GET', `/workspaces/${ws}/config`),
  applyConfig: (ws: string, yaml: string) => callText('POST', `/workspaces/${ws}/config`, yaml),
}

// The interactive preview (VITE_DEMO=1) swaps in an in-browser backend with sample data.
import { demoApi } from './demo'
export const DEMO = import.meta.env.VITE_DEMO === '1'
export const api: typeof httpApi = DEMO ? demoApi : httpApi
