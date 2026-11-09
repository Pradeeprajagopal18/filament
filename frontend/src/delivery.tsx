// Delivery tab: work items moving through Plan → Design → Build → Test → Review → Deploy →
// Operate. Each stage shows its agent, its state and the person who signs it off.
import { useCallback, useEffect, useState, type ReactNode } from 'react'
import { api, DEMO, type Artifact, type DeliveryBoard, type Handoff, type Project, type SloView, type StageState, type WorkItem } from './api'
import { Badge, Content, ErrorLine } from './components'

const GOOD = new Set(['approved', 'done', 'passed', 'watching'])
const BAD = new Set(['failing', 'blocked', 'incident', 'rolled_back'])
const tone = (s: string) => GOOD.has(s) ? 'good' : BAD.has(s) ? 'bad' : s === 'todo' ? 'todo' : 'wait'
const label = (s: string) => s === 'todo' ? 'not started' : s.replace(/_/g, ' ')

export function DeliveryTab({ project }: { project: Project }) {
  const [board, setBoard] = useState<DeliveryBoard | null>(null)
  const [open, setOpen] = useState<string | null>(null)
  const [title, setTitle] = useState('')
  const [body, setBody] = useState('')
  const [repo, setRepo] = useState('')
  const [error, setError] = useState('')
  const [busy, setBusy] = useState('')

  const load = useCallback(async () => setBoard(await api.delivery(project.id)), [project.id])
  useEffect(() => { load().catch(e => setError(String(e))) }, [load])

  async function act(key: string, fn: () => Promise<unknown>) {
    setBusy(key); setError('')
    try { await fn(); await load() } catch (e) { setError(String(e).replace(/^Error: \d+: /, '')) } finally { setBusy('') }
  }

  if (!board) return <ErrorLine error={error} />
  const item = board.items.find(i => i.id === open)
  const hint = board.hint && <p className="note">Preview, next step: {board.hint}</p>
  if (item) return <ItemView item={item} items={board.items} slos={board.slos} busy={busy} act={act} error={error} hint={hint} back={() => { setOpen(null); setError('') }} onOpen={setOpen} />

  return (
    <div className="page">
      {hint}
      <section className="card">
        <h2>Work items</h2>
        <p className="muted small">Each item moves through seven stages. An agent does the work at each stage; a person signs off where it matters. Import issues from GitHub or add one by hand.</p>
        <div className="row">
          <input value={title} onChange={e => setTitle(e.target.value)} placeholder="Title, e.g. Include the cart total in order.placed" />
        </div>
        <textarea rows={3} value={body} onChange={e => setBody(e.target.value)}
          placeholder={'What and why. Add acceptance criteria as a checklist:\n- [ ] order.placed includes total_cents'} />
        <div className="row">
          <button disabled={!title.trim() || !!busy} onClick={() => act('add', async () => { const i = await api.addIssue(project.id, title, body); setTitle(''); setBody(''); setOpen(i.id) })}>Add work item</button>
          <span className="muted small">or import</span>
          <input value={repo} onChange={e => setRepo(e.target.value)} placeholder="owner/repo" style={{ maxWidth: 220 }} />
          <button className="secondary" disabled={!repo.trim() || !!busy} onClick={() => act('import', () => api.importIssues(project.id, repo))}>
            {busy === 'import' ? 'Importing…' : 'Import issues'}
          </button>
        </div>
        <ErrorLine error={error} />
      </section>

      {board.items.length === 0 && <p className="muted">No work items yet.</p>}
      <div className="items">
        {board.items.map(i => (
          <button key={i.id} className="card work-item" onClick={() => setOpen(i.id)}>
            <div className="row spread nowrap">
              <span className="item-title">{i.follow_up_of && <span className="chip chip-incident">follow-up</span>} {i.title}</span>
              {i.readiness != null && <span className={`score s-${i.readiness >= 70 ? 'good' : i.readiness >= 40 ? 'wait' : 'bad'}`}>{i.readiness}</span>}
            </div>
            <div className="pipe-mini" aria-label="Stage progress">
              {i.stages.map(s => <span key={s.key} className={`seg t-${tone(s.state)} ${s.key === i.stage ? 'current' : ''}`} title={`${s.label}: ${label(s.state)}`} />)}
            </div>
            <div className="muted small">{board.stages.find(s => s.key === i.stage)?.label}: {label(i.stages.find(s => s.key === i.stage)!.state)}{waitingFor(i)}</div>
          </button>
        ))}
      </div>

      {board.slos.length > 0 && <SloCard slos={board.slos} />}
    </div>
  )
}

function waitingFor(i: WorkItem) {
  const s = i.stages.find(x => x.pending_approvals.length)
  return s ? ` · waiting for ${s.role_label.toLowerCase()}` : ''
}

type Act = (key: string, fn: () => Promise<unknown>) => Promise<void>

function ItemView({ item, items, slos, busy, act, error, hint, back, onOpen }: {
  item: WorkItem; items: WorkItem[]; slos: SloView[]; busy: string; act: Act; error: string; hint: ReactNode; back: () => void; onOpen: (id: string) => void
}) {
  const [autonomy, setAutonomy] = useState('L2')
  const [target, setTarget] = useState('claude_code')
  const [shown, setShown] = useState<Artifact | null>(null)
  const [summary, setSummary] = useState('')

  const run = (s: StageState) => act(`run-${s.key}`, async () => {
    const r = await api.advance(item.id, { stage: s.key, autonomy, target })
    setSummary(`${s.agent_label}: ${String(r.run.output?.summary ?? r.run.status)}`)
    if (r.run.status !== 'succeeded') throw new Error(r.run.error ?? r.run.status)
  })
  const approve = (s: StageState, decision: 'approved' | 'rejected') => act(`ok-${s.key}`, async () => {
    for (const ap of s.pending_approvals) await api.decide(ap.artifact_id, decision)
  })
  const show = (id: string) => api.artifact(id).then(setShown).catch(() => {})

  return (
    <div className="page">
      <div className="row spread">
        <button className="secondary" onClick={back}>← Work items</button>
        <div className="row">
          <label>Autonomy <select value={autonomy} onChange={e => setAutonomy(e.target.value)}>
            <option value="L1">L1 · drafts</option><option value="L2">L2 · review</option><option value="L3">L3 · act</option>
          </select></label>
          <label>Coding agent <select value={target} onChange={e => setTarget(e.target.value)}>
            <option value="claude_code">Claude Code</option><option value="codex">Codex</option><option value="cursor">Cursor</option>
          </select></label>
        </div>
      </div>
      <section className="card">
        <h1>{item.title}</h1>
        <p className="muted small">{item.readiness != null ? `Readiness ${item.readiness} · ` : ''}{item.url ? <a href={item.url} target="_blank" rel="noreferrer">GitHub issue</a> : 'Added by hand'}{item.follow_up_of ? ' · follow-up from an incident' : ''}</p>
        {summary && <p className="note">{summary}</p>}
        {hint}
        <ErrorLine error={error} />
      </section>

      <div className="pipeline">
        <div className="pipe-head"><span>Agent</span><span>Stage</span><span>Human checkpoint</span></div>
        {item.stages.map(s => (
          <div key={s.key} className={`pipe-row ${s.key === item.stage ? 'current' : ''}`}>
            <div className="pipe-agent">
              <b>{s.agent_label}</b>
              <span className="muted small">{s.agent_does}</span>
              <button className={s.key === item.stage ? '' : 'secondary'} disabled={!!busy} onClick={() => run(s)}>
                {busy === `run-${s.key}` ? 'Working…' : s.state === 'todo' ? 'Run' : 'Run again'}
              </button>
            </div>
            <div className={`pipe-stage s-${s.key}`}>
              <div className="row spread nowrap"><b>{s.label}</b><span className={`state t-${tone(s.state)}`}>{label(s.state)}</span></div>
              <span className="muted small">{s.work}</span>
              {s.detail && <span className="small">{s.detail}</span>}
              {s.artifacts.length > 0 && <div className="chips">{s.artifacts.map(a => (
                <button key={a.id} className="chip link" onClick={() => show(a.id)}>{a.type.replace('_', ' ')}: {a.title.slice(0, 40)}</button>
              ))}</div>}
              {s.key === 'build' && <BuildExtras stage={s} act={act} busy={busy} />}
              {s.key === 'deploy' && <DeployExtras stage={s} act={act} busy={busy} />}
              {s.key === 'operate' && <OperateExtras stage={s} slos={slos} act={act} busy={busy} onOpen={onOpen} items={items} />}
            </div>
            <div className={`pipe-check ${s.role ? '' : 'auto'}`}>
              <b>{s.role ? s.role_label : 'Auto retry'}</b>
              <span className="small">{s.role ? s.checkpoint : 'Loops until all checks pass'}</span>
              {s.pending_approvals.length > 0 && (
                <div className="row">
                  <button disabled={!!busy} onClick={() => approve(s, 'approved')}>{s.pending_approvals.length > 1 ? `Approve all ${s.pending_approvals.length}` : 'Approve'}</button>
                  <button className="secondary" disabled={!!busy} onClick={() => approve(s, 'rejected')}>Reject</button>
                </div>
              )}
              {s.role && !s.pending_approvals.length && GOOD.has(s.state) && <span className="small ok">Signed off</span>}
            </div>
          </div>
        ))}
      </div>

      {shown && (
        <section className="card">
          <div className="row spread"><h2>{shown.title}</h2><button className="secondary" onClick={() => setShown(null)}>Close</button></div>
          <p className="muted small">{shown.type.replace('_', ' ')} · by {shown.author} · <Badge value={shown.status} /></p>
          <ArtifactBody art={shown} />
        </section>
      )}
    </div>
  )
}

function ArtifactBody({ art }: { art: Artifact }) {
  const d = art.data as Record<string, unknown>
  const findings = d.findings as { severity: string; title: string; detail: string; file?: string; line?: number }[] | undefined
  const cases = d.cases as { kind: string; title: string }[] | undefined
  if (findings) return (
    <div className="findings">
      <p className="small">Verdict <b>{String(d.verdict).replace('_', ' ')}</b> · risk {String(d.risk)} · {String(d.files)} file{d.files === 1 ? '' : 's'}, {String(d.changed_lines)} lines</p>
      {findings.length === 0 && <p className="muted">No findings.</p>}
      {findings.map((f, i) => <div key={i} className={`finding sev-${f.severity}`}><span className="sev">{f.severity}</span><div><b>{f.title}</b>{f.file && <code>{f.file}{f.line ? `:${f.line}` : ''}</code>}<div className="small muted">{f.detail}</div></div></div>)}
    </div>
  )
  if (cases) return <ul className="cases">{cases.map((c, i) => <li key={i}><span className="chip">{c.kind}</span> {c.title}</li>)}</ul>
  return <Content text={art.content} />
}

function BuildExtras({ stage, act, busy }: { stage: StageState; act: Act; busy: string }) {
  const tasks = stage.artifacts.filter(a => a.type === 'task')
  const task = tasks[tasks.length - 1]
  const [h, setH] = useState<Handoff | null>(null)
  const [url, setUrl] = useState(DEMO ? 'https://github.com/acme/checkout-api/pull/42' : '')
  const [copied, setCopied] = useState(false)
  const taskId = task?.id
  useEffect(() => { if (taskId) api.handoff(taskId).then(setH).catch(() => setH(null)) }, [taskId])
  if (!task) return null
  const hasPr = stage.artifacts.some(a => a.type === 'pull_request')
  return (
    <div className="extras">
      {h && <>
        <span className="small">Branch <code>{h.branch}</code>{h.attempt > 1 ? ` · attempt ${h.attempt}` : ''}</span>
        <code className="cmd">{h.commands[h.target ?? 'claude_code']}</code>
        <div className="row">
          <button className="secondary" onClick={() => navigator.clipboard?.writeText(h.prompt).then(() => setCopied(true))}>{copied ? 'Copied' : 'Copy prompt'}</button>
          <span className="muted small">Or connect the coding agent to Filament's MCP endpoint: it reads the task with <code>filament_get_task</code>.</span>
        </div>
      </>}
      {!hasPr && <div className="row">
        <input value={url} onChange={e => setUrl(e.target.value)} placeholder="Pull request URL (the GitHub webhook links it for you)" />
        <button disabled={!url.trim() || !!busy} onClick={() => act('pr', () => api.linkPr(task.id, url))}>Link PR</button>
      </div>}
    </div>
  )
}

function DeployExtras({ stage, act, busy }: { stage: StageState; act: Act; busy: string }) {
  const rel = stage.artifacts.find(a => a.type === 'release')
  if (!rel || rel.status !== 'approved' || ['rolled_back', 'done'].includes(stage.state)) return null
  return (
    <div className="row">
      <button disabled={!!busy} onClick={() => act('promote', () => api.releaseAction(rel.id, 'promote'))}>Promote next step</button>
      {stage.state === 'rolling_out' && <button className="secondary" disabled={!!busy} onClick={() => act('rollback', () => api.releaseAction(rel.id, 'rollback', 'Manual rollback'))}>Roll back</button>}
    </div>
  )
}

function OperateExtras({ stage, slos, act, busy, onOpen, items }: { stage: StageState; slos: SloView[]; act: Act; busy: string; onOpen: (id: string) => void; items: WorkItem[] }) {
  const open = stage.state === 'incident' ? stage.artifacts.filter(a => a.type === 'incident') : []
  const incidents = new Set(stage.artifacts.filter(a => a.type === 'incident').map(a => a.id))
  const followUps = items.filter(i => i.follow_up_of && incidents.has(i.follow_up_of))
  return (
    <div className="extras">
      {slos.map(s => <span key={s.id} className="small">SLO {s.title}: <span className={`state t-${s.state === 'ok' ? 'good' : s.state === 'warning' ? 'wait' : 'bad'}`}>{s.state}</span></span>)}
      {open.map(a => (
        <div key={a.id} className="row">
          <button className="secondary" disabled={!!busy} onClick={() => act('resolve', () => api.resolveIncident(a.id, 'Resolved from the delivery board'))}>Resolve incident</button>
        </div>
      ))}
      {followUps.map(f => <button key={f.id} className="secondary" onClick={() => onOpen(f.id)}>Follow-up for triage: {f.title.replace('Follow-up: ', '')}</button>)}
    </div>
  )
}

function SloCard({ slos }: { slos: SloView[] }) {
  return (
    <section className="card">
      <h2>SLOs the SRE agent watches</h2>
      {slos.map(s => (
        <div key={s.id} className="row spread">
          <div><b>{s.title}</b> <span className="muted small">{s.objective}% · {s.reason}</span></div>
          <div className="small muted">burn {Object.entries(s.burn).filter(([, v]) => v != null).map(([k, v]) => `${k} ${v}x`).join(' · ') || 'no data yet'} <span className={`state t-${s.state === 'ok' ? 'good' : s.state === 'warning' ? 'wait' : 'bad'}`}>{s.state}</span></div>
        </div>
      ))}
    </section>
  )
}
