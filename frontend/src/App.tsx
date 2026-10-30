import { useCallback, useEffect, useRef, useState } from 'react'
import { api, DEMO, type Health, type Project, type Workspace } from './api'
import { LaunchpadPage } from './launchpad'
import { AskPage, GapsPage, MemoryPage, ProjectPage, UsagePage } from './pages'
import { ConnectorsPage, RunnersPage } from './settings'

type View = { kind: 'project'; project: Project } | { kind: 'memory' | 'ask' | 'usage' | 'launchpad' | 'gaps' | 'connectors' | 'runners' }

const NAV: { kind: Exclude<View['kind'], 'project'>; label: string }[] = [
  { kind: 'ask', label: 'Ask Filament' },
  { kind: 'memory', label: 'Shared memory' },
  { kind: 'launchpad', label: 'Launchpad' },
  { kind: 'gaps', label: "What we're missing" },
  { kind: 'usage', label: 'Token usage' },
  { kind: 'connectors', label: 'Connectors (MCP)' },
  { kind: 'runners', label: 'Runners and agents' },
]

export default function App() {
  const [health, setHealth] = useState<Health | null>(null)
  const [workspaces, setWorkspaces] = useState<Workspace[]>([])
  const [ws, setWs] = useState<string>(() => localStorageGet('filament.ws'))
  const [projects, setProjects] = useState<Project[]>([])
  const [view, setView] = useState<View>({ kind: 'ask' })
  const [menuOpen, setMenuOpen] = useState(false)
  const [newProject, setNewProject] = useState('')
  const [error, setError] = useState('')

  useEffect(() => {
    api.health().then(setHealth).catch(() => setError('Cannot reach the Filament API. Start it with `make dev-api`.'))
    api.workspaces().then(async list => {
      if (list.length === 0) list = [await api.createWorkspace('My workspace')]
      setWorkspaces(list)
      setWs(cur => (list.some(w => w.id === cur) ? cur : list[0].id))
    }).catch(() => {})
  }, [])

  const opened = useRef(false)
  const loadProjects = useCallback(() => {
    if (!ws) return
    api.projects(ws).then(list => {
      setProjects(list)
      // The preview opens straight onto the sample project.
      if (DEMO && !opened.current && list.length) { opened.current = true; setView({ kind: 'project', project: list[0] }) }
    })
  }, [ws])
  const go = (v: View) => { setView(v); setMenuOpen(false) }
  useEffect(() => { loadProjects(); localStorageSet('filament.ws', ws) }, [ws, loadProjects])

  async function addProject() {
    const p = await api.createProject(ws, newProject, '')
    setNewProject('')
    loadProjects()
    go({ kind: 'project', project: p })
  }

  return (
    <div className="app">
      <div className="topbar">
        <button className="menu-btn" aria-label="Menu" onClick={() => setMenuOpen(o => !o)}>☰</button>
        <div className="brand"><span className="bulb" />Filament</div>
      </div>
      <aside className={menuOpen ? 'open' : ''}>
        <div className="brand"><span className="bulb" />Filament</div>
        <select value={ws} onChange={e => setWs(e.target.value)}>
          {workspaces.map(w => <option key={w.id} value={w.id}>{w.name}</option>)}
        </select>
        <nav>
          {NAV.map(n => (
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
        {health && (
          <div className="status">
            {health.offline ? 'Offline mode — set ANTHROPIC_API_KEY for live agents' : `Live · ${health.model}`}
          </div>
        )}
      </aside>
      <main>
        {DEMO && <div className="demo-banner">Interactive preview · sample data · agents are simulated in your browser. Try “Take idea to design” on a new project.</div>}
        {error && <p className="error">{error}</p>}
        {ws && view.kind === 'project' && <ProjectPage key={view.project.id} project={view.project} />}
        {ws && view.kind === 'memory' && <MemoryPage ws={ws} />}
        {ws && view.kind === 'ask' && <AskPage ws={ws} />}
        {ws && view.kind === 'usage' && <UsagePage ws={ws} />}
        {ws && view.kind === 'gaps' && <GapsPage ws={ws} />}
        {ws && view.kind === 'launchpad' && <LaunchpadPage ws={ws} />}
        {ws && view.kind === 'connectors' && <ConnectorsPage ws={ws} />}
        {ws && view.kind === 'runners' && <RunnersPage ws={ws} />}
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
