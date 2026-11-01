import { useCallback, useEffect, useState } from 'react'
import { api, type Artifact, type Gap, type Memory, type Project, type Run, type UsageRow } from './api'
import { Badge, Content, ErrorLine, Tokens } from './components'

const TYPE_ORDER = ['idea', 'opportunity', 'feature_request', 'prd', 'story', 'flow', 'component_view', 'ui_mock', 'adr', 'api_contract', 'doc', 'task', 'test_plan', 'release', 'incident']

// -- Project: run agents, browse the graph, approve at the human gate --------------
export function ProjectPage({ project }: { project: Project }) {
  const [artifacts, setArtifacts] = useState<Artifact[]>([])
  const [runs, setRuns] = useState<Run[]>([])
  const [selected, setSelected] = useState<Artifact | null>(null)
  const [openRun, setOpenRun] = useState<Run | null>(null)
  const [impact, setImpact] = useState<Artifact[]>([])
  const [idea, setIdea] = useState('')
  const [autonomy, setAutonomy] = useState('L2')
  const [busy, setBusy] = useState('')
  const [error, setError] = useState('')

  const load = useCallback(async () => {
    const [a, r] = await Promise.all([api.artifacts(project.id), api.runs(project.id)])
    setArtifacts(a)
    setRuns(r)
  }, [project.id])

  useEffect(() => { load().catch(e => setError(String(e))) }, [load])
  useEffect(() => {
    if (selected) api.impact(selected.id).then(r => setImpact(r.affected)).catch(() => setImpact([]))
  }, [selected])

  const GOALS: Record<string, string> = {
    orchestrator: 'Take this idea to a reviewed design',
    requirements: 'Draft the PRD and stories',
    flow: 'Draw the user flow and sequence',
  }

  async function run(agent: string) {
    setBusy(agent); setError('')
    try {
      const input = agent === 'flow' ? {} : { idea }
      const r = await api.startRun(project.id, agent, GOALS[agent], input, autonomy)
      if (r.status !== 'succeeded') setError(r.error ?? `Run ${r.status}`)
      if (agent !== 'flow' && r.status === 'succeeded') setIdea('')
      setOpenRun(r)
      await load()
    } catch (e) { setError(String(e)) } finally { setBusy('') }
  }

  async function decide(decision: 'approved' | 'rejected') {
    if (!selected) return
    const updated = await api.decide(selected.id, decision)
    setSelected(updated)
    await load()
  }

  // Known types in a sensible order, then anything else, so no artifact is ever hidden.
  const types = [...TYPE_ORDER, ...new Set(artifacts.map(a => a.type).filter(t => !TYPE_ORDER.includes(t)))]
  const grouped = types.map(t => [t, artifacts.filter(a => a.type === t)] as const).filter(([, list]) => list.length)
  const topRuns = runs.filter(r => !r.parent_run_id)
  const childrenOf = (id: string) => runs.filter(r => r.parent_run_id === id).reverse()

  return (
    <div className="page">
      <header className="page-head">
        <div>
          <h1>{project.name}</h1>
          {project.description && <p className="muted">{project.description}</p>}
        </div>
      </header>

      <section className="card">
        <h2>Start from an idea</h2>
        <textarea rows={3} value={idea} onChange={e => setIdea(e.target.value)}
          placeholder="Describe the idea, e.g. 'Let shoppers save a card for one-click checkout'" />
        <div className="row">
          <button disabled={!idea.trim() || !!busy} onClick={() => run('orchestrator')}>
            {busy === 'orchestrator' ? 'Agents working…' : 'Take idea to design'}
          </button>
          <span className="muted small">or run one agent:</span>
          <button className="secondary" disabled={!idea.trim() || !!busy} onClick={() => run('requirements')}>
            {busy === 'requirements' ? 'Writing…' : 'Requirements'}
          </button>
          <button className="secondary" disabled={!!busy} onClick={() => run('flow')}>
            {busy === 'flow' ? 'Drawing…' : 'Flow'}
          </button>
          <label className="push">Autonomy
            <select value={autonomy} onChange={e => setAutonomy(e.target.value)}>
              <option value="L0">L0 · suggest only</option>
              <option value="L1">L1 · save drafts</option>
              <option value="L2">L2 · send to review</option>
              <option value="L3">L3 · act and report</option>
            </select>
          </label>
        </div>
        <p className="muted small"><b>Take idea to design</b> writes the PRD and stories, then draws the user flow and sequence diagram. <b>Requirements</b> writes only the PRD and stories. <b>Flow</b> draws diagrams from the stories already here{artifacts.some(a => a.type === 'story') ? '' : ' (there are none yet, so add an idea first)'}.</p>
        <p className="muted small">The orchestrator decides which specialists to call and in what order, and reviews their work. Approval gates, budgets and autonomy limits are enforced by the platform.</p>
        <ErrorLine error={error} />
      </section>

      <div className="split">
        <section className="card list">
          <h2>Project graph <span className="muted">{artifacts.length} artifacts</span></h2>
          {grouped.length === 0 && <p className="muted">Nothing yet. Start from an idea above.</p>}
          {grouped.map(([type, list]) => (
            <div key={type}>
              <h3>{type.replace('_', ' ')}</h3>
              {list.map(a => (
                <button key={a.id} className={`item ${selected?.id === a.id ? 'active' : ''}`} onClick={() => setSelected(a)}>
                  <span className="item-title">{a.title}</span>
                  <Badge value={a.status} />
                </button>
              ))}
            </div>
          ))}
        </section>

        <section className="card detail">
          {!selected ? <p className="muted">Select an artifact to read, approve or see its impact.</p> : (
            <>
              <div className="row spread">
                <div>
                  <h2>{selected.title}</h2>
                  <p className="muted">{selected.type} · v{selected.version} · by {selected.author}</p>
                </div>
                <Badge value={selected.status} />
              </div>
              {['draft', 'in_review'].includes(selected.status) && (
                <div className="row">
                  <button onClick={() => decide('approved')}>Approve</button>
                  <button className="secondary" onClick={() => decide('rejected')}>Reject</button>
                </div>
              )}
              <Content text={selected.content} />
              {impact.length > 0 && (
                <div className="impact">
                  <h3>Changing this affects</h3>
                  {impact.map(a => <div key={a.id} className="muted">{a.type} · {a.title}</div>)}
                </div>
              )}
            </>
          )}
        </section>
      </div>

      <div className="split">
        <section className="card list">
          <h2>Agent runs</h2>
          {topRuns.length === 0 && <p className="muted">No runs yet.</p>}
          {topRuns.map(r => (
            <div key={r.id}>
              <RunRow run={r} active={openRun?.id === r.id} onClick={() => setOpenRun(r)} />
              {childrenOf(r.id).map(c => (
                <RunRow key={c.id} run={c} child active={openRun?.id === c.id} onClick={() => setOpenRun(c)} />
              ))}
            </div>
          ))}
        </section>
        <section className="card detail">
          {openRun ? <RunLog run={openRun} /> : <p className="muted">Select a run to see every model turn and tool call.</p>}
        </section>
      </div>
    </div>
  )
}

function RunRow({ run, child, active, onClick }: { run: Run; child?: boolean; active: boolean; onClick: () => void }) {
  return (
    <button className={`item ${active ? 'active' : ''} ${child ? 'child' : ''}`} onClick={onClick}>
      <span className="item-title">{child ? '↳ ' : ''}{run.agent} <span className="muted small">· {run.turns} turns · <Tokens n={run.input_tokens + run.output_tokens} /> tokens</span></span>
      <Badge value={run.status} />
    </button>
  )
}

export function RunLog({ run }: { run: Run }) {
  const summary = run.output?.summary as string | undefined
  const created = (run.output?.created as { id: string; type: string; title: string }[] | undefined) ?? []
  const questions = (run.output?.questions as string[] | undefined) ?? []
  return (
    <div>
      <div className="row spread">
        <div>
          <h2>{run.agent} run</h2>
          <p className="muted">{run.goal} · {run.autonomy} · {run.model ?? '—'} · runner {run.config?.runner ?? 'inline'}{run.claimed_by ? ` (${run.claimed_by})` : ''}</p>
        </div>
        <Badge value={run.status} />
      </div>
      {summary && <p className="summary">{summary}</p>}
      {run.error && <p className="error">{run.error}</p>}
      {run.status === 'queued' && <p className="muted">Waiting for a worker on runner <code>{run.config?.runner}</code>. Start one with <code>fil runner start {run.config?.runner}</code>.</p>}
      <div className="stats">
        <div><b>{run.turns}</b><span>turns</span></div>
        <div><b><Tokens n={run.input_tokens} /></b><span>input tokens</span></div>
        <div><b><Tokens n={run.output_tokens} /></b><span>output tokens</span></div>
        <div><b><Tokens n={run.peak_context_tokens ?? 0} /></b><span>peak context</span></div>
        <div><b>{run.cost_usd == null ? '—' : `$${run.cost_usd.toFixed(4)}`}</b><span>cost</span></div>
      </div>
      <h3>Run log</h3>
      <ol className="trace">
        {run.trace.map((e, i) => (
          <li key={i} className={`trace-${e.type} ${e.error ? 'trace-error' : ''}`}>
            {e.type === 'model' && <><span className="tag">model</span> decided: {e.tool_calls?.length ? e.tool_calls.join(', ') : 'answer and stop'}{e.text ? <em> — {e.text.slice(0, 160)}</em> : null}</>}
            {e.type === 'tool' && <><span className="tag">{e.error ? 'error' : 'tool'}</span> <code>{e.tool}</code> <span className="muted small">{brief(e.input)}</span>{e.error ? <div className="error small">{brief(e.result)}</div> : null}</>}
            {e.type === 'stop' && <><span className="tag">stop</span> {e.reason}</>}
            {e.type === 'compact' && <><span className="tag">compact</span> {e.results} old results compacted · {e.before?.toLocaleString()} → {e.after?.toLocaleString()} tokens</>}
          </li>
        ))}
      </ol>
      {created.length > 0 && (<><h3>Created</h3>{created.map(a => <div key={a.id} className="muted">{a.type} · {a.title}</div>)}</>)}
      {questions.length > 0 && (<><h3>Questions for a human</h3>{questions.map((q, i) => <div key={i} className="muted">{q}</div>)}</>)}
    </div>
  )
}

function brief(v: unknown): string {
  const s = typeof v === 'string' ? v : JSON.stringify(v)
  return s && s.length > 140 ? s.slice(0, 140) + '…' : s ?? ''
}

// -- Memory -------------------------------------------------------------------------
export function MemoryPage({ ws }: { ws: string }) {
  const [entries, setEntries] = useState<Memory[]>([])
  const [content, setContent] = useState('')
  const [scope, setScope] = useState('org')
  const [kind, setKind] = useState('standard')
  const load = useCallback(() => api.memory(ws).then(setEntries), [ws])
  useEffect(() => { load() }, [load])

  async function add() {
    await api.addMemory(ws, scope, kind, content)
    setContent('')
    load()
  }
  async function act(id: string, action: 'confirm' | 'retire') { await api.memoryAction(id, action); load() }

  return (
    <div className="page">
      <h1>Shared memory</h1>
      <p className="muted">Standards, decisions and facts every agent and person works from. Agent-written entries stay proposed until confirmed.</p>
      <section className="card">
        <div className="row">
          <select value={scope} onChange={e => setScope(e.target.value)}>
            <option value="org">Organization</option><option value="team">Team</option><option value="project">Project</option>
          </select>
          <select value={kind} onChange={e => setKind(e.target.value)}>
            <option value="standard">Standard</option><option value="decision">Decision</option>
            <option value="fact">Fact</option><option value="procedure">Procedure</option>
          </select>
          <input value={content} onChange={e => setContent(e.target.value)} placeholder="e.g. All public APIs have OpenAPI specs" />
          <button disabled={!content.trim()} onClick={add}>Add</button>
        </div>
      </section>
      <section className="card">
        <table>
          <thead><tr><th>Entry</th><th>Scope</th><th>Kind</th><th>Status</th><th>Author</th><th /></tr></thead>
          <tbody>
            {entries.map(m => (
              <tr key={m.id}>
                <td>{m.content}</td><td>{m.scope}</td><td>{m.kind}</td><td><Badge value={m.status} /></td><td>{m.author}</td>
                <td className="actions">
                  {m.status === 'proposed' && <button className="small" onClick={() => act(m.id, 'confirm')}>Confirm</button>}
                  {m.status !== 'retired' && <button className="small secondary" onClick={() => act(m.id, 'retire')}>Retire</button>}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </section>
    </div>
  )
}

// -- Ask Filament ----------------------------------------------------------------------
export function AskPage({ ws }: { ws: string }) {
  const [q, setQ] = useState('')
  const [history, setHistory] = useState<{ q: string; a: string; c: string[]; ok: boolean }[]>([])
  async function ask() {
    const r = await api.ask(ws, q)
    setHistory(h => [{ q, a: r.answer, c: r.citations, ok: r.answered }, ...h])
    setQ('')
  }
  return (
    <div className="page">
      <h1>Ask Filament</h1>
      <p className="muted">Answers come from shared memory and the project graph, with sources. Questions nobody can answer become gaps.</p>
      <section className="card row">
        <input value={q} onChange={e => setQ(e.target.value)} onKeyDown={e => e.key === 'Enter' && q.trim() && ask()}
          placeholder="e.g. Do checkout payments use idempotency keys?" />
        <button disabled={!q.trim()} onClick={ask}>Ask</button>
      </section>
      {history.map((h, i) => (
        <section key={i} className="card">
          <p><strong>{h.q}</strong></p>
          <p className={h.ok ? '' : 'muted'}>{h.a}</p>
          {h.c.length > 0 && <p className="muted small">Sources: {h.c.join(', ')}</p>}
        </section>
      ))}
    </div>
  )
}

// -- Usage ----------------------------------------------------------------------------
export function UsagePage({ ws }: { ws: string }) {
  const [by, setBy] = useState('agent')
  const [rows, setRows] = useState<UsageRow[]>([])
  useEffect(() => { api.usage(ws, by).then(setRows) }, [ws, by])
  const total = rows.reduce((s, r) => s + r.input_tokens + r.output_tokens, 0)
  return (
    <div className="page">
      <h1>Token usage</h1>
      <div className="row">
        {['agent', 'project', 'model', 'person', 'status'].map(k => (
          <button key={k} className={by === k ? '' : 'secondary'} onClick={() => setBy(k)}>By {k}</button>
        ))}
      </div>
      <section className="card">
        <table>
          <thead><tr><th>{by}</th><th>Runs</th><th>Input</th><th>Output</th><th>Share</th><th>Cost (USD)</th></tr></thead>
          <tbody>
            {rows.map(r => {
              const share = total ? (100 * (r.input_tokens + r.output_tokens)) / total : 0
              return (
                <tr key={r.key}>
                  <td>{r.key}</td><td>{r.runs}</td><td><Tokens n={r.input_tokens} /></td><td><Tokens n={r.output_tokens} /></td>
                  <td><div className="bar"><div style={{ width: `${share}%` }} /></div></td>
                  <td>{r.cost_usd == null ? '—' : r.cost_usd.toFixed(4)}</td>
                </tr>
              )
            })}
          </tbody>
        </table>
        {rows.some(r => r.cost_usd == null) && <p className="muted small">Cost shows once model pricing is set in FILAMENT_PRICING.</p>}
      </section>
    </div>
  )
}

// -- Gaps -------------------------------------------------------------------------------
export function GapsPage({ ws }: { ws: string }) {
  const [gaps, setGaps] = useState<Gap[]>([])
  useEffect(() => { api.gaps(ws).then(setGaps) }, [ws])
  return (
    <div className="page">
      <h1>What we're missing</h1>
      <p className="muted">Unanswered questions and agent context misses, grouped by topic. Big clusters are candidates for docs, features or projects.</p>
      {gaps.length === 0 && <section className="card muted">No gaps yet.</section>}
      {gaps.map(g => (
        <section key={g.topic} className="card">
          <div className="row spread"><h2>{g.topic}</h2><span className="count">{g.count}</span></div>
          {g.examples.map((e, i) => <p key={i} className="muted">“{e}”</p>)}
        </section>
      ))}
    </div>
  )
}
