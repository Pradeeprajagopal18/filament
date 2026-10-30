import { useCallback, useEffect, useState } from 'react'
import { api, type AgentSetting, type McpServer, type Runner } from './api'
import { Badge, ErrorLine } from './components'

const list = (s: string) => s.split(',').map(x => x.trim()).filter(Boolean)

function parseKv(text: string): Record<string, string> {
  const out: Record<string, string> = {}
  for (const line of text.split('\n')) {
    const i = line.indexOf('=')
    if (i > 0) out[line.slice(0, i).trim()] = line.slice(i + 1).trim()
  }
  return out
}
const kvText = (o: Record<string, string>) => Object.entries(o).map(([k, v]) => `${k}=${v}`).join('\n')

// -- Connectors (MCP servers) ----------------------------------------------------------------
const EMPTY = { name: '', description: '', transport: 'stdio' as 'stdio' | 'http', command: '', args: '', url: '', env: '', headers: '', allowed: '', writes: '', minAutonomy: 'L2', scope: 'workspace', scopeRef: '' }

export function ConnectorsPage({ ws }: { ws: string }) {
  const [servers, setServers] = useState<McpServer[]>([])
  const [form, setForm] = useState(EMPTY)
  const [editing, setEditing] = useState<string | null>(null)
  const [open, setOpen] = useState(false)
  const [busy, setBusy] = useState('')
  const [error, setError] = useState('')
  const load = useCallback(() => api.mcpServers(ws).then(setServers), [ws])
  useEffect(() => { load() }, [load])

  const set = (k: keyof typeof EMPTY) => (e: { target: { value: string } }) => setForm(f => ({ ...f, [k]: e.target.value }))

  function edit(s: McpServer) {
    setEditing(s.id); setOpen(true); setError('')
    setForm({
      name: s.name, description: s.description, transport: s.transport, command: s.command ?? '', args: s.args.join(' '),
      url: s.url ?? '', env: kvText(s.env), headers: kvText(s.headers), allowed: s.allowed_tools.join(', '),
      writes: s.write_tools.join(', '), minAutonomy: s.min_autonomy_for_writes, scope: s.scope, scopeRef: s.scope_ref ?? '',
    })
  }

  async function save() {
    setBusy('save'); setError('')
    const body = {
      description: form.description, command: form.transport === 'stdio' ? form.command : null,
      args: form.transport === 'stdio' ? form.args.split(' ').filter(Boolean) : [],
      url: form.transport === 'http' ? form.url : null, env: parseKv(form.env), headers: parseKv(form.headers),
      allowed_tools: list(form.allowed), write_tools: list(form.writes), min_autonomy_for_writes: form.minAutonomy,
    }
    try {
      if (editing) {
        await api.updateMcpServer(editing, body)
        await api.testMcpServer(editing)
      } else {
        await api.addMcpServer(ws, { ...body, name: form.name, transport: form.transport, scope: form.scope, scope_ref: form.scopeRef || null })
      }
      setForm(EMPTY); setEditing(null); setOpen(false)
      await load()
    } catch (e) { setError(String(e)) } finally { setBusy('') }
  }

  async function act(s: McpServer, action: 'test' | 'toggle' | 'delete') {
    setBusy(s.id + action)
    try {
      if (action === 'test') await api.testMcpServer(s.id)
      if (action === 'toggle') await api.updateMcpServer(s.id, { enabled: !s.enabled })
      if (action === 'delete') await api.deleteMcpServer(s.id)
      await load()
    } finally { setBusy('') }
  }

  return (
    <div className="page">
      <div className="row spread">
        <div>
          <h1>Connectors (MCP)</h1>
          <p className="muted">MCP servers agents can use. Scope them to the workspace, a team or a project; the narrowest scope wins.</p>
        </div>
        {!open && <button onClick={() => { setOpen(true); setEditing(null); setForm(EMPTY) }}>Add server</button>}
      </div>

      {open && (
        <section className="card form">
          <h2>{editing ? `Edit ${form.name}` : 'Add an MCP server'}</h2>
          <div className="grid2">
            <label>Name<input value={form.name} onChange={set('name')} disabled={!!editing} placeholder="github" /></label>
            <label>Transport
              <select value={form.transport} onChange={set('transport')} disabled={!!editing}>
                <option value="stdio">stdio (command)</option><option value="http">HTTP (URL)</option>
              </select>
            </label>
            {form.transport === 'stdio' ? (<>
              <label>Command<input value={form.command} onChange={set('command')} placeholder="npx" /></label>
              <label>Arguments<input value={form.args} onChange={set('args')} placeholder="-y @modelcontextprotocol/server-github" /></label>
            </>) : (
              <label className="span2">URL<input value={form.url} onChange={set('url')} placeholder="https://mcp.example.com/mcp" /></label>
            )}
            <label>{form.transport === 'stdio' ? 'Environment' : 'Headers'} <span className="muted small">KEY=VALUE per line; secrets as ${'{'}env:NAME{'}'}</span>
              <textarea rows={3} value={form.transport === 'stdio' ? form.env : form.headers} onChange={set(form.transport === 'stdio' ? 'env' : 'headers')}
                placeholder={form.transport === 'stdio' ? 'GITHUB_TOKEN=${env:GITHUB_TOKEN}' : 'Authorization=${env:GITHUB_TOKEN}'} />
            </label>
            <label>Description<textarea rows={3} value={form.description} onChange={set('description')} placeholder="What agents should use it for" /></label>
            <label>Only expose these tools <span className="muted small">comma-separated; empty = all</span><input value={form.allowed} onChange={set('allowed')} placeholder="search_issues, get_file" /></label>
            <label>Write tools <span className="muted small">tools that change things</span><input value={form.writes} onChange={set('writes')} placeholder="create_issue, create_pull_request" /></label>
            <label>Write tools need autonomy
              <select value={form.minAutonomy} onChange={set('minAutonomy')}>
                <option value="L1">L1 · drafts</option><option value="L2">L2 · review</option><option value="L3">L3 · act and report</option><option value="L4">L4 · autonomous</option>
              </select>
            </label>
            {!editing && (
              <label>Scope
                <select value={form.scope} onChange={set('scope')}>
                  <option value="workspace">Workspace</option><option value="team">Team</option><option value="project">Project</option>
                </select>
              </label>
            )}
            {!editing && form.scope !== 'workspace' && <label>{form.scope} id<input value={form.scopeRef} onChange={set('scopeRef')} /></label>}
          </div>
          <div className="row">
            <button disabled={!form.name || !!busy} onClick={save}>{busy === 'save' ? 'Saving and testing…' : editing ? 'Save and re-test' : 'Add and test'}</button>
            <button className="secondary" onClick={() => { setOpen(false); setEditing(null) }}>Cancel</button>
          </div>
          <ErrorLine error={error} />
        </section>
      )}

      {servers.length === 0 && !open && <section className="card muted">No MCP servers yet. Add one to give agents tools like GitHub, Jira or your internal APIs.</section>}
      {servers.map(s => {
        const tools = s.tools_cache.filter(t => !s.allowed_tools.length || s.allowed_tools.includes(t.name))
        return (
          <section key={s.id} className={`card ${s.enabled ? '' : 'disabled'}`}>
            <div className="row spread">
              <div>
                <h2>{s.name} <span className="muted small">· {s.transport} · {s.scope}</span></h2>
                {s.description && <p className="muted">{s.description}</p>}
              </div>
              <div className="row nowrap">
                {s.last_test_ok === true && <Badge value="connected" />}
                {s.last_test_ok === false && <Badge value="failed" />}
                {!s.enabled && <Badge value="disabled" />}
              </div>
            </div>
            {s.last_test_error && <p className="error small">{s.last_test_error}</p>}
            <p className="small"><span className="muted">{s.transport === 'stdio' ? `${s.command} ${s.args.join(' ')}` : s.url}</span></p>
            {tools.length > 0 && (
              <div className="chips">
                {tools.map(t => <span key={t.name} className={`chip ${s.write_tools.includes(t.name) ? 'chip-write' : ''}`} title={t.description}>{t.name}{s.write_tools.includes(t.name) ? ` · needs ${s.min_autonomy_for_writes}` : ''}</span>)}
              </div>
            )}
            <div className="row">
              <button className="small secondary" onClick={() => act(s, 'test')}>{busy === s.id + 'test' ? 'Testing…' : 'Test'}</button>
              <button className="small secondary" onClick={() => edit(s)}>Edit</button>
              <button className="small secondary" onClick={() => act(s, 'toggle')}>{s.enabled ? 'Disable' : 'Enable'}</button>
              <button className="small secondary danger" onClick={() => act(s, 'delete')}>Remove</button>
            </div>
          </section>
        )
      })}
    </div>
  )
}

// -- Runners and agent defaults --------------------------------------------------------------
export function RunnersPage({ ws }: { ws: string }) {
  const [runners, setRunners] = useState<Runner[]>([])
  const [agents, setAgents] = useState<AgentSetting[]>([])
  const [drafts, setDrafts] = useState<Record<string, Partial<AgentSetting>>>({})
  const [newRunner, setNewRunner] = useState({ name: '', kind: 'worker' })
  const [yaml, setYaml] = useState('')
  const [showYaml, setShowYaml] = useState(false)
  const [msg, setMsg] = useState('')
  const [error, setError] = useState('')

  const load = useCallback(async () => {
    const [r, a] = await Promise.all([api.runners(ws), api.agentSettings(ws)])
    setRunners(r); setAgents(a); setDrafts({})
  }, [ws])
  useEffect(() => { load() }, [load])

  async function addRunner() {
    setError('')
    try { await api.addRunner(ws, { name: newRunner.name, kind: newRunner.kind }); setNewRunner({ name: '', kind: 'worker' }); load() }
    catch (e) { setError(String(e)) }
  }

  async function saveAgent(a: AgentSetting) {
    const d = { ...a, ...drafts[a.agent] }
    const changed = (k: keyof AgentSetting) => k in (drafts[a.agent] ?? {}) || a.overridden.includes(k)
    await api.setAgentSetting(ws, a.agent, {
      runner: changed('runner') && d.runner !== 'inline' ? d.runner : changed('runner') ? 'inline' : null,
      model: changed('model') ? d.model : null,
      max_turns: changed('max_turns') ? Number(d.max_turns) : null,
      token_budget: changed('token_budget') ? Number(d.token_budget) : null,
      context_budget: changed('context_budget') ? Number(d.context_budget) : null,
    }).catch(e => setError(String(e)))
    load()
  }

  async function resetAgent(agent: string) {
    await api.setAgentSetting(ws, agent, {})
    load()
  }

  async function openYaml() { setYaml(await api.exportConfig(ws)); setShowYaml(true); setMsg('') }
  async function applyYaml() {
    setError(''); setMsg('')
    try { setMsg(await api.applyConfig(ws, yaml)); load() } catch (e) { setError(String(e)) }
  }

  const draft = (agent: string, key: keyof AgentSetting, value: string | number) =>
    setDrafts(d => ({ ...d, [agent]: { ...d[agent], [key]: value } }))

  return (
    <div className="page">
      <div className="row spread">
        <div>
          <h1>Runners and agents</h1>
          <p className="muted">Where each agent runs, which model it uses and its budgets. A run can still override these.</p>
        </div>
        <button className="secondary" onClick={openYaml}>Edit as YAML</button>
      </div>

      {showYaml && (
        <section className="card">
          <h2>Config as code</h2>
          <p className="muted small">Runners, MCP servers and agent defaults. Applying is all-or-nothing; secrets stay as ${'{'}env:NAME{'}'} references.</p>
          <textarea className="code" rows={16} value={yaml} onChange={e => setYaml(e.target.value)} spellCheck={false} />
          <div className="row">
            <button onClick={applyYaml}>Apply</button>
            <button className="secondary" onClick={() => setShowYaml(false)}>Close</button>
            {msg && <span className="muted small">Applied {msg}</span>}
          </div>
          <ErrorLine error={error} />
        </section>
      )}

      <section className="card">
        <h2>Agent defaults</h2>
        <div className="table-wrap">
          <table>
            <thead><tr><th>Agent</th><th>Runner</th><th>Model</th><th>Max turns</th><th>Token budget</th><th>Context budget</th><th /></tr></thead>
            <tbody>
              {agents.map(a => {
                const d = { ...a, ...drafts[a.agent] }
                const mark = (k: string) => a.overridden.includes(k) ? 'set' : ''
                return (
                  <tr key={a.agent}>
                    <td><strong>{a.agent}</strong><div className="muted small">{a.description}</div></td>
                    <td className={mark('runner')}>
                      <select value={d.runner} onChange={e => draft(a.agent, 'runner', e.target.value)}>
                        <option value="inline">inline (API process)</option>
                        {runners.map(r => <option key={r.id} value={r.name}>{r.name} ({r.kind})</option>)}
                      </select>
                    </td>
                    <td className={mark('model')}><input value={d.model} onChange={e => draft(a.agent, 'model', e.target.value)} /></td>
                    <td className={mark('max_turns')}><input type="number" className="num" value={d.max_turns} onChange={e => draft(a.agent, 'max_turns', e.target.value)} /></td>
                    <td className={mark('token_budget')}><input type="number" className="num wide" value={d.token_budget} onChange={e => draft(a.agent, 'token_budget', e.target.value)} /></td>
                    <td className={mark('context_budget')}><input type="number" className="num wide" value={d.context_budget} onChange={e => draft(a.agent, 'context_budget', e.target.value)} /></td>
                    <td className="actions">
                      {drafts[a.agent] && <button className="small" onClick={() => saveAgent(a)}>Save</button>}
                      {a.overridden.length > 0 && !drafts[a.agent] && <button className="small secondary" onClick={() => resetAgent(a.agent)}>Reset</button>}
                    </td>
                  </tr>
                )
              })}
            </tbody>
          </table>
        </div>
        <p className="muted small">Highlighted values are set for this workspace; the rest are platform defaults.</p>
      </section>

      <section className="card">
        <h2>Runners</h2>
        <div className="table-wrap">
          <table>
            <thead><tr><th>Name</th><th>Kind</th><th>Default</th><th>Queued</th><th>Last seen</th><th /></tr></thead>
            <tbody>
              <tr className="muted"><td>inline</td><td>API process</td><td>{runners.some(r => r.is_default) ? '' : '★'}</td><td>—</td><td>always</td><td /></tr>
              {runners.map(r => (
                <tr key={r.id}>
                  <td>{r.name}</td><td>{r.kind}</td><td>{r.is_default ? '★' : ''}</td><td>{r.queued}</td>
                  <td>{r.last_seen_at ? new Date(r.last_seen_at).toLocaleString() : 'never'}</td>
                  <td className="actions">
                    {!r.is_default && <button className="small secondary" onClick={() => api.updateRunner(r.id, { is_default: true }).then(load)}>Make default</button>}
                    <button className="small secondary danger" onClick={() => api.deleteRunner(r.id).then(load)}>Remove</button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        <div className="row">
          <input value={newRunner.name} onChange={e => setNewRunner(n => ({ ...n, name: e.target.value }))} placeholder="Runner name, e.g. build-box" />
          <select value={newRunner.kind} onChange={e => setNewRunner(n => ({ ...n, kind: e.target.value }))}>
            <option value="worker">worker</option><option value="container">container</option><option value="kubernetes">kubernetes</option><option value="inline">inline</option>
          </select>
          <button disabled={!newRunner.name.trim()} onClick={addRunner}>Add runner</button>
        </div>
        <p className="muted small">Runs for a non-inline runner wait in its queue until a worker picks them up: <code>fil runner start &lt;name&gt;</code> on the machine, container or pod where it should run.</p>
        {!showYaml && <ErrorLine error={error} />}
      </section>
    </div>
  )
}
