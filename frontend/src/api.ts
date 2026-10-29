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
  type: 'model' | 'tool' | 'stop'
  text?: string; tool_calls?: string[]; tokens?: [number, number]
  tool?: string; input?: unknown; result?: unknown; error?: boolean
  reason?: string
}
export interface Run {
  id: string; parent_run_id: string | null; agent: string; goal: string; status: string; autonomy: string
  trace: TraceEvent[]; turns: number
  output: Record<string, unknown>; context_pack: { items: { kind: string; id: string; title: string }[]; approx_tokens: number }
  model: string | null; input_tokens: number; output_tokens: number; cost_usd: number | null
  error: string | null; started_at: string
}
export interface Memory {
  id: string; scope: string; kind: string; content: string; status: string; author: string
}
export interface UsageRow { key: string; runs: number; input_tokens: number; output_tokens: number; cost_usd: number | null }
export interface Gap { topic: string; count: number; examples: string[] }
export interface Check { id: string; title: string; passed: boolean; weight: number; detail: string; fix: string }
export interface Readiness { id: string; repo: string; score: number; grade: string; checks: Check[]; created_at: string }
export interface Health { status: string; offline: boolean; model: string; version: string }

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

export const api = {
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
}
