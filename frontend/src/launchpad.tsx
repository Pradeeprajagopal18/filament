// Launchpad: onboard any repo (app, API, frontend, infrastructure, config, library), see what it is,
// what it needs, its API contract, mock UI, components, who depends on it and how it changes over
// time. Filament never writes to these repos: what it creates is kept as references in Filament.
import { useEffect, useMemo, useState } from 'react'
import { api, DEMO } from './api'
import type { Artifact, Check, Endpoint, Impact, JsonSchema, RepoDetail, RepoScan, RepoSummary, SystemMap, UiElement } from './api'
import { Content, ErrorLine, Mermaid } from './components'

const KIND_LABEL: Record<string, string> = {
  frontend: 'Frontend', backend: 'Backend', api: 'API', infrastructure: 'Infrastructure', config: 'Config',
  mobile: 'Mobile', data: 'Data', library: 'Library', docs: 'Docs', monorepo: 'Monorepo', common: 'Every repo',
}
const GEN_LABEL: Record<string, string> = {
  agents_md: 'Agent instructions', overview: 'Overview', architecture: 'Architecture & components',
  codeowners: 'Ownership', api_contract: 'API contract (OpenAPI)',
}
const INTERVALS: [number, string][] = [[6, 'every 6 hours'], [24, 'daily'], [168, 'weekly'], [0, 'manually only']]

function Kinds({ kinds }: { kinds: string[] }) {
  return <span className="kinds">{kinds.map(k => <span key={k} className={`kind kind-${k}`}>{KIND_LABEL[k] ?? k}</span>)}</span>
}

function ago(iso?: string | null) {
  if (!iso) return ''
  const mins = Math.round((Date.now() - new Date(iso).getTime()) / 60000)
  if (Math.abs(mins) < 60) return mins >= 0 ? `${mins}m ago` : `in ${-mins}m`
  const h = Math.round(mins / 60)
  if (Math.abs(h) < 48) return h >= 0 ? `${h}h ago` : `in ${-h}h`
  const d = Math.round(h / 24)
  return d >= 0 ? `${d}d ago` : `in ${-d}d`
}

export function LaunchpadPage({ ws }: { ws: string }) {
  const [ref, setRef] = useState('')
  const [repos, setRepos] = useState<RepoSummary[]>([])
  const [current, setCurrent] = useState<RepoDetail | null>(null)
  const [view, setView] = useState<'repos' | 'map'>('repos')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')

  useEffect(() => { api.repos(ws).then(setRepos).catch(e => setError(String(e))) }, [ws])

  async function onboard() {
    setBusy(true); setError('')
    try {
      const r = await api.onboardRepo(ws, ref.trim())
      setCurrent(r); setRef('')
      setRepos(rs => [r, ...rs.filter(x => x.id !== r.id)])
    } catch (e) { setError(String(e)) } finally { setBusy(false) }
  }
  async function open(id: string) {
    setError('')
    try { setCurrent(await api.repo(id)) } catch (e) { setError(String(e)) }
  }
  function updated(r: RepoDetail) {
    setCurrent(r)
    setRepos(rs => rs.map(x => (x.id === r.id ? r : x)))
  }

  if (current) return <RepoView repo={current} onBack={() => setCurrent(null)} onChange={updated} onOpen={open} />

  return (
    <div className="page">
      <h1>Launchpad</h1>
      <p className="muted">Onboard any repo: an app, an API, a frontend, infrastructure or config. Filament works out what it is, checks what matters for that kind of repo, documents its API, screens and dependencies, and rescans it to keep that knowledge current.</p>
      <section className="card row">
        <input value={ref} onChange={e => setRef(e.target.value)} placeholder="github.com/acme/checkout-api or /path/to/repo"
          onKeyDown={e => e.key === 'Enter' && ref.trim() && !busy && onboard()} />
        <button disabled={!ref.trim() || busy} onClick={onboard}>{busy ? 'Analyzing…' : 'Onboard repo'}</button>
      </section>
      <ErrorLine error={error} />
      <div className="tabs" role="tablist">
        <button role="tab" aria-selected={view === 'repos'} className={view === 'repos' ? 'active' : ''} onClick={() => setView('repos')}>
          Repos<span className="count">{repos.length}</span>
        </button>
        <button role="tab" aria-selected={view === 'map'} className={view === 'map' ? 'active' : ''} onClick={() => setView('map')}>System map</button>
      </div>
      {view === 'map' && <SystemMapView ws={ws} repos={repos} onOpen={open} />}
      {view === 'repos' && repos.length === 0 && <section className="card muted">No repos yet. Onboard one above.</section>}
      {view === 'repos' && (
        <div className="repo-grid">
          {repos.map(r => {
            const made = Object.keys(r.generated ?? {}).length
            return (
              <button key={r.id} className="card repo-card" onClick={() => open(r.id)}>
                <div className="row spread">
                  <strong>{r.name}</strong>
                  <span className={`grade-sm grade-${r.grade}`}>{r.grade}</span>
                </div>
                <Kinds kinds={r.kinds} />
                <span className="muted small">{r.ref}</span>
                <span className="muted small">Scanned {ago(r.scanned_at)}{r.next_scan_at ? ` · next ${ago(r.next_scan_at)}` : ' · manual'}</span>
                {r.last_error && <span className="small error-text">Last scan failed</span>}
                {made > 0 && <span className="small in-filament">{made} reference{made > 1 ? 's' : ''} kept in Filament</span>}
              </button>
            )
          })}
        </div>
      )}
    </div>
  )
}

function SystemMapView({ ws, repos, onOpen }: { ws: string; repos: RepoSummary[]; onOpen: (id: string) => void }) {
  const [map, setMap] = useState<SystemMap | null>(null)
  const [error, setError] = useState('')
  const key = repos.map(r => r.id + r.scanned_at).join(',')
  useEffect(() => { api.systemMap(ws).then(setMap).catch(e => setError(String(e))) }, [ws, key])
  if (error) return <ErrorLine error={error} />
  if (!map) return <section className="card muted">Loading…</section>
  const names = Object.fromEntries(map.nodes.map(n => [n.id, n.label]))
  const links = map.edges.filter(e => e.kind !== 'uses')
  const shared = map.nodes.filter(n => n.kind !== 'repo' && (n.shared_by ?? 0) > 1)
  if (map.nodes.filter(n => n.kind === 'repo').length < 2) {
    return <section className="card muted">Onboard two or more repos to see how they connect.</section>
  }
  return (
    <>
      <section className="card">
        <p className="muted small">How your repos connect, from evidence in the code: HTTP calls matched to the endpoints they hit, configuration, provisioned infrastructure and shared services.</p>
        <Mermaid code={map.mermaid} />
      </section>
      {map.unresolved.length > 0 && (
        <section className="card warn">
          <h3>Calls to endpoints that don't exist</h3>
          <p className="muted small">These repos call an endpoint the target repo no longer serves. They will fail at runtime.</p>
          <ul className="plain">
            {map.unresolved.map((u, i) => (
              <li key={i}><b>{u.repo}</b> → {u.target}: <code>{u.method} {u.path}</code> <span className="muted small">{u.file}:{u.line}</span></li>
            ))}
          </ul>
        </section>
      )}
      <section className="card">
        <h3>Connections</h3>
        <ul className="links">
          {links.map((e, i) => (
            <li key={i}>
              <div>
                <button className="link" onClick={() => onOpen(e.source)}>{names[e.source]}</button>
                <span className={`edge edge-${e.kind}`}>{e.kind}</span>
                <button className="link" onClick={() => onOpen(e.target)}>{names[e.target]}</button>
              </div>
              {e.endpoints.length > 0 && <div className="small">{e.endpoints.map(ep => <code key={ep} className="ep-chip">{ep}</code>)}</div>}
              <div className="muted small">{e.evidence.slice(0, 2).map((ev, j) => <span key={j}>{ev.file}{ev.line ? `:${ev.line}` : ''} {ev.detail}{j === 0 && e.evidence.length > 1 ? ' · ' : ''}</span>)}</div>
            </li>
          ))}
        </ul>
        {shared.length > 0 && <p className="muted small">Shared services: {shared.map(n => `${n.label} (${n.shared_by} repos)`).join(', ')}</p>}
      </section>
    </>
  )
}

type Tab = 'readiness' | 'api' | 'ui' | 'components' | 'history' | 'filament'

function RepoView({ repo, onBack, onChange, onOpen }: {
  repo: RepoDetail; onBack: () => void; onChange: (r: RepoDetail) => void; onOpen: (id: string) => void
}) {
  const a = repo.analysis
  const [impact, setImpact] = useState<Impact | null>(null)
  const [scans, setScans] = useState<RepoScan[]>([])
  const [tab, setTab] = useState<Tab>('readiness')
  const [busy, setBusy] = useState('')
  const [error, setError] = useState('')
  useEffect(() => {
    api.repoImpact(repo.id).then(setImpact).catch(() => setImpact(null))
    api.repoScans(repo.id).then(setScans).catch(() => setScans([]))
  }, [repo.id, repo.scanned_at])
  const breaking = scans[0]?.changes?.breaking?.length ?? 0
  const pending = Object.keys(repo.generated ?? {}).length
  const tabs: [Tab, string, number | null][] = [
    ['readiness', 'Readiness', a.readiness.checks.filter(c => c.status === 'fail').length],
    ['api', 'API', a.api.endpoints.length || null],
    ['ui', 'UI', a.ui.pages.length || null],
    ['components', 'Components', (a.components.nodes.filter(n => n.category !== 'self').length + (impact?.dependents.length ?? 0)) || null],
    ['history', 'History', breaking || null],
    ['filament', 'In Filament', pending || null],
  ]

  async function generate(items: string[]) {
    setBusy(items.join(',') || 'all'); setError('')
    try { onChange(await api.generateRepo(repo.id, items)) } catch (e) { setError(String(e)) } finally { setBusy('') }
  }
  async function rescan() {
    setBusy('rescan'); setError('')
    try { onChange(await api.rescanRepo(repo.id)) } catch (e) { setError(String(e)) } finally { setBusy('') }
  }
  async function schedule(hours: number) {
    setError('')
    try { onChange(await api.setRepoSchedule(repo.id, hours)) } catch (e) { setError(String(e)) }
  }
  const interval = repo.scan_interval_hours ?? 24

  return (
    <div className="page">
      <button className="link" onClick={onBack}>← All repos</button>
      <section className="card">
        <div className="row spread">
          <div>
            <h1 className="repo-title">{repo.name}</h1>
            <Kinds kinds={repo.kinds} />
          </div>
          <span className={`grade grade-${repo.grade}`}>{repo.grade}<small>{Math.round(repo.score)}</small></span>
        </div>
        <p className="muted small">
          {repo.ref} · {a.profile.file_count} files
          {a.profile.frameworks.length > 0 && <> · {a.profile.frameworks.join(', ')}</>}
          {repo.commit && <> · <code>{repo.commit.slice(0, 7)}</code></>}
        </p>
        <details className="small">
          <summary>Why Filament thinks this is {repo.kinds.map(k => KIND_LABEL[k] ?? k).join(' + ')}</summary>
          <ul>{a.profile.kinds.map(k => <li key={k.kind}><b>{KIND_LABEL[k.kind] ?? k.kind}</b>: {k.evidence.slice(0, 3).join('; ')}</li>)}</ul>
        </details>
        <div className="row wrap scan-row">
          <button className="secondary" disabled={!!busy} onClick={rescan}>{busy === 'rescan' ? 'Rescanning…' : 'Rescan now'}</button>
          <span className="muted small">Scanned {ago(repo.scanned_at)}. Keep current:</span>
          <select value={interval} onChange={e => schedule(Number(e.target.value))} aria-label="Rescan interval">
            {INTERVALS.map(([h, label]) => <option key={h} value={h}>{label}</option>)}
            {!INTERVALS.some(([h]) => h === interval) && <option value={interval}>every {interval}h</option>}
          </select>
          {repo.next_scan_at && <span className="muted small">next {ago(repo.next_scan_at)}</span>}
        </div>
        {repo.last_error && <p className="error small">Last scan failed: {repo.last_error}. Showing the previous analysis.</p>}
      </section>
      <ErrorLine error={error} />
      <div className="tabs" role="tablist">
        {tabs.map(([t, label, n]) => (
          <button key={t} role="tab" aria-selected={tab === t} className={tab === t ? 'active' : ''} onClick={() => setTab(t)}>
            {label}{n != null && <span className={`count${t === 'history' && breaking ? ' count-alert' : ''}`}>{n}</span>}
          </button>
        ))}
      </div>
      {tab === 'readiness' && <ReadinessTab repo={repo} busy={busy} generate={generate} openFilament={() => setTab('filament')} />}
      {tab === 'api' && <ApiTab repo={repo} busy={busy} generate={generate} impact={impact} onChange={onChange} />}
      {tab === 'ui' && <UiTab repo={repo} />}
      {tab === 'components' && <ComponentsTab repo={repo} impact={impact} onOpen={onOpen} />}
      {tab === 'history' && <HistoryTab scans={scans} />}
      {tab === 'filament' && <FilamentTab repo={repo} />}
    </div>
  )
}

function ReadinessTab({ repo, busy, generate, openFilament }: {
  repo: RepoDetail; busy: string; generate: (items: string[]) => void; openFilament: () => void
}) {
  const [showSkipped, setShowSkipped] = useState(false)
  const checks = repo.analysis.readiness.checks
  const generated = repo.generated ?? {}
  const missing = [...new Set(checks.filter(c => c.status === 'fail' && c.generate && !generated[c.generate]).map(c => c.generate!))]
  const skipped = checks.filter(c => c.status === 'skip')
  const groups = useMemo(() => {
    const g: Record<string, Check[]> = {}
    for (const c of checks) if (c.status !== 'skip' || showSkipped) (g[c.kind ?? 'common'] ??= []).push(c)
    return Object.entries(g)
  }, [checks, showSkipped])

  return (
    <>
      {missing.length > 0 && (
        <section className="card callout">
          <div>
            <strong>Filament can create {missing.length} missing item{missing.length > 1 ? 's' : ''} for you.</strong>
            <p className="muted small">They're kept in Filament as references for agents and people. The repo isn't touched, and each rescan updates them as the code changes.</p>
          </div>
          <button disabled={!!busy} onClick={() => generate([])}>{busy === 'all' ? 'Creating…' : 'Create all in Filament'}</button>
        </section>
      )}
      {groups.map(([kind, cs]) => (
        <section key={kind} className="card">
          <h3>{KIND_LABEL[kind] ?? kind} checks</h3>
          <ul className="checks">
            {cs.map(c => {
              const made = c.generate ? generated[c.generate] : undefined
              return (
                <li key={c.id} className={`check check-${c.status}`}>
                  <span className="icon" aria-label={c.status}>{c.status === 'pass' ? '✔' : c.status === 'fail' ? '✘' : '–'}</span>
                  <div className="grow">
                    <div>{c.title}</div>
                    <div className="muted small">{c.status === 'fail' ? <>{c.detail} — <em>{c.fix}</em></> : c.detail}</div>
                  </div>
                  {c.status === 'fail' && c.generate && (made
                    ? <button className="link small" onClick={openFilament}>✓ In Filament</button>
                    : <button className="secondary small" disabled={!!busy} onClick={() => generate([c.generate!])}>
                        {busy === c.generate ? 'Creating…' : 'Create in Filament'}
                      </button>)}
                </li>
              )
            })}
          </ul>
        </section>
      ))}
      {skipped.length > 0 && (
        <button className="link small" onClick={() => setShowSkipped(s => !s)}>
          {showSkipped ? 'Hide' : 'Show'} {skipped.length} checks that don't apply to this kind of repo
        </button>
      )}
    </>
  )
}

const METHOD_ORDER = ['GET', 'POST', 'PUT', 'PATCH', 'DELETE']
function tagOf(e: Endpoint) {
  return e.tag || e.path.split('/').filter(s => s && !s.startsWith('{') && !/^(api|v\d+)$/.test(s))[0] || 'root'
}
function typeOf(s?: JsonSchema): string {
  if (!s) return 'string'
  if (typeof s.$ref === 'string') return s.$ref.split('/').pop() ?? 'object'
  if (s.type === 'array') return `${typeOf(s.items as JsonSchema)}[]`
  if (Array.isArray(s.enum)) return (s.enum as string[]).map(v => `"${v}"`).join(' | ')
  return String(s.type ?? 'object') + (s.format ? ` (${s.format})` : '')
}
const pretty = (v: unknown) => JSON.stringify(v, null, 2)

function ApiTab({ repo, busy, generate, impact, onChange }: {
  repo: RepoDetail; busy: string; generate: (items: string[]) => void; impact: Impact | null; onChange: (r: RepoDetail) => void
}) {
  const a = repo.analysis.api
  const [q, setQ] = useState('')
  const [openEp, setOpenEp] = useState<string | null>(null)
  const [enriching, setEnriching] = useState(false)
  const [error, setError] = useState('')
  const groups = useMemo(() => {
    const g: Record<string, Endpoint[]> = {}
    const needle = q.toLowerCase()
    for (const e of a.endpoints) {
      if (needle && !`${e.method} ${e.path} ${e.handler} ${e.summary}`.toLowerCase().includes(needle)) continue
      ;(g[tagOf(e)] ??= []).push(e)
    }
    for (const list of Object.values(g)) list.sort((x, y) => x.path.localeCompare(y.path) || METHOD_ORDER.indexOf(x.method) - METHOD_ORDER.indexOf(y.method))
    return Object.entries(g).sort(([x], [y]) => x.localeCompare(y))
  }, [a.endpoints, q])

  async function download() {
    const text = await api.repoOpenapi(repo.id)
    const url = URL.createObjectURL(new Blob([text], { type: 'application/yaml' }))
    const link = document.createElement('a')
    link.href = url; link.download = `${repo.name}-openapi.yaml`; link.click()
    URL.revokeObjectURL(url)
  }
  async function enrich() {
    setEnriching(true); setError('')
    try { onChange(await api.enrichApi(repo.id)) } catch (e) { setError(String(e)) } finally { setEnriching(false) }
  }

  if (a.endpoints.length === 0) {
    return <section className="card muted">No API found. Filament looks for OpenAPI, AsyncAPI, gRPC and GraphQL contracts and for routes in FastAPI, Flask, Django, Express, NestJS, Spring and Go code.</section>
  }
  const saved = !!repo.generated?.api_contract
  const drift = a.drift
  const driftCount = drift ? drift.in_code_not_in_contract.length + drift.in_contract_not_in_code.length : 0
  const models = Object.entries(a.schemas ?? {})
  const consumers = impact?.endpoint_consumers ?? {}
  return (
    <>
      <section className="card">
        <div className="row spread wrap">
          <div>
            <strong>{a.endpoints.length} endpoints</strong>{' '}
            <span className={`badge ${a.source === 'spec' ? 'badge-approved' : 'badge-draft'}`}>{a.source === 'spec' ? 'from contract' : 'inferred from code'}</span>
            {(a.auth ?? []).length > 0 && <span className="badge badge-in_review">auth: {(a.auth ?? []).join(', ')}</span>}
            <p className="muted small">{a.source === 'spec'
              ? `Read from ${a.specs.join(', ')}`
              : `No contract is checked in, so Filament read the routes, request and response models, auth and error codes from the code${models.length ? ` (${models.length} models)` : ''}.`}</p>
            {a.enrichment && <p className="muted small">Descriptions added by {a.enrichment.model} for {a.enrichment.applied} endpoints, {ago(a.enrichment.at)}.</p>}
          </div>
          <div className="row wrap">
            <button className="secondary" onClick={download}>Download OpenAPI</button>
            {!DEMO && <button className="secondary" disabled={enriching} onClick={enrich}>{enriching ? 'Describing…' : 'Describe with AI'}</button>}
            {!saved && <button disabled={!!busy} onClick={() => generate(['api_contract'])}>{busy === 'api_contract' ? 'Saving…' : 'Save contract in Filament'}</button>}
            {saved && <span className="small in-filament">✓ Contract kept in Filament</span>}
          </div>
        </div>
        <ErrorLine error={error} />
        <input value={q} onChange={e => setQ(e.target.value)} placeholder="Filter endpoints" />
      </section>
      {driftCount > 0 && drift && (
        <section className="card warn">
          <h3>Contract and code disagree ({driftCount})</h3>
          {drift.in_code_not_in_contract.length > 0 && <>
            <p className="small"><b>In the code but not the contract</b> (undocumented):</p>
            <ul className="plain small">{drift.in_code_not_in_contract.map(d => <li key={d.method + d.path}><code>{d.method} {d.path}</code> <span className="muted">{d.file}{d.line ? `:${d.line}` : ''}</span></li>)}</ul>
          </>}
          {drift.in_contract_not_in_code.length > 0 && <>
            <p className="small"><b>In the contract but not the code</b> (calls will fail):</p>
            <ul className="plain small">{drift.in_contract_not_in_code.map(d => <li key={d.method + d.path}><code>{d.method} {d.path}</code></li>)}</ul>
          </>}
        </section>
      )}
      {groups.map(([tag, eps]) => (
        <section key={tag} className="card">
          <h3>{tag}</h3>
          <ul className="endpoints">
            {eps.map(e => {
              const key = `${e.method} ${e.path}`
              const users = consumers[key] ?? []
              return (
                <li key={key}>
                  <button className="endpoint" onClick={() => setOpenEp(openEp === key ? null : key)}>
                    <span className={`method method-${e.method}`}>{e.method}</span>
                    <code>{e.path}</code>
                    {e.auth && <span title="Requires authentication" aria-label="Requires authentication">🔒</span>}
                    <span className="muted small grow">{e.summary}{users.length > 0 && <span className="used-by"> · used by {[...new Set(users.map(u => u.repo))].join(', ')}</span>}</span>
                  </button>
                  {openEp === key && <EndpointDetail repoId={repo.id} ep={e} users={users} />}
                </li>
              )
            })}
          </ul>
        </section>
      ))}
      {models.length > 0 && (
        <section className="card">
          <h3>Models</h3>
          {models.map(([name, s]) => {
            const props = Object.entries((s.properties ?? {}) as Record<string, JsonSchema>)
            const req = new Set((s.required ?? []) as string[])
            return (
              <details key={name} className="model">
                <summary><code>{name}</code> <span className="muted small">{props.length} fields</span></summary>
                <table className="fields"><tbody>
                  {props.map(([f, fs]) => <tr key={f}><td><code>{f}</code>{req.has(f) && <span className="req">*</span>}</td><td className="muted small">{typeOf(fs)}</td></tr>)}
                </tbody></table>
              </details>
            )
          })}
        </section>
      )}
    </>
  )
}

function EndpointDetail({ repoId, ep, users }: { repoId: string; ep: Endpoint; users: { repo: string; file: string; line: number }[] }) {
  const [tryResult, setTryResult] = useState<{ status: number; body: unknown } | null>(null)
  const [trying, setTrying] = useState(false)
  const query = ep.params.filter(p => p.in === 'query')
  const pathParams = ep.params.filter(p => (p.in || 'path') === 'path')
  async function tryIt() {
    setTrying(true)
    const path = ep.path.replace(/\{[^}]+\}/g, 'sample-1')
    try { setTryResult(await api.mockCall(repoId, ep.method, path, ep.example_request)) }
    catch (e) { setTryResult({ status: 0, body: String(e) }) }
    finally { setTrying(false) }
  }
  return (
    <div className="endpoint-detail small">
      {ep.description && <p>{ep.description}</p>}
      <div className="muted">
        {ep.handler && <>Handler <code>{ep.handler}</code>{ep.framework && <> · {ep.framework}</>} · </>}
        {ep.file}{ep.line ? `:${ep.line}` : ''}{ep.auth && <> · requires {ep.auth} auth</>}
      </div>
      {(pathParams.length > 0 || query.length > 0) && (
        <table className="fields"><tbody>
          {[...pathParams, ...query].map(p => (
            <tr key={p.in + p.name}><td><code>{p.name}</code>{(p.required ?? p.in === 'path') && <span className="req">*</span>}</td>
              <td className="muted">{p.in} · {typeOf(p.schema)}</td></tr>
          ))}
        </tbody></table>
      )}
      {ep.request && <div><b>Request</b> <code>{typeOf(ep.request.schema)}</code>{ep.example_request != null && <pre className="json">{pretty(ep.example_request)}</pre>}</div>}
      {ep.responses && (
        <div className="responses">
          <b>Responses</b>{' '}
          {Object.entries(ep.responses).map(([code, r]) => (
            <span key={code} className={`status status-${code[0]}`} title={r.description}>{code}{r.schema ? ` ${typeOf(r.schema)}` : ''}</span>
          ))}
        </div>
      )}
      {ep.example_response != null && !tryResult && <pre className="json">{pretty(ep.example_response)}</pre>}
      <div className="row wrap">
        <button className="secondary small" disabled={trying} onClick={tryIt}>{trying ? 'Calling…' : 'Try it (simulated)'}</button>
        {tryResult && <span className="muted">Mock server answered {tryResult.status}</span>}
      </div>
      {tryResult && <pre className="json">{pretty(tryResult.body)}</pre>}
      {users.length > 0 && (
        <div><b>Used by</b>{users.map((u, i) => <div key={i} className="muted">{u.repo} · {u.file}:{u.line}</div>)}</div>
      )}
    </div>
  )
}

function Outline({ items, depth = 0 }: { items: UiElement[]; depth?: number }) {
  return (
    <ul className="outline">
      {items.slice(0, 60).map((e, i) => (
        <li key={i}>
          <span className="el-type">{e.type}</span> {e.label}{e.repeated && <span className="muted"> ×n</span>}
          {e.children && depth < 1 && <Outline items={e.children} depth={depth + 1} />}
        </li>
      ))}
    </ul>
  )
}

function UiTab({ repo }: { repo: RepoDetail }) {
  const pages = repo.analysis.ui.pages
  const [idx, setIdx] = useState(0)
  const [outline, setOutline] = useState(false)
  if (pages.length === 0) {
    return <section className="card muted">No screens found. Filament reads React Router, Vue Router, Angular and Next.js routes, and falls back to pages/ and views/ folders.</section>
  }
  const page = pages[Math.min(idx, pages.length - 1)]
  const calls = (repo.analysis.client_calls ?? []).filter(c => page.file && c.file === page.file)
  return (
    <>
      <div className="chips">
        {pages.map((p, i) => (
          <button key={p.path + i} className={i === idx ? 'chip active' : 'chip'} onClick={() => setIdx(i)}>{p.path}</button>
        ))}
      </div>
      <section className="card">
        <div className="row spread wrap">
          <div><strong>{page.component}</strong> <span className="muted small">{page.router}{page.file && ` · ${page.file}`}</span></div>
          <button className="link small" onClick={() => setOutline(o => !o)}>{outline ? 'Show mock' : 'Show element list'}</button>
        </div>
        {outline
          ? <Outline items={page.elements} />
          : <iframe className="mock" title={`Mock of ${page.path}`} sandbox="" srcDoc={page.mock_html} />}
        {calls.length > 0 && <p className="small">Calls: {calls.map(c => <code key={c.method + c.path} className="ep-chip">{c.method} {c.path}</code>)}</p>}
        <p className="muted small">A wireframe built from the page's markup: real headings, buttons, inputs and child components, without styling or data.</p>
      </section>
    </>
  )
}

const CAT_LABEL: Record<string, string> = {
  datastore: 'Data store', cache: 'Cache', queue: 'Queue', service: 'Internal service', external: 'External service',
  cloud: 'Cloud', observability: 'Observability', compute: 'Compute', network: 'Network', security: 'Security', module: 'Module',
}

function ComponentsTab({ repo, impact, onOpen }: { repo: RepoDetail; impact: Impact | null; onOpen: (id: string) => void }) {
  const c = repo.analysis.components
  const nodes = c.nodes.filter(n => n.category !== 'self')
  const dependents = impact?.dependents ?? []
  const unresolved = (impact?.unresolved_calls ?? []).filter(u => u.repo_id === repo.id)
  return (
    <>
      <section className="card">
        <h3>Used by</h3>
        {dependents.length === 0
          ? <p className="muted small">No other onboarded repo depends on this one.</p>
          : <ul className="links">
              {dependents.map(d => (
                <li key={d.repo_id + d.kind}>
                  <div><button className="link" onClick={() => onOpen(d.repo_id)}>{d.repo}</button> <span className={`edge edge-${d.kind}`}>{d.kind}</span> this repo</div>
                  {d.endpoints.length > 0 && <div className="small">{d.endpoints.map(ep => <code key={ep} className="ep-chip">{ep}</code>)}</div>}
                </li>
              ))}
            </ul>}
        <p className="muted small">Change something here and these are what to check. Filament flags breaking API changes to them on every rescan.</p>
      </section>
      {unresolved.length > 0 && (
        <section className="card warn">
          <h3>This repo calls endpoints that don't exist</h3>
          <ul className="plain small">{unresolved.map((u, i) => <li key={i}><code>{u.method} {u.path}</code> on {u.target} · {u.file}:{u.line}</li>)}</ul>
        </section>
      )}
      {nodes.length === 0 ? <section className="card muted">No connections found yet.</section> : <>
        <section className="card">
          <p className="muted small">
            {Object.entries(c.summary).filter(([k]) => k !== 'self').map(([k, n]) => `${n} ${(CAT_LABEL[k] ?? k).toLowerCase()}`).join(' · ')}
          </p>
          <Mermaid code={c.mermaid} />
        </section>
        <section className="card">
          <h3>What it connects to, and why Filament thinks so</h3>
          <table className="deps">
            <thead><tr><th>Component</th><th>Type</th><th>Evidence</th></tr></thead>
            <tbody>
              {nodes.map(n => (
                <tr key={n.id}>
                  <td>{n.label}</td>
                  <td><span className={`cat cat-${n.category}`}>{CAT_LABEL[n.category] ?? n.category}</span></td>
                  <td className="muted small">
                    {n.evidence.slice(0, 2).map((ev, i) => <div key={i}><code>{ev.file}{ev.line ? `:${ev.line}` : ''}</code> {ev.detail}</div>)}
                    {n.evidence.length > 2 && <div>+{n.evidence.length - 2} more</div>}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </section>
      </>}
    </>
  )
}

const TRIGGER: Record<string, string> = { onboard: 'Onboarded', manual: 'Manual rescan', schedule: 'Scheduled rescan', webhook: 'Push to main' }

function HistoryTab({ scans }: { scans: RepoScan[] }) {
  if (scans.length === 0) return <section className="card muted">No scans yet.</section>
  return (
    <>
      <p className="muted small">Every scan is compared with the one before. Breaking API changes are flagged to the repos that use those endpoints.</p>
      {scans.map(s => {
        const ch = s.changes ?? { summary: '', breaking: [], counts: {} }
        const refs = ch.references ?? {}
        return (
          <section key={s.id} className={`card${ch.breaking?.length ? ' warn' : ''}`}>
            <div className="row spread wrap">
              <div><strong>{TRIGGER[s.trigger] ?? s.trigger}</strong> <span className="muted small">{new Date(s.scanned_at).toLocaleString()} · {ago(s.scanned_at)}{s.commit && <> · <code>{s.commit.slice(0, 7)}</code></>}</span></div>
              <span className={`grade-sm grade-${s.grade}`}>{s.grade}</span>
            </div>
            {s.status === 'failed'
              ? <p className="error small">Scan failed: {s.error}</p>
              : <p className="small">{ch.summary}{ch.score_delta ? ` · score ${ch.score_delta > 0 ? '+' : ''}${ch.score_delta}` : ''}</p>}
            {(ch.breaking ?? []).length > 0 && (
              <ul className="plain small">
                {ch.breaking.map((b, i) => (
                  <li key={i}><span className="breaking">breaking</span> <code>{b.endpoint}</code> — {b.change}
                    {b.consumers && b.consumers.length > 0 && <b> · used by {b.consumers.join(', ')}</b>}</li>
                ))}
              </ul>
            )}
            <ChangeList label="Endpoints added" items={ch.endpoints_added} />
            <ChangeList label="Endpoints removed" items={ch.endpoints_removed} />
            {(ch.endpoints_changed ?? []).length > 0 && (
              <div className="small"><b>Endpoints changed</b>
                <ul className="plain">{ch.endpoints_changed!.map(c => <li key={c.endpoint}><code>{c.endpoint}</code>: {c.changes.join('; ')}</li>)}</ul>
              </div>
            )}
            <ChangeList label="New connections" items={ch.connections_added} />
            <ChangeList label="Checks fixed" items={ch.checks_fixed} />
            <ChangeList label="Checks regressed" items={ch.checks_regressed} />
            {(refs.refreshed?.length || refs.pending_review?.length) ? (
              <p className="muted small">References in Filament: {refs.refreshed?.length ? `${refs.refreshed.length} refreshed` : ''}
                {refs.pending_review?.length ? `${refs.refreshed?.length ? ', ' : ''}${refs.pending_review.length} approved ones have updates to review` : ''}</p>
            ) : null}
          </section>
        )
      })}
    </>
  )
}

function ChangeList({ label, items }: { label: string; items?: string[] }) {
  if (!items?.length) return null
  return <div className="small"><b>{label}</b> {items.map(i => <code key={i} className="ep-chip">{i}</code>)}</div>
}

function FilamentTab({ repo }: { repo: RepoDetail }) {
  const [arts, setArts] = useState<Artifact[]>([])
  const [openId, setOpenId] = useState<string | null>(null)
  const [busy, setBusy] = useState('')
  const entries = Object.entries(repo.generated ?? {})
  const key = entries.map(([, id]) => id).join(',') + repo.scanned_at
  useEffect(() => {
    if (!entries.length) return
    Promise.all(entries.map(([, i]) => api.artifact(i))).then(setArts).catch(() => setArts([]))
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key])
  async function accept(item: string) {
    setBusy(item)
    try {
      const updated = await api.acceptReference(repo.id, item)
      setArts(as => as.map(a => (a.id === updated.id ? updated : a)))
    } finally { setBusy('') }
  }
  if (entries.length === 0) {
    return <section className="card muted">Nothing created yet. Use “Create in Filament” on a failing check to draft it here.</section>
  }
  return (
    <>
      <p className="muted small">References Filament keeps about this repo. They live in Filament's project graph and memory, not in the repo: agents use them now, and each rescan refreshes them from the code. Approved references aren't changed silently; you review the update first.</p>
      {arts.map(art => {
        const gen = String(art.data?.generator ?? '')
        const pending = art.data?.pending_update as { content: string; at: string } | undefined
        const refreshed = art.data?.refreshed_at as string | undefined
        return (
          <section key={art.id} className={`card${pending ? ' warn' : ''}`}>
            <div className="row spread wrap">
              <div><strong>{GEN_LABEL[gen] ?? art.title}</strong> <span className="muted small">v{art.version} · {art.status}{refreshed ? ` · refreshed ${ago(refreshed)}` : ''}</span></div>
              <button className="link small" onClick={() => setOpenId(openId === art.id ? null : art.id)}>{openId === art.id ? 'Hide' : 'Read'}</button>
            </div>
            {pending && (
              <div className="row spread wrap">
                <span className="small">The code changed since this was approved ({ago(pending.at)}).</span>
                <button className="small" disabled={busy === gen} onClick={() => accept(gen)}>{busy === gen ? 'Applying…' : 'Review update'}</button>
              </div>
            )}
            {openId === art.id && <Content text={art.content} />}
          </section>
        )
      })}
    </>
  )
}
