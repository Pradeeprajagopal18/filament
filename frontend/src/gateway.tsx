// MCP gateway: one place that decides which tools agents can reach. Native MCP servers and
// servers Ignite generates from your repos' APIs share a registry (tools start off until an
// owner enables them), policy, response trimming, redaction and call metrics.
import { useCallback, useEffect, useState } from 'react'
import { api, DEMO } from './api'
import type { GatewayCall, GatewayMetrics, GatewayServer, GatewayTool, InvokeResult } from './api'
import { ErrorLine } from './components'

type Tab = 'registry' | 'activity' | 'connect'

function ago(iso?: string | null) {
  if (!iso) return 'never'
  const mins = Math.round((Date.now() - new Date(iso).getTime()) / 60000)
  if (mins < 60) return `${Math.max(mins, 0)}m ago`
  const h = Math.round(mins / 60)
  return h < 48 ? `${h}h ago` : `${Math.round(h / 24)}d ago`
}

export function GatewayPage({ ws }: { ws: string }) {
  const [tab, setTab] = useState<Tab>('registry')
  const [servers, setServers] = useState<GatewayServer[]>([])
  const [metrics, setMetrics] = useState<GatewayMetrics | null>(null)
  const [q, setQ] = useState('')
  const [error, setError] = useState('')
  const load = useCallback(() => {
    api.gatewayRegistry(ws, q).then(setServers).catch(e => setError(String(e)))
    api.gatewayMetrics(ws).then(setMetrics).catch(() => setMetrics(null))
  }, [ws, q])
  useEffect(() => { load() }, [load])
  const tools = servers.reduce((n, s) => n + s.tools, 0)
  const enabled = servers.reduce((n, s) => n + s.enabled_tools, 0)
  const review = servers.reduce((n, s) => n + s.counts.changed + s.counts.new, 0)

  return (
    <div className="page">
      <h1>MCP gateway</h1>
      <p className="muted">Every tool agents can call goes through here: third-party MCP servers you add under Connectors, and servers Ignite generates from your repos' APIs. Tools start off until an owner enables them, then policy, rate limits, response trimming and redaction apply to every call.</p>
      <div className="stats">
        <div><b>{servers.length}</b><span>servers</span></div>
        <div><b>{enabled}<small>/{tools}</small></b><span>tools enabled</span></div>
        <div className={review ? 'stat-alert' : ''}><b>{review}</b><span>to review</span></div>
        <div><b>{metrics?.calls ?? 0}</b><span>calls (24h)</span></div>
        <div><b>{Math.round((metrics?.bytes_saved ?? 0) / 1024)}<small>KB</small></b><span>trimmed</span></div>
        <div><b>{metrics?.redactions ?? 0}</b><span>redactions</span></div>
      </div>
      <ErrorLine error={error} />
      <div className="tabs" role="tablist">
        {([['registry', 'Registry'], ['activity', 'Activity'], ['connect', 'Connect agents']] as [Tab, string][]).map(([t, label]) => (
          <button key={t} role="tab" aria-selected={tab === t} className={tab === t ? 'active' : ''} onClick={() => setTab(t)}>{label}</button>
        ))}
      </div>
      {tab === 'registry' && (
        <>
          <input value={q} onChange={e => setQ(e.target.value)} placeholder="Search servers and tools, e.g. 'cart' or 'issues'" />
          {servers.length === 0 && <section className="card muted">No servers yet. Add one under Connectors, or onboard a repo with an API in Launchpad.</section>}
          {servers.map(s => <ServerCard key={s.id} ws={ws} server={s} onChange={load} />)}
        </>
      )}
      {tab === 'activity' && <Activity ws={ws} metrics={metrics} />}
      {tab === 'connect' && <Connect ws={ws} />}
    </div>
  )
}

function ServerCard({ ws, server, onChange }: { ws: string; server: GatewayServer; onChange: () => void }) {
  const [open, setOpen] = useState(server.counts.changed > 0)
  const [tools, setTools] = useState<GatewayTool[] | null>(null)
  const [url, setUrl] = useState(server.url ?? '')
  const [busy, setBusy] = useState('')
  const [error, setError] = useState('')
  const loadTools = useCallback(() => api.gatewayTools(server.id).then(setTools).catch(e => setError(String(e))), [server.id])
  useEffect(() => { if (open) loadTools() }, [open, loadTools])

  async function act(key: string, fn: () => Promise<unknown>) {
    setBusy(key); setError('')
    try { await fn(); await loadTools(); onChange() } catch (e) { setError(String(e)) } finally { setBusy('') }
  }
  const generated = server.kind === 'generated'
  return (
    <section className="card">
      <button className="server-head" onClick={() => setOpen(o => !o)} aria-expanded={open}>
        <div className="grow">
          <strong>{server.name}</strong>{' '}
          <span className={`kind ${generated ? 'kind-api' : 'kind-library'}`}>{generated ? 'generated from repo' : 'MCP server'}</span>
          {!server.enabled && <span className="badge badge-rejected">off</span>}
          <div className="muted small">{server.description}</div>
        </div>
        <div className="server-counts small">
          <span>{server.enabled_tools}/{server.tools} on</span>
          {server.counts.changed > 0 && <span className="badge badge-in_review">{server.counts.changed} changed</span>}
          {server.counts.new > 0 && <span className="badge badge-draft">{server.counts.new} new</span>}
        </div>
      </button>
      {open && (
        <div className="server-body">
          {generated && (
            <div className="row wrap base-url">
              <input value={url} onChange={e => setUrl(e.target.value)} placeholder="Base URL, e.g. https://checkout.internal (empty = simulate from the contract)" />
              <button className="secondary small" disabled={!!busy || url === (server.url ?? '')}
                onClick={() => act('url', () => api.setGatewayServer(server.id, { url: url.trim() || null }))}>Save URL</button>
            </div>
          )}
          <p className="muted small">
            {generated ? (server.url ? `Calls go to ${server.url}.` : 'No base URL: calls return the example responses from the API contract, so agents can be tried safely.') : ''}
            {' '}Writes need autonomy {server.min_autonomy_for_writes}+. Synced {ago(server.last_synced_at)}.{' '}
            <label className="inline"><input type="checkbox" checked={server.redact} onChange={e => act('redact', () => api.setGatewayServer(server.id, { redact: e.target.checked }))} /> redact PII and secrets</label>
            {' · '}<button className="link small" disabled={!!busy} onClick={() => act('sync', () => api.syncGatewayServer(server.id))}>{busy === 'sync' ? 'Syncing…' : 'Sync now'}</button>
          </p>
          <ErrorLine error={error} />
          {!tools ? <p className="muted small">Loading…</p> : (
            <ul className="gw-tools">
              {tools.map(t => <ToolRow key={t.id} ws={ws} server={server} tool={t} busy={busy} act={act} />)}
            </ul>
          )}
        </div>
      )}
    </section>
  )
}

function ToolRow({ ws, server, tool, busy, act }: {
  ws: string; server: GatewayServer; tool: GatewayTool; busy: string; act: (k: string, fn: () => Promise<unknown>) => void
}) {
  const [open, setOpen] = useState(tool.status === 'changed')
  const [desc, setDesc] = useState(tool.description)
  const [agents, setAgents] = useState(tool.allowed_agents.join(', '))
  const [limit, setLimit] = useState(tool.rate_limit_per_min ? String(tool.rate_limit_per_min) : '')
  const removed = tool.status === 'removed'
  const props = Object.keys(((tool.input_schema as { properties?: object }).properties ?? {}))
  const pendingProps = tool.pending ? Object.keys(((tool.pending.input_schema as { properties?: object }).properties ?? {})) : []
  const bodyKeys = (s: Record<string, unknown>) => Object.keys((((s as { properties?: Record<string, { properties?: object }> }).properties ?? {}).body?.properties) ?? {})
  return (
    <li className={`gw-tool status-${tool.status}`}>
      <div className="row wrap gw-tool-head">
        <label className="switch" title={removed ? 'Removed from the server' : tool.enabled ? 'Enabled' : 'Disabled'}>
          <input type="checkbox" disabled={removed || !!busy} checked={tool.enabled}
            onChange={e => act(`en${tool.id}`, () => api.patchGatewayTool(tool.id, { enabled: e.target.checked }))} />
          <span />
        </label>
        <button className="link tool-name" onClick={() => setOpen(o => !o)}><code>{tool.name}</code></button>
        {tool.write && <span className="badge badge-in_review">write</span>}
        {Boolean(tool.meta.looks_like_write) && !tool.write && <span className="badge badge-draft" title="Named like a write; mark it if it changes things">write?</span>}
        {tool.status !== 'active' && <span className={`badge gw-${tool.status}`}>{tool.status}</span>}
        <span className="muted small grow">{tool.description.slice(0, 90)}</span>
        <span className="muted small">v{tool.version}</span>
      </div>
      {open && (
        <div className="gw-tool-body small">
          {tool.pending && (
            <div className="pending">
              <b>Change waiting for review</b> <span className="muted">({tool.pending.source}, {ago(tool.pending.at)})</span>
              {tool.pending.description !== tool.description && <div className="diff"><div className="del">{tool.description}</div><div className="add">{tool.pending.description}</div></div>}
              {JSON.stringify(tool.pending.input_schema) !== JSON.stringify(tool.input_schema) && (
                <div className="diff">
                  {[...new Set([...props, ...pendingProps, ...bodyKeys(tool.input_schema).map(k => `body.${k}`), ...bodyKeys(tool.pending.input_schema).map(k => `body.${k}`)])].map(k => {
                    const before = k.startsWith('body.') ? bodyKeys(tool.input_schema).includes(k.slice(5)) : props.includes(k)
                    const after = k.startsWith('body.') ? bodyKeys(tool.pending!.input_schema).includes(k.slice(5)) : pendingProps.includes(k)
                    return before === after ? null : <div key={k} className={after ? 'add' : 'del'}>{after ? '+' : '−'} {k}</div>
                  })}
                  {props.length === pendingProps.length && bodyKeys(tool.input_schema).length === bodyKeys(tool.pending.input_schema).length && <div className="muted">Schema details changed (types or required fields).</div>}
                </div>
              )}
              <div className="row">
                <button className="small" disabled={!!busy} onClick={() => act(`ap${tool.id}`, () => api.approveGatewayTool(tool.id))}>Approve</button>
                <button className="secondary small" disabled={!!busy} onClick={() => act(`rj${tool.id}`, () => api.rejectGatewayTool(tool.id))}>Keep current</button>
              </div>
            </div>
          )}
          <div className="muted">Inputs: {props.length ? props.join(', ') : 'none'}{tool.meta.method ? ` · ${String(tool.meta.method)} ${String(tool.meta.path)}` : ''}</div>
          <label>Description (changes are reviewed before agents see them)
            <textarea rows={2} value={desc} onChange={e => setDesc(e.target.value)} />
          </label>
          <div className="row wrap">
            <label className="grow">Only these agents (blank = any)
              <input value={agents} onChange={e => setAgents(e.target.value)} placeholder="e.g. requirements, flow" />
            </label>
            <label>Calls / min
              <input value={limit} onChange={e => setLimit(e.target.value.replace(/\D/g, ''))} placeholder="no limit" className="narrow" />
            </label>
          </div>
          <div className="row wrap">
            <button className="secondary small" disabled={!!busy} onClick={() => act(`sv${tool.id}`, async () => {
              await api.patchGatewayTool(tool.id, {
                allowed_agents: agents.split(',').map(a => a.trim()).filter(Boolean),
                rate_limit_per_min: limit ? Number(limit) : 0,
                ...(desc !== tool.description ? { description: desc } : {}),
              })
            })}>Save policy{desc !== tool.description ? ' and propose description' : ''}</button>
            {server.kind === 'native' && <button className="link small" disabled={!!busy}
              onClick={() => act(`wr${tool.id}`, () => api.patchGatewayTool(tool.id, { write: !tool.write }))}>{tool.write ? 'Mark read-only' : 'Mark as write'}</button>}
          </div>
          {tool.history.length > 1 && (
            <details>
              <summary>History ({tool.history.length})</summary>
              <ul className="plain">
                {[...tool.history].reverse().map(h => (
                  <li key={h.version + h.at}>v{h.version} · {h.by} · {ago(h.at)}{h.rolled_back_to ? ` (rollback to v${h.rolled_back_to})` : ''}
                    {h.version !== tool.version && <> · <button className="link small" disabled={!!busy} onClick={() => act(`rb${tool.id}`, () => api.rollbackGatewayTool(tool.id, h.version))}>Roll back</button></>}
                  </li>
                ))}
              </ul>
            </details>
          )}
          {tool.enabled && <TryTool ws={ws} server={server} tool={tool} />}
        </div>
      )}
    </li>
  )
}

function TryTool({ ws, server, tool }: { ws: string; server: GatewayServer; tool: GatewayTool }) {
  const props = (tool.input_schema as { properties?: Record<string, { type?: string }> }).properties ?? {}
  const [args, setArgs] = useState(() => JSON.stringify(Object.fromEntries(Object.entries(props)
    .filter(([k]) => k !== 'body').map(([k, v]) => [k, v.type === 'boolean' ? false : v.type === 'integer' ? 1 : 'sample'])), null, 0))
  const [fields, setFields] = useState('')
  const [autonomy, setAutonomy] = useState('L1')
  const [res, setRes] = useState<InvokeResult | null>(null)
  const [busy, setBusy] = useState(false)
  async function run() {
    setBusy(true)
    try {
      setRes(await api.gatewayInvoke(ws, { server: server.name, tool: tool.name, arguments: JSON.parse(args || '{}'),
        fields: fields ? fields.split(',').map(f => f.trim()).filter(Boolean) : null, autonomy }))
    } catch (e) { setRes({ status: 0, body: { error: String(e) } }) } finally { setBusy(false) }
  }
  return (
    <div className="try">
      <b>Try it</b>
      <input value={args} onChange={e => setArgs(e.target.value)} aria-label="Arguments (JSON)" />
      <div className="row wrap">
        <input className="grow" value={fields} onChange={e => setFields(e.target.value)} placeholder="Only these fields, e.g. id, items.sku" />
        {tool.write && <select value={autonomy} onChange={e => setAutonomy(e.target.value)} aria-label="Autonomy"><option>L1</option><option>L2</option><option>L3</option></select>}
        <button className="secondary small" disabled={busy} onClick={run}>{busy ? 'Calling…' : 'Call'}</button>
      </div>
      {res && <pre className="json">{res.status} · {JSON.stringify(res.body, null, 2)}</pre>}
    </div>
  )
}

function Activity({ ws, metrics }: { ws: string; metrics: GatewayMetrics | null }) {
  const [calls, setCalls] = useState<GatewayCall[]>([])
  useEffect(() => { api.gatewayCalls(ws).then(setCalls).catch(() => setCalls([])) }, [ws])
  return (
    <>
      <section className="card">
        <h3>Last 24 hours</h3>
        {!metrics || metrics.tools.length === 0 ? <p className="muted small">No calls yet.</p> : (
          <table className="deps">
            <thead><tr><th>Tool</th><th>Calls</th><th>Errors</th><th>Denied</th><th>p95</th><th>Trimmed</th></tr></thead>
            <tbody>
              {metrics.tools.map(t => (
                <tr key={t.server + t.tool}>
                  <td><code>{t.server}.{t.tool}</code><div className="muted small">{t.actors.join(', ')}{t.simulated ? ` · ${t.simulated} simulated` : ''}</div></td>
                  <td>{t.calls}</td><td>{t.errors || '—'}</td><td>{t.denied || '—'}</td><td>{t.p95_ms} ms</td>
                  <td>{t.bytes_saved ? `${Math.round(t.bytes_saved / 1024 * 10) / 10} KB` : '—'}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </section>
      <section className="card">
        <h3>Recent calls</h3>
        <ul className="plain small">
          {calls.map((c, i) => (
            <li key={i} className="call-row">
              <span className={`call-status call-${c.status}`}>{c.status}</span> <code>{c.server}.{c.tool}</code> · {c.actor} · {c.latency_ms} ms · {ago(c.at)}
              {c.redactions > 0 && <> · {c.redactions} redacted</>}
              {c.error && <div className="muted">{c.error}</div>}
            </li>
          ))}
          {calls.length === 0 && <li className="muted">No calls yet.</li>}
        </ul>
      </section>
    </>
  )
}

function Connect({ ws }: { ws: string }) {
  const origin = DEMO ? 'https://ignite.your-company.internal' : window.location.origin
  const url = `${origin}/api/workspaces/${ws}/mcp/omni`
  return (
    <>
      <section className="card">
        <h3>One MCP endpoint for everything</h3>
        <p className="small">Point Claude Code, an IDE or any MCP client at the gateway instead of installing servers one by one. It exposes four tools (discover servers, list tools, get a schema, invoke) over every enabled tool, with the same policy, trimming and redaction.</p>
        <pre className="json">claude mcp add --transport http ignite {url}</pre>
        <p className="muted small">Calls are read-only unless the client sends <code>X-Ignite-Autonomy: L3</code> (or the server's minimum) for write tools.</p>
      </section>
      <section className="card">
        <h3>Code Mode from the terminal</h3>
        <pre className="json">{`ign mcp search "cart"
ign mcp enable api-checkout-api get_cart
ign mcp call api-checkout-api get_cart --arg cart_id=c1 --fields id,total -o cart.json
ign mcp metrics`}</pre>
        <p className="muted small">Results can go to a file and be trimmed to the fields you need, so large responses never enter a model's context.</p>
      </section>
      <section className="card">
        <h3>Ignite's own agents</h3>
        <p className="small">With a handful of tools, agents see each one directly. Above the discovery threshold (15 by default, <code>IGNITE_MCP_DISCOVERY_THRESHOLD</code>), they get the same four discovery tools instead, so hundreds of tools don't flood their context.</p>
      </section>
    </>
  )
}
