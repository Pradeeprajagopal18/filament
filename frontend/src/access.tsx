// Sign-in, People & access (admins), project access, and personal tokens.
import { useCallback, useEffect, useState } from 'react'
import {
  api, CHECKPOINT_ROLES, PROJECT_ROLES,
  type ApiTokenRow, type AuditRow, type CheckpointRoleRow, type Me, type Person, type Project, type ProjectAccess, type Team,
} from './api'
import { ErrorLine } from './components'

const label = (s: string) => s.replace(/_/g, ' ')

export function SignIn({ error }: { error?: string }) {
  const next = typeof window === 'undefined' ? '/' : window.location.pathname + window.location.search.replace(/[?&]login_error=[^&]*/, '')
  return (
    <div className="signin">
      <div className="card">
        <div className="brand"><span className="bulb" />Filament</div>
        <p className="muted">Sign in with your company account. You'll see the projects and repos you have access to.</p>
        {error && <p className="error">{error}</p>}
        <a className="button" href={`/api/auth/login?next=${encodeURIComponent(next || '/')}`}>Sign in with SSO</a>
      </div>
    </div>
  )
}

// -- People & access (workspace admins) --------------------------------------------------------
export function PeoplePage({ ws, projects }: { ws: string; projects: Project[] }) {
  const [people, setPeople] = useState<Person[]>([])
  const [teams, setTeams] = useState<Team[]>([])
  const [roles, setRoles] = useState<CheckpointRoleRow[]>([])
  const [audit, setAudit] = useState<AuditRow[]>([])
  const [invite, setInvite] = useState({ email: '', role: 'member', github: '' })
  const [assign, setAssign] = useState({ user: '', role: 'product_owner', project: '' })
  const [newTeam, setNewTeam] = useState('')
  const [error, setError] = useState('')
  const load = useCallback(async () => {
    try {
      const [p, t, r, a] = await Promise.all([api.people(ws), api.teams(ws), api.checkpointRoles(ws), api.audit(ws)])
      setPeople(p); setTeams(t); setRoles(r); setAudit(a); setError('')
    } catch (e) { setError(String(e)) }
  }, [ws])
  useEffect(() => { load() }, [load])
  const act = async (f: () => Promise<unknown>) => { try { await f(); await load() } catch (e) { setError(String(e)) } }
  const teamName = (id: string) => teams.find(t => t.id === id)?.name ?? id
  const projectName = (id: string | null) => (id ? projects.find(p => p.id === id)?.name ?? id : 'every project')

  return (
    <div className="page">
      <h1>People & access</h1>
      <p className="muted">People sign in with the company SSO. Open projects are visible to everyone in the workspace; restricted projects only to their members and teams. Private repos follow GitHub: someone sees one if they can read it there (set their GitHub username below), otherwise only with a role on its project.</p>
      <ErrorLine error={error} />

      <section className="card">
        <h3>People</h3>
        <div className="table-wrap">
          <table className="people">
            <thead><tr><th>Person</th><th>Workspace</th><th>GitHub</th><th>Teams</th><th /></tr></thead>
            <tbody>
              {people.map(p => (
                <tr key={p.id} className={p.active ? '' : 'disabled'}>
                  <td><b>{p.name || p.email}</b><div className="muted small">{p.email}{p.last_login_at ? '' : ' · not signed in yet'}</div></td>
                  <td>
                    {p.is_admin ? <span className="badge badge-approved">admin</span> : (
                      <select aria-label={`Workspace role for ${p.email}`} value={p.workspace_role ?? 'none'} onChange={e => act(() => api.updatePerson(ws, p.id, { workspace_role: e.target.value }))}>
                        <option value="member">member</option><option value="admin">admin</option>
                      </select>
                    )}
                  </td>
                  <td>
                    <input aria-label={`GitHub username for ${p.email}`} className="small-input" defaultValue={p.github_login ?? ''} placeholder="username"
                      onBlur={e => e.target.value !== (p.github_login ?? '') && act(() => api.updatePerson(ws, p.id, { github_login: e.target.value }))} />
                  </td>
                  <td className="small">{p.teams.map(t => <span key={t.team_id} className="chip" title={t.source === 'sso' ? 'From an SSO group' : 'Added by an admin'}>{teamName(t.team_id)} · {t.role}{t.source === 'sso' ? ' · SSO' : ''}</span>)}</td>
                  <td><button className="secondary small" onClick={() => act(() => api.updatePerson(ws, p.id, { active: !p.active }))}>{p.active ? 'Disable' : 'Enable'}</button></td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        <div className="row">
          <input value={invite.email} onChange={e => setInvite({ ...invite, email: e.target.value })} placeholder="name@company.com" />
          <input value={invite.github} onChange={e => setInvite({ ...invite, github: e.target.value })} placeholder="GitHub username (optional)" />
          <select value={invite.role} onChange={e => setInvite({ ...invite, role: e.target.value })}><option value="member">member</option><option value="admin">admin</option></select>
          <button disabled={!invite.email.includes('@')} onClick={() => act(async () => { await api.invite(ws, invite.email, invite.role, invite.github || null); setInvite({ email: '', role: 'member', github: '' }) })}>Add person</button>
        </div>
      </section>

      <section className="card">
        <h3>Teams and SSO groups</h3>
        <p className="muted small">Everyone in a team's SSO group joins the team when they sign in, and leaves it when they're removed from the group. A team gives its members a role on the team's projects.</p>
        <ul className="plain">
          {teams.map(t => <TeamRow key={t.id} team={t} onSave={(g, r) => act(() => api.setTeamGroup(ws, t.id, g, r))} />)}
        </ul>
        <div className="row">
          <input value={newTeam} onChange={e => setNewTeam(e.target.value)} placeholder="New team" />
          <button className="secondary" disabled={!newTeam.trim()} onClick={() => act(async () => { await api.createTeam(ws, newTeam.trim()); setNewTeam('') })}>Add team</button>
        </div>
      </section>

      <section className="card">
        <h3>Checkpoint roles</h3>
        <p className="muted small">Who signs off each delivery checkpoint. When nobody holds a role on a project, its owners and maintainers can sign off instead.</p>
        <ul className="plain">
          {roles.map(r => (
            <li key={r.id} className="row nowrap">
              <span><b>{label(r.role)}</b>: {r.email} <span className="muted small">on {projectName(r.project_id)}</span></span>
              <button className="link small" onClick={() => act(() => api.removeCheckpointRole(ws, r.id))}>remove</button>
            </li>
          ))}
        </ul>
        <div className="row">
          <select aria-label="Person" value={assign.user} onChange={e => setAssign({ ...assign, user: e.target.value })}>
            <option value="">Person…</option>{people.map(p => <option key={p.id} value={p.id}>{p.email}</option>)}
          </select>
          <select aria-label="Checkpoint role" value={assign.role} onChange={e => setAssign({ ...assign, role: e.target.value })}>
            {CHECKPOINT_ROLES.map(r => <option key={r} value={r}>{label(r)}</option>)}
          </select>
          <select aria-label="Project" value={assign.project} onChange={e => setAssign({ ...assign, project: e.target.value })}>
            <option value="">Every project</option>{projects.map(p => <option key={p.id} value={p.id}>{p.name}</option>)}
          </select>
          <button className="secondary" disabled={!assign.user} onClick={() => act(() => api.addCheckpointRole(ws, assign.user, assign.role, assign.project || null))}>Assign</button>
        </div>
      </section>

      <section className="card">
        <h3>Audit log</h3>
        {audit.length === 0 ? <p className="muted small">Nothing yet.</p> : (
          <ul className="plain audit">
            {audit.slice(0, 40).map((a, i) => (
              <li key={i} className={a.action === 'access.denied' ? 'denied' : ''}>
                <span className="muted small">{new Date(a.at).toLocaleString()}</span> <b>{a.actor}</b> {label(a.action.replace('.', ' '))} <code>{a.resource}</code>
              </li>
            ))}
          </ul>
        )}
      </section>
    </div>
  )
}

function TeamRow({ team, onSave }: { team: Team; onSave: (group: string, role: string) => void }) {
  const [group, setGroup] = useState(team.sso_group ?? '')
  const [role, setRole] = useState(team.sso_role ?? 'contributor')
  const dirty = group !== (team.sso_group ?? '') || role !== (team.sso_role ?? 'contributor')
  return (
    <li className="row nowrap">
      <b className="team-name">{team.name}</b>
      <input aria-label={`SSO group for ${team.name}`} value={group} onChange={e => setGroup(e.target.value)} placeholder="SSO group, e.g. eng-payments" />
      <select aria-label={`Role for ${team.name}`} value={role} onChange={e => setRole(e.target.value)}>
        {['viewer', 'contributor', 'maintainer'].map(r => <option key={r}>{r}</option>)}
      </select>
      <button className="secondary small" disabled={!dirty} onClick={() => onSave(group, role)}>Save</button>
    </li>
  )
}

// -- Project access tab ------------------------------------------------------------------------
export function ProjectAccessTab({ project, ws, me }: { project: Project; ws: string; me: Me | null }) {
  const [acc, setAcc] = useState<ProjectAccess | null>(null)
  const [people, setPeople] = useState<Person[]>([])
  const [add, setAdd] = useState({ user: '', role: 'contributor' })
  const [error, setError] = useState('')
  const isAdmin = !!me?.workspaces.find(w => w.id === ws && w.role === 'admin')
  const load = useCallback(() => {
    api.projectAccess(project.id).then(setAcc).catch(e => setError(String(e)))
    if (isAdmin) api.people(ws).then(setPeople).catch(() => {})
  }, [project.id, ws, isAdmin])
  useEffect(() => { load() }, [load])
  if (!acc) return <><ErrorLine error={error} />{!error && <p className="muted">Loading…</p>}</>
  const canManage = ['owner', 'maintainer'].includes(acc.my_role ?? '')
  const act = async (f: () => Promise<ProjectAccess>) => { try { setAcc(await f()); setError('') } catch (e) { setError(String(e)) } }
  const setMembers = (list: { user_id: string; role: string }[]) => act(() => api.setMembers(project.id, list))
  const current = acc.members.map(m => ({ user_id: m.user_id, role: m.role }))

  return (
    <>
      <section className="card">
        <div className="row spread">
          <div>
            <h3>{acc.restricted ? 'Restricted project' : 'Open project'}</h3>
            <p className="muted small">{acc.restricted
              ? `Only its members${acc.team ? `, the ${acc.team.name} team` : ''} and workspace admins can see this project and its repos. Others still see its repos on the system map, without details.`
              : 'Everyone in the workspace can see this project and contribute to it.'}</p>
          </div>
          {canManage && <button className="secondary" onClick={() => act(() => api.setRestricted(project.id, !acc.restricted))}>{acc.restricted ? 'Open to the workspace' : 'Restrict'}</button>}
        </div>
        <p className="small">Your role: <b>{acc.my_role ?? 'none'}</b>{acc.team && <> · Team: <b>{acc.team.name}</b>{acc.team.sso_group && <span className="muted"> (SSO group {acc.team.sso_group})</span>}</>}</p>
        <ErrorLine error={error} />
      </section>
      <section className="card">
        <h3>Members</h3>
        {acc.members.length === 0 && <p className="muted small">No one added directly{acc.team ? '; access comes from the team' : ''}.</p>}
        <ul className="plain">
          {acc.members.map(m => (
            <li key={m.user_id} className="row nowrap">
              <span className="grow">{m.name || m.email} <span className="muted small">{m.email}</span></span>
              {canManage ? (
                <select aria-label={`Role for ${m.email}`} value={m.role} onChange={e => setMembers(current.map(c => c.user_id === m.user_id ? { ...c, role: e.target.value } : c))}>
                  {PROJECT_ROLES.map(r => <option key={r}>{r}</option>)}
                </select>
              ) : <span className="badge">{m.role}</span>}
              {canManage && <button className="link small" onClick={() => setMembers(current.filter(c => c.user_id !== m.user_id))}>remove</button>}
            </li>
          ))}
        </ul>
        {canManage && isAdmin && (
          <div className="row">
            <select aria-label="Person to add" value={add.user} onChange={e => setAdd({ ...add, user: e.target.value })}>
              <option value="">Add person…</option>
              {people.filter(p => !acc.members.some(m => m.user_id === p.id)).map(p => <option key={p.id} value={p.id}>{p.email}</option>)}
            </select>
            <select aria-label="Role" value={add.role} onChange={e => setAdd({ ...add, role: e.target.value })}>{PROJECT_ROLES.map(r => <option key={r}>{r}</option>)}</select>
            <button className="secondary" disabled={!add.user} onClick={() => { setMembers([...current, { user_id: add.user, role: add.role }]); setAdd({ user: '', role: 'contributor' }) }}>Add</button>
          </div>
        )}
      </section>
      <section className="card">
        <h3>Who signs off</h3>
        <ul className="plain">
          {CHECKPOINT_ROLES.map(r => (
            <li key={r}><b>{label(r)}</b>: {acc.checkpoints[r]?.length ? acc.checkpoints[r].map(w => w.name || w.email).join(', ') : <span className="muted">nobody assigned, so owners and maintainers</span>}</li>
          ))}
        </ul>
      </section>
    </>
  )
}

// -- My tokens -----------------------------------------------------------------------------------
export function TokensPage() {
  const [rows, setRows] = useState<ApiTokenRow[]>([])
  const [name, setName] = useState('')
  const [days, setDays] = useState('90')
  const [made, setMade] = useState('')
  const [error, setError] = useState('')
  const load = useCallback(() => api.tokens().then(setRows).catch(e => setError(String(e))), [])
  useEffect(() => { load() }, [load])
  return (
    <div className="page">
      <h1>My tokens</h1>
      <p className="muted">Personal tokens let the <code>fil</code> CLI and MCP clients (Claude Code, Omni) act as you, with your access. Set <code>FILAMENT_TOKEN</code> to use one.</p>
      <ErrorLine error={error} />
      {made && (
        <section className="card warn">
          <p className="small">Copy it now; it is not shown again.</p>
          <code className="token">{made}</code>
          <div className="row"><button className="secondary small" onClick={() => { navigator.clipboard?.writeText(made).catch(() => {}) }}>Copy</button><button className="link small" onClick={() => setMade('')}>Done</button></div>
        </section>
      )}
      <section className="card">
        <ul className="plain">
          {rows.length === 0 && <li className="muted small">No tokens.</li>}
          {rows.map(t => (
            <li key={t.id} className="row nowrap">
              <span className="grow"><b>{t.name}</b> <code>{t.prefix}…</code> <span className="muted small">{t.expires_at ? `expires ${new Date(t.expires_at).toLocaleDateString()}` : 'never expires'}{t.last_used_at ? ` · last used ${new Date(t.last_used_at).toLocaleDateString()}` : ' · never used'}</span></span>
              <button className="link small" onClick={async () => { await api.revokeToken(t.id); load() }}>revoke</button>
            </li>
          ))}
        </ul>
        <div className="row">
          <input value={name} onChange={e => setName(e.target.value)} placeholder="What it's for, e.g. laptop CLI" />
          <select aria-label="Expiry" value={days} onChange={e => setDays(e.target.value)}>
            <option value="30">30 days</option><option value="90">90 days</option><option value="365">1 year</option>
          </select>
          <button disabled={!name.trim()} onClick={async () => {
            try { const t = await api.createToken(name.trim(), Number(days)); setMade(t.token); setName(''); load() } catch (e) { setError(String(e)) }
          }}>Create token</button>
        </div>
      </section>
    </div>
  )
}
