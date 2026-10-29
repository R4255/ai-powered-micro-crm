import { useCallback, useEffect, useState } from 'react'
import { api, type Draft, type Meta, type NewClient, type Priority, type QueueItem } from './api'
import AccountPanel from './AccountPanel'
import AskBar from './AskBar'

export const PRIORITY_LABEL: Record<Priority, string> = {
  urgent: 'To do today',
  soon: 'This week',
  waiting: 'On hold',
  healthy: 'All good',
}

const GROUPS: Priority[] = ['urgent', 'soon', 'waiting', 'healthy']

export function formatDate(iso: string | null) {
  if (!iso) return ''
  return new Date(iso + 'T00:00:00').toLocaleDateString(undefined, { month: 'short', day: 'numeric' })
}

function daysAgo(days: number | null) {
  if (days === null) return 'never'
  return days === 0 ? 'today' : days === 1 ? 'yesterday' : `${days} days ago`
}

export default function App() {
  const [meta, setMeta] = useState<Meta | null>(null)
  const [queue, setQueue] = useState<QueueItem[]>([])
  const [selected, setSelected] = useState<string | null>(window.location.hash.slice(1) || null)
  const [pendingDraft, setPendingDraft] = useState<{ id: string; draft: Draft } | null>(null)
  const [adding, setAdding] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [collapsed, setCollapsed] = useState<Set<Priority>>(new Set(['healthy']))
  const [search, setSearch] = useState('')
  const [matches, setMatches] = useState<Record<string, string[]> | null>(null)

  const load = useCallback(() => api.today().then(setQueue).catch(e => setError(e.message)), [])

  // A draft handed over from Ask belongs to one client only.
  useEffect(() => {
    if (pendingDraft && pendingDraft.id !== selected) setPendingDraft(null)
  }, [selected, pendingDraft])

  // Keep the open client in the URL so a refresh stays put.
  useEffect(() => {
    history.replaceState(null, '', selected ? `#${selected}` : window.location.pathname)
  }, [selected])

  useEffect(() => {
    api.meta().then(setMeta).catch(e => setError(e.message))
    load()
  }, [load])

  // Plain keyword search (no AI): filter the list as you type.
  useEffect(() => {
    if (!search.trim()) {
      setMatches(null)
      return
    }
    const t = setTimeout(() => api.search(search).then(setMatches).catch(e => setError(e.message)), 150)
    return () => clearTimeout(t)
  }, [search])

  // The backend worker updates clients in the background; refresh the list until it is done.
  const updating = queue.filter(q => q.analysis.updating).length
  useEffect(() => {
    if (!updating) return
    const t = setInterval(load, 3000)
    return () => clearInterval(t)
  }, [updating, load])

  function openDraft(id: string, draft: Draft) {
    setPendingDraft({ id, draft })
    setSelected(id)
  }

  async function createClient(body: NewClient) {
    const { id } = await api.createClient(body)
    setAdding(false)
    await load()
    setSelected(id)
  }

  function toggle(p: Priority) {
    const next = new Set(collapsed)
    if (next.has(p)) next.delete(p)
    else next.add(p)
    setCollapsed(next)
  }

  const aiOn = meta && meta.provider !== 'rules'
  const count = (p: Priority) => queue.filter(q => q.analysis.priority === p).length

  return (
    <div className="app">
      <header className="topbar">
        <div className="brand">
          <h1>Micro-CRM</h1>
          {meta && <span className="today">{formatDate(meta.today)}, {meta.today.slice(0, 4)}</span>}
        </div>
        <div className="topbar-right small muted">
          {!aiOn ? <span>AI off: using simple rules</span>
            : updating > 0 ? <span className="updating">AI is updating {updating} client{updating > 1 ? 's' : ''}…</span>
            : <span>AI: {meta?.model}</span>}
          <button className="btn" onClick={() => { setAdding(true); setSelected(null) }}>＋ New client</button>
        </div>
      </header>

      <AskBar onOpen={setSelected} onDraft={openDraft} />

      {error && <div className="banner">{error}</div>}

      <main className="layout">
        <section className="queue">
          <div className="search">
            <input
              placeholder="🔍  Search clients, people or notes…"
              value={search}
              onChange={e => setSearch(e.target.value)}
              onKeyDown={e => e.key === 'Escape' && setSearch('')}
            />
            {search && <button className="btn btn-ghost clear" onClick={() => setSearch('')} aria-label="Clear search">✕</button>}
          </div>
          {matches && Object.keys(matches).length === 0 && <p className="muted">No clients match "{search}".</p>}
          {GROUPS.map(p => {
            const items = queue.filter(q => q.analysis.priority === p && (!matches || q.id in matches))
            if (!items.length) return null
            const open = !!matches || !collapsed.has(p)
            return (
              <div key={p} className="group">
                <button className={`group-title p-${p}`} onClick={() => toggle(p)}>
                  <span className="caret">{open ? '▾' : '▸'}</span>
                  {PRIORITY_LABEL[p]} <span className="count">{items.length}</span>
                </button>
                {open && items.map(item => (
                  <QueueCard key={item.id} item={item} match={matches?.[item.id]}
                    active={selected === item.id} onClick={() => setSelected(item.id)} />
                ))}
              </div>
            )
          })}
        </section>

        <section className="detail">
          {selected ? (
            <AccountPanel id={selected} onChanged={load} onClose={() => setSelected(null)}
              initialDraft={pendingDraft?.id === selected ? pendingDraft.draft : undefined} />
          ) : adding ? (
            <NewClientForm onSave={createClient} onCancel={() => setAdding(false)} />
          ) : (
            <div className="empty">
              <h3>Good morning 👋</h3>
              <p>
                You have <strong>{count('urgent')} follow-ups today</strong> and {count('soon')} more this week.
              </p>
              <p className="muted">Click a client on the left to see what's going on and what to do next.</p>
            </div>
          )}
        </section>
      </main>
    </div>
  )
}

interface CardProps { item: QueueItem; match?: string[]; active: boolean; onClick: () => void }

function QueueCard({ item, match, active, onClick }: CardProps) {
  const a = item.analysis
  const headline =
    a.priority === 'waiting' ? `On hold${a.snooze_until ? ` until ${formatDate(a.snooze_until)}` : ''}: ${a.reason}`
    : a.priority === 'healthy' ? a.reason
    : a.next_action
  return (
    <button className={`card p-border-${a.priority} ${active ? 'active' : ''}`} onClick={onClick}>
      <div className="card-head">
        <span className="card-name">{item.name}</span>
        <span className={`status status-${item.status}`}>{item.status}</span>
      </div>
      <div className="card-action">{headline}</div>
      <div className="card-meta">
        {item.contact && <span>{item.contact.name}</span>}
        <span>last contact {daysAgo(item.facts.days_since_last_interaction)}</span>
      </div>
      {match && (
        <div className="match">
          {match[0]}{match.length > 1 && <span className="muted"> +{match.length - 1} more</span>}
        </div>
      )}
    </button>
  )
}

function NewClientForm({ onSave, onCancel }: { onSave: (c: NewClient) => Promise<void>; onCancel: () => void }) {
  const [form, setForm] = useState<NewClient>({ name: '', status: 'prospect', contact_name: '', contact_email: '', contact_role: '' })
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const set = (k: keyof NewClient) => (e: { target: { value: string } }) => setForm({ ...form, [k]: e.target.value })

  async function save() {
    setBusy(true)
    setError(null)
    try {
      await onSave(form)
    } catch (e) {
      setError((e as Error).message)
      setBusy(false)
    }
  }

  return (
    <div className="panel">
      <div className="panel-head">
        <h2>New client</h2>
        <button className="btn btn-ghost" onClick={onCancel} aria-label="Close">✕</button>
      </div>
      <div className="form">
        <label>Practice name<input value={form.name} onChange={set('name')} placeholder="e.g. Hillside Dental" /></label>
        <label>Status
          <select value={form.status} onChange={set('status')}>
            <option value="prospect">Prospect</option>
            <option value="customer">Customer</option>
          </select>
        </label>
        <label>Contact name<input value={form.contact_name} onChange={set('contact_name')} placeholder="e.g. Anna Lee" /></label>
        <label>Contact email<input value={form.contact_email} onChange={set('contact_email')} placeholder="anna@hillside.example" /></label>
        <label>Role<input value={form.contact_role} onChange={set('contact_role')} placeholder="e.g. Office Manager" /></label>
      </div>
      {error && <div className="banner">{error}</div>}
      <div className="row end">
        <button className="btn btn-primary" disabled={busy || !form.name.trim() || !form.contact_name.trim()} onClick={save}>
          {busy ? 'Saving…' : 'Add client'}
        </button>
      </div>
    </div>
  )
}
