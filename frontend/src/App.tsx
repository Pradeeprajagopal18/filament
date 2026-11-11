import { useCallback, useEffect, useRef, useState } from 'react'
import { AdminPage, ChangePassword, SignIn, TokensPage } from './access'
import { KnowledgePage } from './knowledge'
import { api, DEMO, MUST_CHANGE, SIGNED_OUT, type Health, type Me, type Project, type Workspace } from './api'
import { DEMO_PEOPLE, demoViewAs } from './demo'
import { GatewayPage } from './gateway'
import { LaunchpadPage } from './launchpad'
import { AskPage, GapsPage, MemoryPage, ProjectPage, UsagePage } from './pages'
import { ConnectorsPage, RunnersPage } from './settings'

type View = { kind: 'project'; project: Project } | { kind: 'memory' | 'ask' | 'usage' | 'launchpad' | 'gaps' | 'connectors' | 'gateway' | 'runners' | 'people' | 'tokens' | 'knowledge' }

// Pages only workspace admins can use (the API enforces this too).
const ADMIN_ONLY = new Set<View['kind']>(['usage', 'connectors', 'gateway', 'runners', 'people', 'knowledge'])

const NAV: { kind: Exclude<View['kind'], 'project'>; label: string }[] = [
  { kind: 'ask', label: 'Ask Ignite' },
  { kind: 'memory', label: 'Shared memory' },
  { kind: 'launchpad', label: 'Launchpad' },
  { kind: 'gaps', label: "What we're missing" },
  { kind: 'usage', label: 'Token usage' },
  { kind: 'connectors', label: 'Connectors (MCP)' },
  { kind: 'gateway', label: 'MCP gateway' },
  { kind: 'runners', label: 'Runners and agents' },
  { kind: 'knowledge', label: 'Knowledge' },
  { kind: 'people', label: 'People & access' },
  { kind: 'tokens', label: 'My tokens' },
]

export default function App() {
  const [health, setHealth] = useState<Health | null>(null)
  const [workspaces, setWorkspaces] = useState<Workspace[]>([])
  const [ws, setWs] = useState<string>(() => localStorageGet('ignite.ws'))
  const [projects, setProjects] = useState<Project[]>([])
  const [view, setView] = useState<View>({ kind: 'ask' })
  const [menuOpen, setMenuOpen] = useState(false)
  const [newProject, setNewProject] = useState('')
  const [error, setError] = useState('')
  const [me, setMe] = useState<Me | null>(null)
  const [signedOut, setSignedOut] = useState(false)
  const [asUser, setAsUser] = useState('u-admin')
  const loginError = new URLSearchParams(window.location.search).get('login_error') ?? ''

  const loadAll = useCallback(() => {
    api.health().then(setHealth).catch(() => setError('Cannot reach the Ignite API. Start it with `make dev-api`.'))
    const loadMe = () => api.me().then(m => { setMe(m); setSignedOut(false); setAsUser(u => m.id ?? u) })
      .catch(e => { if (String(e).includes('401')) setSignedOut(true) })
    // Workspaces first: on a new install the first one is created here, and your role in it
    // (which decides whether the admin pages show) is only known after that.
    api.workspaces().then(async list => {
      if (list.length === 0) list = [await api.createWorkspace('My workspace')]
      setWorkspaces(list)
      setWs(cur => (list.some(w => w.id === cur) ? cur : list[0].id))
    }).catch(() => {}).finally(loadMe)
  }, [])
  useEffect(() => { loadAll() }, [loadAll])
  useEffect(() => {
    const out = () => setSignedOut(true)
    const change = () => { api.me().then(setMe).catch(() => {}) }
    window.addEventListener(SIGNED_OUT, out)
    window.addEventListener(MUST_CHANGE, change)
    return () => { window.removeEventListener(SIGNED_OUT, out); window.removeEventListener(MUST_CHANGE, change) }
  }, [])
  const isAdmin = !me || me.mode === 'none' || me.is_admin || !!me.workspaces.find(w => w.id === ws && w.role === 'admin')
  // Features an admin turned off for this person (the API enforces it; this keeps the menu honest).
  const feats = me?.workspaces.find(w => w.id === ws)?.features
  const has = (f: string) => !feats || isAdmin || feats.includes(f)
  const NEEDS: Partial<Record<View['kind'], string[]>> = { memory: ['memory'], ask: ['ask'], gaps: ['ask'], launchpad: ['launchpad', 'system_map'], gateway: ['mcp_tools'] }
  const nav = NAV.filter(n => (!NEEDS[n.kind] || NEEDS[n.kind]!.some(has)) && (isAdmin || !ADMIN_ONLY.has(n.kind)) && (n.kind !== 'tokens' || me?.mode === 'login') && (n.kind !== 'people' || me?.mode === 'login'))
  function switchUser(u: string) {
    demoViewAs(u); setAsUser(u); setView({ kind: 'launchpad' }); setMenuOpen(false); loadAll()
  }

  const opened = useRef(false)
  const loadProjects = useCallback(() => {
    if (!ws) return
    api.projects(ws).then(list => {
      setProjects(list)
      // The preview opens straight onto the sample project.
      if (DEMO && !opened.current && list.length) { opened.current = true; setView({ kind: 'project', project: list[0] }) }
    })
  }, [ws, asUser]) // eslint-disable-line react-hooks/exhaustive-deps -- reload when the preview user changes
  const go = (v: View) => { setView(v); setMenuOpen(false) }
  useEffect(() => { loadProjects(); localStorageSet('ignite.ws', ws) }, [ws, loadProjects])

  async function addProject() {
    const p = await api.createProject(ws, newProject, '')
    setNewProject('')
    loadProjects()
    go({ kind: 'project', project: p })
  }

  if (signedOut) return <SignIn error={loginError} onSignedIn={() => { setView({ kind: 'launchpad' }); loadAll() }} />
  if (me?.must_change_password) return <ChangePassword me={me} onDone={loadAll} />

  return (
    <div className="app">
      <div className="topbar">
        <button className="menu-btn" aria-label="Menu" onClick={() => setMenuOpen(o => !o)}>☰</button>
        <div className="brand"><span className="bulb" />Ignite</div>
      </div>
      <aside className={menuOpen ? 'open' : ''}>
        <div className="brand"><span className="bulb" />Ignite</div>
        <select value={ws} onChange={e => setWs(e.target.value)}>
          {workspaces.map(w => <option key={w.id} value={w.id}>{w.name}</option>)}
        </select>
        <nav>
          {nav.map(n => (
            <button key={n.kind} className={view.kind === n.kind ? 'active' : ''} onClick={() => go({ kind: n.kind })}>{n.label}</button>
          ))}
        </nav>
        <h4>Projects</h4>
        <nav>
          {projects.map(p => (
            <button key={p.id} className={view.kind === 'project' && view.project.id === p.id ? 'active' : ''}
              onClick={() => go({ kind: 'project', project: p })}>{p.name}</button>
          ))}
        </nav>
        <div className="new-project">
          <input value={newProject} onChange={e => setNewProject(e.target.value)} placeholder="New project"
            onKeyDown={e => e.key === 'Enter' && newProject.trim() && addProject()} />
        </div>
        {me && me.mode === 'login' && (
          <div className="me">
            <div><b>{me.name || me.email}</b><div className="muted small">{me.email || me.username}{isAdmin ? ' · admin' : ''}</div></div>
            {DEMO && (
              <label className="small">View as <select value={asUser} onChange={e => switchUser(e.target.value)}>
                {DEMO_PEOPLE().map(p => <option key={p.id} value={p.id}>{p.label}</option>)}
              </select></label>
            )}
            <button className="link small" onClick={async () => { await api.logout().catch(() => {}); setMe(null); setSignedOut(true) }}>Sign out</button>
          </div>
        )}
        {health && (
          <div className="status">
            {health.offline ? 'Offline mode — set ANTHROPIC_API_KEY for live agents' : `Live · ${health.model}`}
          </div>
        )}
      </aside>
      <main>
        {DEMO && <div className="demo-banner">Interactive preview · sample data · the delivery steps replay a run of the real agents. Open a work item and follow the seven stages.</div>}
        {error && <p className="error">{error}</p>}
        {ws && view.kind === 'project' && <ProjectPage key={view.project.id + asUser} project={view.project} ws={ws} me={me} can={has} />}
        {ws && view.kind === 'memory' && <MemoryPage ws={ws} />}
        {ws && view.kind === 'ask' && <AskPage ws={ws} offline={health?.offline} />}
        {ws && view.kind === 'usage' && <UsagePage ws={ws} />}
        {ws && view.kind === 'gaps' && <GapsPage ws={ws} />}
        {ws && view.kind === 'launchpad' && <LaunchpadPage key={asUser} ws={ws} can={has} />}
        {ws && view.kind === 'connectors' && <ConnectorsPage ws={ws} />}
        {ws && view.kind === 'gateway' && <GatewayPage ws={ws} />}
        {ws && view.kind === 'runners' && <RunnersPage ws={ws} />}
        {ws && view.kind === 'people' && <AdminPage key={asUser} ws={ws} projects={projects} me={me} />}
        {view.kind === 'tokens' && <TokensPage key={asUser} />}
        {ws && view.kind === 'knowledge' && <KnowledgePage key={asUser} ws={ws} />}
      </main>
    </div>
  )
}

function localStorageGet(key: string): string {
  try { return localStorage.getItem(key) ?? '' } catch { return '' }
}
function localStorageSet(key: string, value: string) {
  try { localStorage.setItem(key, value) } catch { /* storage unavailable */ }
}
