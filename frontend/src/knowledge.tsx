// Knowledge: admins connect sources (Confluence, docs sites, files, repo docs) that Ask and the
// agents search. Everything is indexed and stored on your own servers.
import { useCallback, useEffect, useState } from 'react'
import {
  api, DEMO,
  type KnowledgeDoc, type KnowledgeIndexInput, type KnowledgeOverview, type KnowledgeSource, type Passage,
  type RepoSummary, type SourceInput, type SourceKind, type Team, type TestResult,
} from './api'
import { ErrorLine } from './components'

const clean = (e: unknown) => String(e).replace(/^Error: (\d{3}: )?/, '')
const when = (s: string | null) => (s ? new Date(s).toLocaleString() : 'never')

const KINDS: { kind: SourceKind; name: string; blurb: string; example: Partial<SourceInput> }[] = [
  {
    kind: 'confluence', name: 'Confluence', blurb: 'Spaces from Confluence Cloud or Data Center, kept in sync.',
    example: { name: 'Engineering wiki', config: { base_url: 'https://acme.atlassian.net', spaces: ['ENG', 'OPS'], email: 'ignite-bot@acme.com', token_ref: '${env:CONFLUENCE_TOKEN}' } },
  },
  {
    kind: 'web', name: 'Docs site or wiki', blurb: 'Any internal website: crawled from start pages or a sitemap.',
    example: { name: 'Developer portal', config: { urls: ['https://docs.acme.internal/handbook/'], max_pages: 200 } },
  },
  { kind: 'upload', name: 'Upload files', blurb: 'PDF, Word, Markdown, HTML or text files.', example: { name: 'Runbooks', config: {} } },
  { kind: 'repo', name: 'Docs in repos', blurb: 'READMEs, docs/ folders and ADRs from onboarded repos.', example: { name: 'Repo docs', config: { repo_ids: [] } } },
]
const AGENTS = ['requirements', 'flow', 'triage', 'coding', 'qa', 'review', 'release', 'sre']

export function KnowledgePage({ ws }: { ws: string }) {
  const [data, setData] = useState<KnowledgeOverview | null>(null)
  const [open, setOpen] = useState<string | null>(null)
  const [adding, setAdding] = useState<SourceKind | null>(null)
  const [error, setError] = useState('')
  const load = useCallback(() => api.knowledge(ws).then(setData).catch(e => setError(clean(e))), [ws])
  useEffect(() => { load() }, [load])
  useEffect(() => { // follow syncs in progress
    if (!data?.sources.some(s => s.status === 'syncing')) return
    const t = setTimeout(load, 2500)
    return () => clearTimeout(t)
  }, [data, load])
  if (open) return <SourceView id={open} ws={ws} onBack={() => { setOpen(null); load() }} />
  return (
    <div className="page">
      <h1>Knowledge</h1>
      <p className="muted">Connect where your team's knowledge lives. Ignite reads it into searchable passages that Ask and the agents use, with links back to the original. It's all stored in Ignite's own database, and people only find passages from sources they're allowed to read.</p>
      <ErrorLine error={error} />
      <section className="card">
        <h3>Sources</h3>
        {data && data.sources.length === 0 && <p className="muted small">No sources yet. Add one below.</p>}
        <ul className="plain sources-list">
          {data?.sources.map(s => (
            <li key={s.id}>
              <button className="link source-name" onClick={() => setOpen(s.id)}>{s.name}</button>
              <span className={`badge status-${s.status}`}>{s.status === 'ok' ? 'up to date' : s.status}</span>
              <div className="muted small">{KINDS.find(k => k.kind === s.kind)?.name} · {s.doc_count} documents · {s.chunk_count} passages · used {s.hits} times · synced {when(s.last_sync_at)}{s.audience === 'some' ? ' · limited audience' : ''}{s.agents.length ? ` · agents: ${s.agents.join(', ')}` : ''}</div>
              {s.last_error && <div className="error small">{s.last_error}</div>}
            </li>
          ))}
        </ul>
      </section>
      <section className="card">
        <h3>Add a source</h3>
        <div className="kind-grid">
          {KINDS.map(k => (
            <button key={k.kind} className={`kind ${adding === k.kind ? 'active' : ''}`} onClick={() => setAdding(adding === k.kind ? null : k.kind)}>
              <b>{k.name}</b><span className="muted small">{k.blurb}</span>
            </button>
          ))}
        </div>
        {adding && <SourceForm ws={ws} kind={adding} onDone={s => { setAdding(null); setOpen(s.id) }} />}
      </section>
      <TryIt ws={ws} />
      {data && <IndexSettings ws={ws} index={data.index} onSaved={load} />}
    </div>
  )
}

function SourceForm({ ws, kind, source, onDone }: { ws: string; kind: SourceKind; source?: KnowledgeSource; onDone: (s: KnowledgeSource) => void }) {
  const ex = KINDS.find(k => k.kind === kind)!.example
  const [name, setName] = useState(source?.name ?? '')
  const [cfg, setCfg] = useState<Record<string, unknown>>(source?.config ?? {})
  const [audience, setAudience] = useState(source?.audience ?? 'everyone')
  const [teamsSel, setTeamsSel] = useState<string[]>(source?.teams ?? [])
  const [groups, setGroups] = useState((source?.groups ?? []).join(', '))
  const [agents, setAgents] = useState<string[]>(source?.agents ?? [])
  const [hours, setHours] = useState(source?.sync_hours ?? (kind === 'upload' ? 0 : 24))
  const [teams, setTeams] = useState<Team[]>([])
  const [repos, setRepos] = useState<RepoSummary[]>([])
  const [error, setError] = useState('')
  useEffect(() => { api.teams(ws).then(setTeams).catch(() => {}); if (kind === 'repo') api.repos(ws).then(setRepos).catch(() => {}) }, [ws, kind])
  const exCfg = (ex.config ?? {}) as Record<string, unknown>
  const text = (k: string) => (Array.isArray(cfg[k]) ? (cfg[k] as string[]).join(', ') : (cfg[k] as string) ?? '')
  const ph = (k: string) => (Array.isArray(exCfg[k]) ? (exCfg[k] as string[]).join(', ') : String(exCfg[k] ?? ''))
  const setText = (k: string, list = false) => (e: React.ChangeEvent<HTMLInputElement>) =>
    setCfg(c => ({ ...c, [k]: list ? e.target.value.split(',').map(x => x.trim()).filter(Boolean) : e.target.value }))
  const field = (k: string, label: string, list = false, help?: string) => (
    <label className="setting">{label}<input value={text(k)} placeholder={ph(k)} onChange={setText(k, list)} />{help && <span className="muted small">{help}</span>}</label>
  )
  async function save() {
    setError('')
    const body = { name: name || String(ex.name), kind, config: cfg, audience, teams: audience === 'some' ? teamsSel : [], groups: audience === 'some' ? groups.split(',').map(g => g.trim()).filter(Boolean) : [], users: [], project_id: null, agents, sync_hours: hours, enabled: true } as SourceInput
    try { onDone(source ? await api.updateSource(source.id, body) : await api.addSource(ws, body)) } catch (e) { setError(clean(e)) }
  }
  return (
    <div className="source-form">
      <div className="settings-grid">
        <label className="setting">Name<input value={name} placeholder={String(ex.name)} onChange={e => setName(e.target.value)} /></label>
        {kind === 'confluence' && <>
          {field('base_url', 'Confluence address', false, 'Cloud: https://yourco.atlassian.net · Data Center: https://confluence.yourco.com')}
          {field('spaces', 'Space keys (comma-separated)', true)}
          {field('email', 'Account email (Cloud only)')}
          {field('token_ref', 'API token', false, 'Set it on the server and enter ${env:NAME}. Cloud: an API token · Data Center: a personal access token')}
          {field('cql', 'Narrow with CQL (optional)', false, 'e.g. label = "runbook" or ancestor = 12345')}
        </>}
        {kind === 'web' && <>
          {field('urls', 'Start pages (comma-separated)', true, 'Ignite follows links on the same site under these paths')}
          {field('sitemap', 'Or a sitemap (optional)')}
          <label className="setting">Pages at most<input type="number" min={1} value={String(cfg.max_pages ?? 200)} onChange={e => setCfg(c => ({ ...c, max_pages: Number(e.target.value) }))} /></label>
          {field('exclude', 'Skip URLs containing (optional)', true)}
          {field('token_ref', 'Bearer token for a signed-in site (optional)', false, '${env:NAME}')}
        </>}
        {kind === 'repo' && (
          <div className="setting">Repos
            <div className="chips">{repos.map(r => <label key={r.id} className="check"><input type="checkbox" checked={((cfg.repo_ids as string[]) ?? []).includes(r.id)} onChange={e => setCfg(c => ({ ...c, repo_ids: e.target.checked ? [...((c.repo_ids as string[]) ?? []), r.id] : ((c.repo_ids as string[]) ?? []).filter(x => x !== r.id) }))} /> {r.name}</label>)}</div>
            {repos.length === 0 && <span className="muted small">Onboard repos in Launchpad first.</span>}
            <span className="muted small">Reads README files, docs/, doc/, adr/ and runbooks/. Each repo's docs follow that repo's visibility.</span>
          </div>
        )}
        {kind !== 'upload' && <label className="setting">Sync<select value={hours} onChange={e => setHours(Number(e.target.value))}>
          <option value={0}>only when I click Sync</option><option value={6}>every 6 hours</option><option value={24}>daily</option><option value={168}>weekly</option></select></label>}
        <label className="setting">Who can find it<select value={audience} onChange={e => setAudience(e.target.value as 'everyone' | 'some')}>
          <option value="everyone">everyone in the workspace</option><option value="some">only some teams or groups</option></select></label>
      </div>
      {audience === 'some' && (
        <div className="feature-edit">
          <div className="chips">{teams.map(t => <label key={t.id} className="check"><input type="checkbox" checked={teamsSel.includes(t.id)} onChange={() => setTeamsSel(s => s.includes(t.id) ? s.filter(x => x !== t.id) : [...s, t.id])} /> {t.name}</label>)}</div>
          <label className="setting">Directory or SSO groups (comma-separated)<input value={groups} onChange={e => setGroups(e.target.value)} placeholder="eng-payments" /></label>
        </div>
      )}
      <div className="setting">Agents that may use it <span className="muted small">(none ticked: all of them)</span>
        <div className="chips">{AGENTS.map(a => <label key={a} className="check"><input type="checkbox" checked={agents.includes(a)} onChange={() => setAgents(s => s.includes(a) ? s.filter(x => x !== a) : [...s, a])} /> {a}</label>)}</div>
      </div>
      <ErrorLine error={error} />
      <button onClick={save}>{source ? 'Save' : 'Add source'}</button>
    </div>
  )
}

function SourceView({ id, ws, onBack }: { id: string; ws: string; onBack: () => void }) {
  const [s, setS] = useState<(KnowledgeSource & { docs: KnowledgeDoc[] }) | null>(null)
  const [edit, setEdit] = useState(false)
  const [test, setTest] = useState<{ ok: boolean; message: string; sample: { title: string; url: string | null }[] } | null>(null)
  const [upload, setUpload] = useState<{ file: string; passages?: number; error?: string }[] | null>(null)
  const [error, setError] = useState('')
  const load = useCallback(() => api.source(id).then(setS).catch(e => setError(clean(e))), [id])
  useEffect(() => { load() }, [load])
  useEffect(() => { if (s?.status !== 'syncing') return; const t = setTimeout(load, 2000); return () => clearTimeout(t) }, [s, load])
  if (!s) return <div className="page"><ErrorLine error={error} /></div>
  const r = s.last_report
  return (
    <div className="page">
      <button className="link" onClick={onBack}>← Knowledge</button>
      <h1>{s.name} <span className={`badge status-${s.status}`}>{s.status === 'ok' ? 'up to date' : s.status}</span></h1>
      <p className="muted small">{KINDS.find(k => k.kind === s.kind)?.name} · {s.doc_count} documents · {s.chunk_count} passages · used {s.hits} times · last synced {when(s.last_sync_at)}{s.next_sync_at ? ` · next ${when(s.next_sync_at)}` : ''}</p>
      <ErrorLine error={error || s.last_error || ''} />
      <div className="row">
        {s.kind !== 'upload' && <button disabled={s.status === 'syncing'} onClick={async () => { await api.syncSource(s.id); load() }}>{s.status === 'syncing' ? 'Syncing…' : 'Sync now'}</button>}
        {s.kind !== 'upload' && <button className="secondary" onClick={async () => setTest(await api.testSource(s.id))}>Test connection</button>}
        <button className="secondary" onClick={() => setEdit(!edit)}>{edit ? 'Close settings' : 'Settings'}</button>
        <button className="link danger" onClick={async () => { if (confirm(`Remove ${s.name} and its ${s.chunk_count} passages?`)) { await api.deleteSource(s.id); onBack() } }}>Remove</button>
      </div>
      {test && <section className={`card ${test.ok ? '' : 'warn'}`}><p className={test.ok ? 'ok' : 'error'}>{test.message}</p><ul className="plain small">{test.sample.map((x, i) => <li key={i}>{x.url ? <a href={x.url} target="_blank" rel="noreferrer">{x.title}</a> : x.title}</li>)}</ul></section>}
      {r && (r.added || r.updated || r.removed || r.unchanged || r.failed?.length) ? (
        <p className="small">Last sync: {r.added ?? 0} added, {r.updated ?? 0} updated, {r.unchanged ?? 0} unchanged, {r.removed ?? 0} removed{r.failed?.length ? `, ${r.failed.length} failed` : ''}.</p>
      ) : null}
      {r?.failed?.length ? <details className="small"><summary>Documents that failed</summary><ul>{r.failed.map((f, i) => <li key={i}><b>{f.title}</b>: {f.error}</li>)}</ul></details> : null}
      {edit && <section className="card"><SourceForm ws={ws} kind={s.kind} source={s} onDone={() => { setEdit(false); load() }} /></section>}
      {s.kind === 'upload' && (
        <section className="card">
          <h3>Add files</h3>
          <input type="file" multiple accept=".pdf,.docx,.md,.markdown,.html,.htm,.txt,.rst,.adoc" onChange={async e => {
            const files = Array.from(e.target.files ?? [])
            if (!files.length) return
            try { const out = await api.uploadFiles(s.id, files); setUpload(out.results); load() } catch (err) { setError(clean(err)) }
            e.target.value = ''
          }} />
          <p className="muted small">PDF, Word (.docx), Markdown, HTML or text, up to 25 MB each.</p>
          {upload && <ul className="plain small">{upload.map(u => <li key={u.file} className={u.error ? 'error' : ''}>{u.file}: {u.error ?? `${u.passages} passages`}</li>)}</ul>}
        </section>
      )}
      <TryIt ws={ws} sourceId={s.id} />
      <section className="card">
        <h3>Documents</h3>
        {s.docs.length === 0 && <p className="muted small">Nothing yet{s.kind !== 'upload' ? ': click Sync now' : ''}.</p>}
        <ul className="plain docs">
          {s.docs.map(d => (
            <li key={d.id} className="row nowrap spread">
              <span className="grow">{d.url ? <a href={d.url} target="_blank" rel="noreferrer">{d.title}</a> : d.title} <span className="muted small">· {d.passages} passages</span></span>
              <button className="link small" onClick={async () => { await api.deleteDoc(s.id, d.id); load() }}>remove</button>
            </li>
          ))}
        </ul>
      </section>
    </div>
  )
}

function TryIt({ ws, sourceId }: { ws: string; sourceId?: string }) {
  const [q, setQ] = useState('')
  const [res, setRes] = useState<Passage[] | null>(null)
  const [error, setError] = useState('')
  async function go() {
    try { setRes((await api.searchKnowledge(ws, q, sourceId ? [sourceId] : undefined)).results); setError('') } catch (e) { setError(clean(e)) }
  }
  return (
    <section className="card">
      <h3>Try a question</h3>
      <p className="muted small">See exactly which passages Ask and the agents would get{sourceId ? ' from this source' : ''}.</p>
      <div className="row">
        <input value={q} onChange={e => setQ(e.target.value)} onKeyDown={e => e.key === 'Enter' && q.trim() && go()} placeholder="How do we roll back a release?" />
        <button className="secondary" disabled={!q.trim()} onClick={go}>Search</button>
      </div>
      <ErrorLine error={error} />
      {res && res.length === 0 && <p className="muted small">No passages match.</p>}
      {res && res.map(p => (
        <div key={p.id} className="passage">
          <div className="small"><b>{p.url ? <a href={p.url} target="_blank" rel="noreferrer">{p.heading || p.title}</a> : (p.heading || p.title)}</b> <span className="muted">· {p.source} · {p.match}</span></div>
          <p className="small">{p.text.length > 400 ? p.text.slice(0, 400) + '…' : p.text}</p>
        </div>
      ))}
    </section>
  )
}

function IndexSettings({ ws, index, onSaved }: { ws: string; index: KnowledgeOverview['index']; onSaved: () => void }) {
  const [f, setF] = useState<KnowledgeIndexInput>({ provider: index.provider, model: index.model, model_path: index.model_path, endpoint_url: index.endpoint_url, api_key_ref: index.api_key_ref })
  const [msg, setMsg] = useState<TestResult | null>(null)
  const set = (k: keyof KnowledgeIndexInput) => (e: React.ChangeEvent<HTMLInputElement | HTMLSelectElement>) => setF(x => ({ ...x, [k]: e.target.value || null }))
  return (
    <section className="card">
      <h3>Search by meaning</h3>
      <p className="muted small">Keyword search always works. To also match questions worded differently from the docs, turn on an embedding model. It runs on your own servers, so documents never leave your network.</p>
      <div className="settings-grid">
        <label className="setting">Embeddings<select value={f.provider} onChange={set('provider')}>
          <option value="none">off (keyword search only)</option>
          <option value="local">a model inside Ignite</option>
          <option value="endpoint">a model server you run (Ollama, TEI, vLLM)</option>
        </select></label>
        {f.provider !== 'none' && <label className="setting">Model<input value={f.model ?? ''} onChange={set('model')} placeholder={f.provider === 'local' ? 'BAAI/bge-small-en-v1.5' : 'nomic-embed-text'} /></label>}
        {f.provider === 'local' && <label className="setting">Model folder on the server (optional)<input value={f.model_path ?? ''} onChange={set('model_path')} placeholder="/opt/models/bge-small-en-v1.5" /><span className="muted small">{index.fastembed ? 'Downloaded once if not set. Set it if the server can’t reach the internet.' : 'The server needs the fastembed package: pip install "igniteai[knowledge]".'}</span></label>}
        {f.provider === 'endpoint' && <>
          <label className="setting">Server URL<input value={f.endpoint_url ?? ''} onChange={set('endpoint_url')} placeholder="http://ollama.internal:11434/v1" /></label>
          <label className="setting">Key (optional)<input value={f.api_key_ref ?? ''} onChange={set('api_key_ref')} placeholder="${env:EMBEDDINGS_KEY}" /></label>
        </>}
      </div>
      <div className="row">
        <button className="secondary" onClick={async () => { try { setMsg(await api.testKnowledgeIndex(ws, f)) } catch (e) { setMsg({ ok: false, message: clean(e) }) } }}>Test</button>
        <button onClick={async () => { try { const r = await api.setKnowledgeIndex(ws, f); setMsg({ ok: true, message: r.reembedding ? 'Saved. Existing passages are being re-indexed in the background.' : 'Saved.' }); onSaved() } catch (e) { setMsg({ ok: false, message: clean(e) }) } }}>Save</button>
      </div>
      {msg && <p className={`small ${msg.ok ? 'ok' : 'error'}`}>{msg.message}</p>}
      {DEMO && <p className="muted small">Preview: the sample sources use keyword search.</p>}
    </section>
  )
}
