import { useCallback, useEffect, useState } from 'react'
import { api, type Account, type Draft } from './api'
import { formatDate, PRIORITY_LABEL } from './App'

interface Props { id: string; onChanged: () => void; onClose: () => void; initialDraft?: Draft }

export default function AccountPanel({ id, onChanged, onClose, initialDraft }: Props) {
  const [account, setAccount] = useState<Account | null>(null)
  const [analyzing, setAnalyzing] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const load = useCallback(() => api.account(id).then(setAccount).catch(e => setError(e.message)), [id])

  useEffect(() => {
    setAccount(null)
    setError(null)
    load()
  }, [load])

  // While the worker updates this client in the background, poll until it finishes, then refresh the list.
  const updating = !!account?.analysis.updating
  useEffect(() => {
    if (!updating) return
    const t = setInterval(async () => {
      const next = await api.account(id)
      setAccount(next)
      if (!next.analysis.updating) onChanged()
    }, 2500)
    return () => clearInterval(t)
  }, [updating, id, onChanged])

  async function refresh() {
    await load()
    onChanged()
  }

  async function reanalyze() {
    setAnalyzing(true)
    setError(null)
    try {
      await api.reanalyze(id)
      await refresh()
    } catch (e) {
      setError((e as Error).message)
    } finally {
      setAnalyzing(false)
    }
  }

  if (!account) return <div className="panel"><p className="muted">Loading…</p></div>

  const a = account.analysis
  const contacts = Object.fromEntries(account.contacts.map(c => [c.id, c]))
  const contact = a.contact_id ? contacts[a.contact_id] : undefined
  const evidence = new Set(a.evidence)
  const goodToKnow = a.good_to_know

  return (
    <div className="panel">
      <div className="panel-head">
        <div>
          <h2>{account.name}</h2>
          <div className="muted">
            <span className={`status status-${account.status}`}>{account.status}</span>
            {contact && <> &nbsp;{contact.name}, {contact.role}</>}
          </div>
        </div>
        <button className="btn btn-ghost" onClick={onClose} aria-label="Close">✕</button>
      </div>

      {error && <div className="banner">{error}</div>}

      <div className={`next p-border-${a.priority}`}>
        <div className="next-label">
          <span className={`pill p-${a.priority}`}>{PRIORITY_LABEL[a.priority]}</span>
          {a.due_date && a.priority !== 'waiting' && a.priority !== 'healthy' && <span className="muted small">by {formatDate(a.due_date)}</span>}
          {a.snooze_until && a.priority === 'waiting' && <span className="muted small">until {formatDate(a.snooze_until)}</span>}
          {updating && <span className="updating">AI is updating…</span>}
        </div>
        <div className="next-action">{a.priority === 'healthy' ? 'Nothing to do right now' : a.next_action}</div>
        <div className="muted">Why: {a.reason}</div>
        {(a.priority !== 'healthy' || initialDraft) && (
          <DraftBox account={account} initialDraft={initialDraft} onSent={refresh} />
        )}
      </div>

      <section>
        <h3>Summary</h3>
        <p className="summary">{a.summary}</p>
        {goodToKnow.length > 0 && (
          <>
            <h3>Good to know</h3>
            <ul className="facts">{goodToKnow.map((x, i) => <li key={i}>{x}</li>)}</ul>
          </>
        )}
      </section>

      <NoteBox account={account} onSaved={refresh} />

      <section>
        <h3>History</h3>
        {account.history_summary && (
          <div className="earlier">
            <div className="muted small">Earlier history · AI summary of the first {account.history_summary.notes_covered} notes</div>
            <p>{account.history_summary.summary}</p>
            {account.history_summary.key_facts.length > 0 && (
              <ul className="facts">{account.history_summary.key_facts.map((f, i) => <li key={i}>{f}</li>)}</ul>
            )}
          </div>
        )}
        <ol className="timeline">
          {[...account.interactions].reverse().map(i => (
            <li key={i.id} className={evidence.has(i.id) ? 'evidence' : ''}
              title={evidence.has(i.id) ? 'The AI used this to decide the next step' : undefined}>
              <div className="tl-meta">
                {formatDate(i.occurred_at)} · {i.type}{i.contact_id && contacts[i.contact_id] ? ` with ${contacts[i.contact_id].name}` : ''}
              </div>
              <div>{i.notes}</div>
            </li>
          ))}
        </ol>
      </section>

      <div className="muted small footer">
        {a.source === 'rules' ? 'Based on simple rules (AI not run yet)' : `Analyzed by ${a.source}`} ·{' '}
        <button className="link" disabled={analyzing || updating} onClick={reanalyze}>{analyzing ? 'queuing…' : 're-analyze'}</button>
      </div>
    </div>
  )
}

function DraftBox({ account, initialDraft, onSent }: { account: Account; initialDraft?: Draft; onSent: () => void }) {
  const [draft, setDraft] = useState<Draft | null>(initialDraft ?? null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [copied, setCopied] = useState(false)
  const [sentNote, setSentNote] = useState<string | null>(null)
  const [writing, setWriting] = useState<string | null>(null)  // text so far, while the AI is writing

  useEffect(() => {
    setDraft(initialDraft ?? null)
    setSentNote(null)
  }, [account.id, initialDraft])

  // The draft streams in: show the words as they arrive, then the editable draft.
  async function generate() {
    setBusy(true)
    setError(null)
    setDraft(null)
    setWriting('')
    try {
      await api.draft(account.id, e => {
        if (e.type === 'text') setWriting(w => (w ?? '') + e.text)
        if (e.type === 'draft') setDraft(e.draft)
      })
    } catch (e) {
      setError((e as Error).message)
    } finally {
      setWriting(null)
      setBusy(false)
    }
  }

  // With the default "outbox" provider this only stores the email locally; nothing is sent.
  async function send() {
    if (!draft) return
    setBusy(true)
    setError(null)
    try {
      const r = await api.send(account.id, { contact_id: draft.contact_id, subject: draft.subject, body: draft.body })
      setDraft(null)
      setSentNote(r.provider === 'outbox' ? `Saved to the local outbox for ${r.to} (demo mode, not actually sent)` : `Sent to ${r.to}`)
      onSent()
    } catch (e) {
      setError((e as Error).message)
    } finally {
      setBusy(false)
    }
  }

  if (!draft && writing !== null) {
    return <div className="draft"><p className="pre muted">{writing || 'Writing…'}</p></div>
  }

  if (!draft) {
    return (
      <div className="row">
        <button className="btn btn-primary" disabled={busy} onClick={generate}>
          {busy ? 'Writing…' : '✉️ Write the email for me'}
        </button>
        {sentNote && <span className="sent-note">✓ {sentNote}</span>}
        {error && <span className="error-text">{error}</span>}
      </div>
    )
  }

  const to = account.contacts.find(c => c.id === draft.contact_id)
  return (
    <div className="draft">
      {to && <div className="muted small">To: {to.name} &lt;{to.email}&gt;</div>}
      <input className="subject" value={draft.subject} onChange={e => setDraft({ ...draft, subject: e.target.value })} />
      <textarea rows={8} value={draft.body} onChange={e => setDraft({ ...draft, body: e.target.value })} />
      {draft.warnings.length > 0 && (
        <div className="warn">⚠️ Please check: {draft.warnings.join(', ')} {draft.warnings.length > 1 ? "don't" : "doesn't"} appear in the notes.</div>
      )}
      <div className="row end">
        <button className="link" disabled={busy} onClick={generate}>{busy ? 'Writing…' : 'Try again'}</button>
        <button className="btn" onClick={() => {
          navigator.clipboard.writeText(`Subject: ${draft.subject}\n\n${draft.body}`)
          setCopied(true)
          setTimeout(() => setCopied(false), 1500)
        }}>{copied ? 'Copied ✓' : 'Copy'}</button>
        <button className="btn btn-primary" disabled={busy} onClick={send}>{busy ? 'Sending…' : 'Send'}</button>
      </div>
      {error && <div className="error-text">{error}</div>}
    </div>
  )
}

function NoteBox({ account, onSaved }: { account: Account; onSaved: () => void }) {
  const [notes, setNotes] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  async function submit() {
    if (!notes.trim()) return
    setBusy(true)
    setError(null)
    try {
      await api.log(account.id, { notes, type: 'note' })
      setNotes('')
      onSaved()
    } catch (e) {
      setError((e as Error).message)
    } finally {
      setBusy(false)
    }
  }

  return (
    <section>
      <h3>Add a note</h3>
      <div className="row">
        <input
          placeholder="What happened? e.g. Called Sarah, partners approved 2 locations"
          value={notes}
          onChange={e => setNotes(e.target.value)}
          onKeyDown={e => e.key === 'Enter' && submit()}
        />
        <button className="btn" disabled={busy || !notes.trim()} onClick={submit}>
          {busy ? 'Saving…' : 'Save'}
        </button>
      </div>
      {error && <div className="banner">{error}</div>}
    </section>
  )
}
