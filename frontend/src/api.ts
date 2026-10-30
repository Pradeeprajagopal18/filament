// Typed client for the Filament API (same endpoints the CLI uses).

export interface Workspace { id: string; name: string }
export interface Project { id: string; workspace_id: string; name: string; description: string }
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
export interface Endpoint {
  method: string; path: string; file: string | null; line: number | null; handler: string
  framework: string; summary: string; tag: string; params: { name: string; in: string }[]
}
export interface UiElement { type: string; label: string; repeated?: boolean; children?: UiElement[] }
export interface UiPage { path: string; component: string; file: string | null; router: string; elements: UiElement[]; mock_html: string }
export interface DepNode { id: string; label: string; category: string; evidence: Evidence[] }
export interface RepoAnalysis {
  repo: string; name: string
  profile: { kinds: { kind: string; confidence: number; evidence: string[] }[]; kind_names: string[]; frameworks: string[]; languages: Record<string, number>; packages: string[]; manifests: string[]; file_count: number }
  readiness: { score: number; grade: string; checks: Check[] }
  commands: Record<string, string>
  api: { source: 'spec' | 'inferred' | 'none'; specs: string[]; endpoints: Endpoint[]; openapi: Record<string, unknown> | null }
  ui: { pages: UiPage[]; components: string[] }
  components: { nodes: DepNode[]; edges: { source: string; target: string; label: string }[]; mermaid: string; summary: Record<string, number> }
}
export interface RepoSummary {
  id: string; workspace_id: string; project_id: string; ref: string; name: string; kinds: string[]
  score: number; grade: string; generated: Record<string, string>; scanned_at: string
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
export type McpServerInput = Partial<Omit<McpServer, 'id' | 'tools_cache' | 'last_test_ok' | 'last_test_error' | 'last_tested_at'>> & { name: string }
export interface Runner {
  id: string; name: string; kind: string; description: string; labels: string[]; config: Record<string, unknown>
  is_default: boolean; max_concurrency: number; timeout_s: number; last_seen_at: string | null; queued: number
}
export interface AgentSetting {
  agent: string; description: string; runner: string; model: string; max_turns: number
  token_budget: number; context_budget: number; overridden: string[]
}
export interface Health { status: string; offline: boolean; model: string; version: string }

async function callText(method: string, path: string, body?: string): Promise<string> {
  const res = await fetch(`/api${path}`, { method, headers: body ? { 'Content-Type': 'text/plain' } : undefined, body })
  if (!res.ok) {
    let detail = res.statusText
    try { detail = (await res.json()).detail ?? detail } catch { /* not JSON */ }
    throw new Error(`${res.status}: ${detail}`)
  }
  return res.text()
}

async function call<T>(method: string, path: string, body?: unknown): Promise<T> {
  const res = await fetch(`/api${path}`, {
    method,
    headers: body ? { 'Content-Type': 'application/json' } : undefined,
    body: body ? JSON.stringify(body) : undefined,
  })
  if (!res.ok) {
    let detail = res.statusText
    try { detail = (await res.json()).detail ?? detail } catch { /* not JSON */ }
    throw new Error(`${res.status}: ${typeof detail === 'string' ? detail : JSON.stringify(detail)}`)
  }
  return res.json() as Promise<T>
}

const httpApi = {
  health: () => call<Health>('GET', '/health'),
  workspaces: () => call<Workspace[]>('GET', '/workspaces'),
  createWorkspace: (name: string) => call<Workspace>('POST', '/workspaces', { name }),
  projects: (ws: string) => call<Project[]>('GET', `/workspaces/${ws}/projects`),
  createProject: (ws: string, name: string, description: string) =>
    call<Project>('POST', `/workspaces/${ws}/projects`, { name, description }),
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
    call<{ answer: string; citations: string[]; answered: boolean }>('POST', `/workspaces/${ws}/ask`, { question, asker: 'web' }),
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
