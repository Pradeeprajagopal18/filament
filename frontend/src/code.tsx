// Code tab: the repo's symbol-level graph (Filament's tree-sitter extractor, no model). Subsystems
// you can drill into, explain-by-click, hotspots, design notes and change / PR impact.
import cytoscape from 'cytoscape'
import { useEffect, useRef, useState } from 'react'
import { api } from './api'
import type { ChangeImpact, CodeExplain, CodeGraphView, CodeLookup, CodeSym, RepoDetail } from './api'
import { ErrorLine, Mermaid } from './components'

const REL_COLOR: Record<string, string> = {
  calls: '#2f80ed', imports: '#9aa5b1', imports_from: '#9aa5b1', inherits: '#9b51e0', uses: '#27ae60',
  references: '#e2a03f', contains: '#d5d9de', method: '#56657a',
}

function Graph({ view, onPick }: { view: CodeGraphView; onPick: (id: string, isCommunity: boolean) => void }) {
  const box = useRef<HTMLDivElement>(null)
  useEffect(() => {
    if (!box.current) return
    const subsystems = view.mode === 'subsystems'
    const max = Math.max(1, ...view.nodes.map(n => n.size ?? 1))
    const cy = cytoscape({
      container: box.current,
      elements: [
        ...view.nodes.map(n => ({ data: { id: n.id, label: n.label, size: subsystems ? 24 + 46 * Math.sqrt((n.size ?? 1) / max) : 18,
          kind: n.kind ?? 'code', inside: n.inside ?? true, community: n.community } })),
        ...view.edges.map((e, i) => ({ data: { id: `e${i}`, source: e.source, target: e.target, rel: e.relation ?? 'link',
          inferred: e.confidence === 'inferred', weight: e.weight ?? 1 } })),
      ],
      style: [
        { selector: 'node', style: { label: 'data(label)', width: 'data(size)', height: 'data(size)', 'font-size': 9, color: '#333',
          'text-valign': 'bottom', 'text-margin-y': 3, 'background-color': subsystems ? '#f0b429' : '#2f80ed', 'border-width': 1, 'border-color': '#fff' } },
        { selector: 'node[?inside]', style: {} },
        { selector: 'node[!inside]', style: { 'background-color': '#c9d1da', 'font-size': 8, color: '#777' } },
        { selector: 'node[kind = "external"], node[kind = "concept"]', style: { 'background-color': '#c4825b', shape: 'round-rectangle' } },
        { selector: 'edge', style: { width: subsystems ? 'mapData(weight, 1, 20, 1, 6)' : 1.2, 'line-color': '#c9d1da', 'curve-style': 'bezier',
          'target-arrow-shape': subsystems ? 'none' : 'triangle', 'target-arrow-color': '#c9d1da', 'arrow-scale': 0.7 } },
        ...Object.entries(REL_COLOR).map(([rel, color]) => ({ selector: `edge[rel = "${rel}"]`, style: { 'line-color': color, 'target-arrow-color': color } })),
        { selector: 'edge[?inferred]', style: { 'line-style': 'dashed' } },
        { selector: ':selected', style: { 'border-width': 3, 'border-color': '#111' } },
      ],
      layout: { name: 'cose', animate: false, nodeRepulsion: () => (subsystems ? 9000 : 4500), idealEdgeLength: () => (subsystems ? 90 : 60), padding: 16 } as cytoscape.LayoutOptions,
      wheelSensitivity: 0.3,
    })
    cy.on('tap', 'node', evt => {
      const d = evt.target.data()
      onPick(subsystems ? String(d.community) : d.id, subsystems)
    })
    return () => cy.destroy()
  }, [view, onPick])
  return <div ref={box} className="code-graph" />
}

function Lookup<T>({ result, onPick, render }: { result: CodeLookup<T> | null; onPick: (id: string) => void; render: (d: T) => React.ReactNode }) {
  if (!result) return null
  if (result.ok) return <>{render(result.data)}</>
  const list = (result.candidates ?? result.suggestions ?? []) as CodeSym[]
  return (
    <div className="small">
      <p className={result.ambiguous ? '' : 'error'}>{result.ambiguous ? `${result.detail}:` : result.detail}{!result.ambiguous && list.length ? '. Did you mean:' : ''}</p>
      <ul className="plain">{list.map(c => <li key={c.id}><button className="link small" onClick={() => onPick(c.id)}>{c.label}</button> <span className="muted">{c.file}:{c.line}</span></li>)}</ul>
    </div>
  )
}

function ExplainView({ d, onPick }: { d: CodeExplain; onPick: (id: string) => void }) {
  const refs = (label: string, items: CodeExplain['calls']) => items.length > 0 && (
    <div><b>{label}</b> {items.map((c, i) => (
      <button key={i} className={`chip sym conf-${c.confidence}`} title={`${c.relation} · ${c.confidence}${c.at ? ' · ' + c.at : ''}`} onClick={() => onPick(c.node.id)}>{c.node.label}</button>
    ))}</div>
  )
  return (
    <div className="explain small">
      <div><strong>{d.symbol.label}</strong> <span className="muted">{d.symbol.file}:{d.symbol.line}</span></div>
      {d.serves_endpoints.length > 0 && <div><b>Serves</b> {d.serves_endpoints.map(e => <code key={e} className="ep-chip">{e}</code>)}</div>}
      {d.uses_services.length > 0 && <div><b>Reaches</b> {d.uses_services.map(s => <span key={s.service} className="cat cat-external">{s.label}{s.via === 'direct' ? '' : ` via ${s.via}`}</span>)}</div>}
      {refs('Calls', d.calls)}
      {refs('Called by', d.called_by)}
      {refs('Other links', d.other.slice(0, 12))}
      {d.notes.length > 0 && <div><b>Notes</b><ul className="plain">{d.notes.map((n, i) => <li key={i} className="muted">“{n}”</li>)}</ul></div>}
      <p className="muted">Solid chips are read from the code; dashed ones were inferred.</p>
    </div>
  )
}

export function CodeTab({ repo }: { repo: RepoDetail }) {
  const code = repo.analysis.code
  const [view, setView] = useState<CodeGraphView | null>(null)
  const [name, setName] = useState('')
  const [explained, setExplained] = useState<CodeLookup<CodeExplain> | null>(null)
  const [error, setError] = useState('')
  const loadView = (community?: number) => api.repoCodeGraph(repo.id, community).then(setView).catch(e => setError(String(e)))
  useEffect(() => { if (code?.symbols) loadView() }, [repo.id]) // eslint-disable-line react-hooks/exhaustive-deps
  async function explain(target: string) {
    setName(target)
    setExplained(await api.codeExplain(repo.id, target))
  }
  const pick = (id: string, isCommunity: boolean) => { if (isCommunity) loadView(Number(id)); else explain(id) }

  if (!code?.engine || code.error || !code.symbols) {
    return <section className="card muted">{code?.error ?? code?.reason ?? 'No code graph yet.'} The code graph is built from the source on each scan (Python, JavaScript, TypeScript, Java, Go); no model is involved.</section>
  }
  const names = Object.fromEntries(code.communities.map(c => [c.id, c.name]))
  return (
    <>
      <div className="stats">
        <div><b>{code.symbols}</b><span>functions &amp; classes</span></div>
        <div><b>{code.files}</b><span>files</span></div>
        <div><b>{code.edges}</b><span>links</span></div>
        <div><b>{code.extracted_pct}%</b><span>read from code</span></div>
        <div><b>{code.communities.length}</b><span>subsystems</span></div>
        <div className={code.cycles.length ? 'stat-alert' : ''}><b>{code.cycles.length}</b><span>import cycles</span></div>
      </div>
      <section className="card">
        <div className="row spread wrap">
          <h3>{view?.mode === 'symbols' ? `Subsystem: ${view.name ?? names[view.community ?? -1] ?? view.community}` : 'Subsystems'}</h3>
          {view?.mode === 'symbols' && <button className="link small" onClick={() => loadView()}>← All subsystems</button>}
        </div>
        <p className="muted small">{view?.mode === 'symbols' ? 'Tap a symbol to explain it. Grey nodes are neighbours in other subsystems; dashed lines are inferred.' : 'Tap a subsystem to see its functions and classes. Size is how much code it holds; lines are links between subsystems.'}</p>
        <ErrorLine error={error} />
        {view && <Graph view={view} onPick={pick} />}
        <div className="legend small">{Object.entries({ calls: 'calls', imports: 'imports', uses: 'uses type', inherits: 'inherits', references: 'references' }).map(([k, v]) => (
          <span key={k}><i style={{ background: REL_COLOR[k] }} />{v}</span>
        ))}</div>
      </section>
      <section className="card">
        <h3>Explain a symbol</h3>
        <div className="row">
          <input value={name} onChange={e => setName(e.target.value)} placeholder="e.g. place_order or app/events.py:publish"
            onKeyDown={e => e.key === 'Enter' && name.trim() && explain(name.trim())} />
          <button className="secondary" disabled={!name.trim()} onClick={() => explain(name.trim())}>Explain</button>
        </div>
        <Lookup result={explained} onPick={explain} render={d => <ExplainView d={d} onPick={explain} />} />
      </section>
      <ImpactPanel repo={repo} />
      <section className="card">
        <h3>Hotspots</h3>
        <p className="muted small">The most-depended-on code. Changes here ripple furthest.</p>
        <ul className="plain small">{code.hotspots.slice(0, 8).map(h => (
          <li key={h.id}><button className="link small" onClick={() => explain(h.id)}>{h.label}</button> <span className="muted">{h.file}:{h.line} · used by {h.fan_in} · uses {h.fan_out}</span></li>
        ))}</ul>
        {code.cycles.length > 0 && <>
          <h3>Import cycles</h3>
          <ul className="plain small">{code.cycles.map((c, i) => <li key={i}><code>{c.map(x => x.split('/').pop()).join(' → ')}</code></li>)}</ul>
        </>}
      </section>
      <section className="card">
        <h3>Subsystems</h3>
        <ul className="plain small">{code.communities.slice(0, 12).map(c => (
          <li key={c.id}><button className="link small" onClick={() => loadView(c.id)}>{c.name}</button> <span className="muted">{c.size} symbols · {c.files} files · cohesion {Math.round(c.cohesion * 100)}% · {c.key_symbols.slice(0, 3).join(', ')}</span></li>
        ))}</ul>
      </section>
      {code.rationale.length > 0 && (
        <section className="card">
          <h3>Design notes in the code</h3>
          <p className="muted small">NOTE / WHY / DECISION comments. Filament proposes them to project memory so agents see the reasoning, linked to the ADRs they cite.</p>
          <ul className="plain small">{code.rationale.map(n => (
            <li key={n.id}><span className="badge badge-draft">{n.tag}</span> {n.text} <span className="muted">{n.file}:{n.line}{n.about ? ` · ${n.about}` : ''}</span>
              {n.refs.map(r => <span key={r} className="badge badge-approved">{r}</span>)}</li>
          ))}</ul>
        </section>
      )}
      <p className="muted small">Built from the code only{code.languages ? ` (${Object.entries(code.languages).map(([l, n]) => `${n} ${l}`).join(', ')} files)` : ''}; nothing is sent to a model.</p>
    </>
  )
}

function ImpactPanel({ repo }: { repo: RepoDetail }) {
  const [input, setInput] = useState('')
  const [res, setRes] = useState<ChangeImpact | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  async function run() {
    setBusy(true); setError('')
    const text = input.trim()
    const body = /^https?:\/\/github\.com\/.+\/pull\/\d+/.test(text) ? { pr: text }
      : text.includes('@@') || text.startsWith('diff --git') ? { diff: text }
        : { files: text.split(/[\s,]+/).filter(Boolean) }
    try { setRes(await api.codeChanges(repo.id, body)) } catch (e) { setError(String(e)) } finally { setBusy(false) }
  }
  const hot = repo.analysis.code?.hotspots[0]?.file
  return (
    <section className="card">
      <h3>What would a change affect?</h3>
      <p className="muted small">Paste changed files, a <code>git diff</code>, or a GitHub pull request URL. Filament follows the code graph to the endpoints touched and the repos that call them.</p>
      <textarea rows={3} value={input} onChange={e => setInput(e.target.value)} placeholder={hot ? `e.g. ${hot}` : 'e.g. app/events.py'} />
      <div className="row"><button className="secondary" disabled={!input.trim() || busy} onClick={run}>{busy ? 'Tracing…' : 'Trace impact'}</button></div>
      <ErrorLine error={error} />
      {res && (
        <div className="impact small">
          <div><span className={`risk risk-${res.risk}`}>{res.risk} risk</span> {res.summary}</div>
          {res.consumers.length > 0 && <div><b>Repos affected</b>{res.consumers.map(c => <div key={c.repo}>{c.repo}: {c.endpoints.map(e => <code key={e} className="ep-chip">{e}</code>)}</div>)}</div>}
          {res.endpoints.length > 0 && <div><b>Endpoints</b> {res.endpoints.map(e => <code key={e} className="ep-chip">{e}</code>)}</div>}
          {res.hotspots_touched.length > 0 && <div><b>Touches hotspots</b> {res.hotspots_touched.join(', ')}</div>}
          <div><b>Changed</b> {res.changed.filter(c => c.kind === 'code').slice(0, 12).map(c => <code key={c.id} className="ep-chip">{c.label}</code>)}</div>
          {res.affected.length > 0 && <details><summary>Affected code ({res.affected.length})</summary>
            <ul className="plain">{res.affected.slice(0, 40).map(a => <li key={a.id}>{a.label} <span className="muted">{a.file}:{a.line} · {a.distance} hop{a.distance === 1 ? '' : 's'} away</span></li>)}</ul></details>}
        </div>
      )}
    </section>
  )
}

export function DataTab({ repo }: { repo: RepoDetail }) {
  const dm = repo.analysis.data_model
  if (!dm?.entities.length) return <section className="card muted">No data model found. Filament reads SQL migrations, SQLAlchemy, Django and Prisma models.</section>
  return (
    <>
      <section className="card">
        <p className="muted small">{dm.entities.length} tables and {dm.relations.length} relationships from {[...new Set(dm.entities.map(e => e.source))].join(', ')}.</p>
        <div className="content"><Mermaid code={dm.mermaid} /></div>
      </section>
      <section className="card">
        {dm.entities.map(e => (
          <details key={e.name} className="model">
            <summary><code>{e.name}</code> <span className="muted small">{e.fields.length} columns · {e.file}:{e.line}</span></summary>
            <table className="fields"><tbody>{e.fields.map(f => (
              <tr key={f.name}><td><code>{f.name}</code>{f.pk && <span className="badge badge-approved">PK</span>}{f.fk && <span className="badge badge-in_review">→ {f.fk}</span>}</td>
                <td className="muted small">{f.type}{f.nullable ? '' : ' · required'}</td></tr>
            ))}</tbody></table>
          </details>
        ))}
      </section>
    </>
  )
}
