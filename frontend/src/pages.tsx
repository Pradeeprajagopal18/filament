import { useCallback, useEffect, useState } from 'react'
import { api, type Artifact, type Gap, type Memory, type Project, type Readiness, type Run, type UsageRow } from './api'
import { Badge, Content, ErrorLine, Tokens } from './components'

const TYPE_ORDER = ['idea', 'prd', 'story', 'flow', 'adr', 'api_contract', 'task', 'test_plan', 'release']

// -- Project: run agents, browse the graph, approve at the human gate --------------
export function ProjectPage({ project }: { project: Project }) {
  const [artifacts, setArtifacts] = useState<Artifact[]>([])
  const [runs, setRuns] = useState<Run[]>([])
  const [selected, setSelected] = useState<Artifact | null>(null)
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

  async function run(agent: string) {
    setBusy(agent); setError('')
    try {
      const input = agent === 'requirements' ? { idea } : {}
      const r = await api.startRun(project.id, agent, agent === 'requirements' ? 'Draft requirements' : 'Draw the flow', input, autonomy)
      if (r.status !== 'succeeded') setError(r.error ?? 'Run failed')
      if (agent === 'requirements' && r.status === 'succeeded') setIdea('')
      await load()
    } catch (e) { setError(String(e)) } finally { setBusy('') }
  }

  async function decide(decision: 'approved' | 'rejected') {
    if (!selected) return
    const updated = await api.decide(selected.id, decision)
    setSelected(updated)
    await load()
  }

  const grouped = TYPE_ORDER.map(t => [t, artifacts.filter(a => a.type === t)] as const).filter(([, list]) => list.length)

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
          <label>Autonomy
            <select value={autonomy} onChange={e => setAutonomy(e.target.value)}>
              <option value="L0">L0 · suggest only</option>
              <option value="L1">L1 · save drafts</option>
              <option value="L2">L2 · send to review</option>
              <option value="L3">L3 · act and report</option>
            </select>
          </label>
          <button disabled={!idea.trim() || !!busy} onClick={() => run('requirements')}>
            {busy === 'requirements' ? 'Writing PRD…' : 'Write PRD and stories'}
          </button>
          <button className="secondary" disabled={!!busy || !artifacts.some(a => a.type === 'story')} onClick={() => run('flow')}>
            {busy === 'flow' ? 'Drawing…' : 'Draw the flow'}
          </button>
        </div>
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

      <section className="card">
        <h2>Agent runs</h2>
        <table>
          <thead><tr><th>Agent</th><th>Status</th><th>Autonomy</th><th>Model</th><th>Tokens in / out</th><th>Context items</th></tr></thead>
          <tbody>
            {runs.map(r => (
              <tr key={r.id} title={r.error ?? ''}>
                <td>{r.agent}</td><td><Badge value={r.status} /></td><td>{r.autonomy}</td><td>{r.model}</td>
                <td><Tokens n={r.input_tokens} /> / <Tokens n={r.output_tokens} /></td>
                <td>{r.context_pack?.items?.length ?? 0}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </section>
    </div>
  )
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

// -- Launchpad -------------------------------------------------------------------------
export function LaunchpadPage() {
  const [repo, setRepo] = useState('')
  const [reports, setReports] = useState<Readiness[]>([])
  const [current, setCurrent] = useState<Readiness | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  useEffect(() => { api.readiness().then(setReports) }, [])

  async function scan() {
    setBusy(true); setError('')
    try {
      const r = await api.scan(repo)
      setCurrent(r)
      setReports(rs => [r, ...rs])
    } catch (e) { setError(String(e)) } finally { setBusy(false) }
  }

  return (
    <div className="page">
      <h1>Launchpad · repo readiness</h1>
      <p className="muted">Is this repo structured so agents can work in it? Paste a local path or a git URL.</p>
      <section className="card row">
        <input value={repo} onChange={e => setRepo(e.target.value)} placeholder="github.com/acme/checkout-api or /path/to/repo" />
        <button disabled={!repo.trim() || busy} onClick={scan}>{busy ? 'Scanning…' : 'Check readiness'}</button>
      </section>
      <ErrorLine error={error} />
      {current && (
        <section className="card">
          <div className="row spread">
            <h2>{current.repo}</h2>
            <span className={`grade grade-${current.grade}`}>{current.grade}<small>{current.score}</small></span>
          </div>
          <table>
            <tbody>
              {current.checks.map(c => (
                <tr key={c.id}>
                  <td className={c.passed ? 'pass' : 'fail'}>{c.passed ? '✔' : '✘'}</td>
                  <td>{c.title}</td>
                  <td className="muted">{c.passed ? c.detail : <>{c.detail} — <em>{c.fix}</em></>}</td>
                  <td className="muted">{c.weight}</td>
                </tr>
              ))}
            </tbody>
          </table>
          <p className="muted small">Generate starter files for failing checks with <code>fil ready fix &lt;path&gt; --write</code>.</p>
        </section>
      )}
      {reports.length > 0 && (
        <section className="card">
          <h2>History</h2>
          <table>
            <thead><tr><th>Repo</th><th>Grade</th><th>Score</th><th>Checked</th></tr></thead>
            <tbody>
              {reports.map(r => (
                <tr key={r.id} onClick={() => setCurrent(r)} className="clickable">
                  <td>{r.repo}</td><td><span className={`grade-sm grade-${r.grade}`}>{r.grade}</span></td>
                  <td>{r.score}</td><td>{new Date(r.created_at).toLocaleString()}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </section>
      )}
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
