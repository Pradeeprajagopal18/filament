import { useCallback, useEffect, useState } from 'react'
import { api, type Health, type Project, type Workspace } from './api'
import { AskPage, GapsPage, LaunchpadPage, MemoryPage, ProjectPage, UsagePage } from './pages'

type View = { kind: 'project'; project: Project } | { kind: 'memory' | 'ask' | 'usage' | 'launchpad' | 'gaps' }

const NAV: { kind: Exclude<View['kind'], 'project'>; label: string }[] = [
  { kind: 'ask', label: 'Ask Filament' },
  { kind: 'memory', label: 'Shared memory' },
  { kind: 'launchpad', label: 'Launchpad' },
  { kind: 'gaps', label: "What we're missing" },
  { kind: 'usage', label: 'Token usage' },
]

export default function App() {
  const [health, setHealth] = useState<Health | null>(null)
  const [workspaces, setWorkspaces] = useState<Workspace[]>([])
  const [ws, setWs] = useState<string>(() => localStorageGet('filament.ws'))
  const [projects, setProjects] = useState<Project[]>([])
  const [view, setView] = useState<View>({ kind: 'ask' })
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

  const loadProjects = useCallback(() => { if (ws) api.projects(ws).then(setProjects) }, [ws])
  useEffect(() => { loadProjects(); localStorageSet('filament.ws', ws) }, [ws, loadProjects])

  async function addProject() {
    const p = await api.createProject(ws, newProject, '')
    setNewProject('')
    loadProjects()
    setView({ kind: 'project', project: p })
  }

  return (
    <div className="app">
      <aside>
        <div className="brand"><span className="bulb" />Filament</div>
        <select value={ws} onChange={e => setWs(e.target.value)}>
          {workspaces.map(w => <option key={w.id} value={w.id}>{w.name}</option>)}
        </select>
        <nav>
          {NAV.map(n => (
            <button key={n.kind} className={view.kind === n.kind ? 'active' : ''} onClick={() => setView({ kind: n.kind })}>{n.label}</button>
          ))}
        </nav>
        <h4>Projects</h4>
        <nav>
          {projects.map(p => (
            <button key={p.id} className={view.kind === 'project' && view.project.id === p.id ? 'active' : ''}
              onClick={() => setView({ kind: 'project', project: p })}>{p.name}</button>
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
        {error && <p className="error">{error}</p>}
        {ws && view.kind === 'project' && <ProjectPage key={view.project.id} project={view.project} />}
        {ws && view.kind === 'memory' && <MemoryPage ws={ws} />}
        {ws && view.kind === 'ask' && <AskPage ws={ws} />}
        {ws && view.kind === 'usage' && <UsagePage ws={ws} />}
        {ws && view.kind === 'gaps' && <GapsPage ws={ws} />}
        {view.kind === 'launchpad' && <LaunchpadPage />}
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
