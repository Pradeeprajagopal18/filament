// Launchpad: onboard any repo (app, API, frontend, infrastructure, config, library), see what it is,
// what it needs, its API contract, mock UI and component view, and create missing pieces in Filament.
import { useEffect, useMemo, useState } from 'react'
import { api } from './api'
import type { Artifact, Check, Endpoint, RepoDetail, RepoSummary, UiElement } from './api'
import { Content, ErrorLine, Mermaid } from './components'

const KIND_LABEL: Record<string, string> = {
  frontend: 'Frontend', backend: 'Backend', api: 'API', infrastructure: 'Infrastructure', config: 'Config',
  mobile: 'Mobile', data: 'Data', library: 'Library', docs: 'Docs', monorepo: 'Monorepo', common: 'Every repo',
}
const GEN_LABEL: Record<string, string> = {
  agents_md: 'Agent instructions', overview: 'Overview', architecture: 'Architecture & components',
  codeowners: 'Ownership', api_contract: 'API contract (OpenAPI)',
}

function Kinds({ kinds }: { kinds: string[] }) {
  return <span className="kinds">{kinds.map(k => <span key={k} className={`kind kind-${k}`}>{KIND_LABEL[k] ?? k}</span>)}</span>
}

export function LaunchpadPage({ ws }: { ws: string }) {
  const [ref, setRef] = useState('')
  const [repos, setRepos] = useState<RepoSummary[]>([])
  const [current, setCurrent] = useState<RepoDetail | null>(null)
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

  if (current) return <RepoView repo={current} onBack={() => setCurrent(null)} onChange={updated} />

  return (
    <div className="page">
      <h1>Launchpad</h1>
      <p className="muted">Onboard any repo: an app, an API, a frontend, infrastructure or config. Filament works out what it is, checks what matters for that kind of repo, and documents its API, screens and dependencies.</p>
      <section className="card row">
        <input value={ref} onChange={e => setRef(e.target.value)} placeholder="github.com/acme/checkout-api or /path/to/repo"
          onKeyDown={e => e.key === 'Enter' && ref.trim() && !busy && onboard()} />
        <button disabled={!ref.trim() || busy} onClick={onboard}>{busy ? 'Analyzing…' : 'Onboard repo'}</button>
      </section>
      <ErrorLine error={error} />
      {repos.length === 0 && <section className="card muted">No repos yet. Onboard one above.</section>}
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
              {made > 0 && <span className="small in-filament">{made} item{made > 1 ? 's' : ''} created in Filament</span>}
            </button>
          )
        })}
      </div>
    </div>
  )
}

type Tab = 'readiness' | 'api' | 'ui' | 'components' | 'filament'

function RepoView({ repo, onBack, onChange }: { repo: RepoDetail; onBack: () => void; onChange: (r: RepoDetail) => void }) {
  const a = repo.analysis
  const tabs: [Tab, string, number | null][] = [
    ['readiness', 'Readiness', a.readiness.checks.filter(c => c.status === 'fail').length],
    ['api', 'API', a.api.endpoints.length || null],
    ['ui', 'UI', a.ui.pages.length || null],
    ['components', 'Components', a.components.nodes.filter(n => n.category !== 'self').length || null],
    ['filament', 'In Filament', Object.keys(repo.generated ?? {}).length || null],
  ]
  const [tab, setTab] = useState<Tab>('readiness')
  const [busy, setBusy] = useState('')
  const [error, setError] = useState('')

  async function generate(items: string[]) {
    setBusy(items.join(',') || 'all'); setError('')
    try { onChange(await api.generateRepo(repo.id, items)) } catch (e) { setError(String(e)) } finally { setBusy('') }
  }
  async function rescan() {
    setBusy('rescan'); setError('')
    try { onChange(await api.rescanRepo(repo.id)) } catch (e) { setError(String(e)) } finally { setBusy('') }
  }

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
          {' · '}scanned {new Date(repo.scanned_at).toLocaleString()}
        </p>
        <details className="small">
          <summary>Why Filament thinks this is {repo.kinds.map(k => KIND_LABEL[k] ?? k).join(' + ')}</summary>
          <ul>{a.profile.kinds.map(k => <li key={k.kind}><b>{KIND_LABEL[k.kind] ?? k.kind}</b>: {k.evidence.slice(0, 3).join('; ')}</li>)}</ul>
        </details>
        <button className="secondary" disabled={!!busy} onClick={rescan}>{busy === 'rescan' ? 'Rescanning…' : 'Rescan'}</button>
      </section>
      <ErrorLine error={error} />
      <div className="tabs" role="tablist">
        {tabs.map(([t, label, n]) => (
          <button key={t} role="tab" aria-selected={tab === t} className={tab === t ? 'active' : ''} onClick={() => setTab(t)}>
            {label}{n != null && <span className="count">{n}</span>}
          </button>
        ))}
      </div>
      {tab === 'readiness' && <ReadinessTab repo={repo} busy={busy} generate={generate} openFilament={() => setTab('filament')} />}
      {tab === 'api' && <ApiTab repo={repo} busy={busy} generate={generate} />}
      {tab === 'ui' && <UiTab repo={repo} />}
      {tab === 'components' && <ComponentsTab repo={repo} />}
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
            <p className="muted small">Drafts live in Filament's graph and memory, so agents and people can use them now. Nothing is written to the repo until you export them.</p>
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

function ApiTab({ repo, busy, generate }: { repo: RepoDetail; busy: string; generate: (items: string[]) => void }) {
  const a = repo.analysis.api
  const [q, setQ] = useState('')
  const [openEp, setOpenEp] = useState<string | null>(null)
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

  if (a.endpoints.length === 0) {
    return <section className="card muted">No API found. Filament looks for OpenAPI, AsyncAPI, gRPC and GraphQL contracts and for routes in FastAPI, Flask, Django, Express, NestJS, Spring and Go code.</section>
  }
  const saved = !!repo.generated?.api_contract
  return (
    <>
      <section className="card">
        <div className="row spread wrap">
          <div>
            <strong>{a.endpoints.length} endpoints</strong>{' '}
            <span className={`badge ${a.source === 'spec' ? 'badge-approved' : 'badge-draft'}`}>{a.source === 'spec' ? 'from contract' : 'inferred from code'}</span>
            <p className="muted small">{a.source === 'spec' ? `Read from ${a.specs.join(', ')}` : 'No contract is checked in, so Filament read the route definitions and generated an OpenAPI document.'}</p>
          </div>
          <div className="row">
            <button className="secondary" onClick={download}>Download OpenAPI</button>
            {!saved && <button disabled={!!busy} onClick={() => generate(['api_contract'])}>{busy === 'api_contract' ? 'Saving…' : 'Save contract in Filament'}</button>}
            {saved && <span className="small in-filament">✓ Contract saved in Filament</span>}
          </div>
        </div>
        <input value={q} onChange={e => setQ(e.target.value)} placeholder="Filter endpoints" />
      </section>
      {groups.map(([tag, eps]) => (
        <section key={tag} className="card">
          <h3>{tag}</h3>
          <ul className="endpoints">
            {eps.map(e => {
              const key = `${e.method} ${e.path}`
              return (
                <li key={key}>
                  <button className="endpoint" onClick={() => setOpenEp(openEp === key ? null : key)}>
                    <span className={`method method-${e.method}`}>{e.method}</span>
                    <code>{e.path}</code>
                    <span className="muted small grow">{e.summary}</span>
                  </button>
                  {openEp === key && (
                    <div className="endpoint-detail small">
                      {e.handler && <div>Handler <code>{e.handler}</code>{e.framework && <> · {e.framework}</>}</div>}
                      {e.file && <div className="muted">{e.file}{e.line ? `:${e.line}` : ''}</div>}
                      {e.params.length > 0 && <div>Parameters: {e.params.map(p => <code key={p.name}>{p.name} ({p.in})</code>)}</div>}
                    </div>
                  )}
                </li>
              )
            })}
          </ul>
        </section>
      ))}
    </>
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
        <p className="muted small">A wireframe built from the page's markup: real headings, buttons, inputs and child components, without styling or data.</p>
      </section>
    </>
  )
}

const CAT_LABEL: Record<string, string> = {
  datastore: 'Data store', cache: 'Cache', queue: 'Queue', service: 'Internal service', external: 'External service',
  cloud: 'Cloud', observability: 'Observability', compute: 'Compute', network: 'Network', security: 'Security', module: 'Module',
}

function ComponentsTab({ repo }: { repo: RepoDetail }) {
  const c = repo.analysis.components
  const nodes = c.nodes.filter(n => n.category !== 'self')
  if (nodes.length === 0) return <section className="card muted">No connections found yet.</section>
  return (
    <>
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
    </>
  )
}

function FilamentTab({ repo }: { repo: RepoDetail }) {
  const [arts, setArts] = useState<Artifact[]>([])
  const [openId, setOpenId] = useState<string | null>(null)
  const ids = Object.values(repo.generated ?? {})
  const key = ids.join(',')
  useEffect(() => {
    if (!key) return
    Promise.all(key.split(',').map(i => api.artifact(i))).then(setArts).catch(() => setArts([]))
  }, [key])
  if (ids.length === 0) {
    return <section className="card muted">Nothing created yet. Use “Create in Filament” on a failing check to draft it here.</section>
  }
  return (
    <>
      <p className="muted small">Drafts kept in Filament's project graph, not in the repo yet. Agents read them now; approve them and export when you want them committed. Conventions Filament found were proposed to project memory for review.</p>
      {arts.map(art => {
        const gen = String(art.data?.generator ?? '')
        return (
          <section key={art.id} className="card">
            <div className="row spread">
              <div><strong>{GEN_LABEL[gen] ?? art.title}</strong> <span className="muted small">v{art.version} · {art.status} · not in repo</span></div>
              <button className="link small" onClick={() => setOpenId(openId === art.id ? null : art.id)}>{openId === art.id ? 'Hide' : 'Read'}</button>
            </div>
            {openId === art.id && <Content text={art.content} />}
          </section>
        )
      })}
    </>
  )
}
