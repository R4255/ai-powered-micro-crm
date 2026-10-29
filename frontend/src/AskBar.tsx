import { useState } from 'react'
import { api, type ClientCard, type Draft, type StreamEvent, type Tool } from './api'
import { PRIORITY_LABEL } from './App'

interface Props { onOpen: (id: string) => void; onDraft: (id: string, draft: Draft) => void }

// What the AI decided to do, in plain words.
const TOOL_LABEL: Record<Tool, string> = {
  list_by_status: 'listed clients by status',
  find_clients: 'searched the notes for matching clients',
  answer: 'searched the notes, then wrote an answer',
  draft_email: 'is writing a draft email',
}

interface Result {
  tool: Tool; args: Record<string, string | null>; clients: ClientCard[]
  searched?: { chunks: number; clients: number }; text: string; draft: Draft | null; seconds: number
}

export default function AskBar({ onOpen, onDraft }: Props) {
  const [question, setQuestion] = useState('')
  const [result, setResult] = useState<Result | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  async function ask() {
    if (!question.trim()) return
    setBusy(true)
    setError(null)
    setResult(null)
    const start = performance.now()
    try {
      await api.ask(question, (e: StreamEvent) => {
        if (e.type === 'tool') {
          const seconds = (performance.now() - start) / 1000
          setResult({ tool: e.tool, args: e.args, clients: e.clients, searched: e.searched, text: '', draft: null, seconds })
        }
        if (e.type === 'text') setResult(r => r && { ...r, text: r.text + e.text })
        if (e.type === 'draft') setResult(r => r && { ...r, draft: e.draft })
      })
    } catch (e) {
      setError((e as Error).message)
    } finally {
      setBusy(false)
    }
  }

  const args = result && Object.entries(result.args).filter(([, v]) => v).map(([k, v]) => `${k}: "${v}"`).join(', ')

  return (
    <div className="ask">
      <div className="row">
        <input
          placeholder="✨  Ask AI… e.g. who uses Dentrix? · who is on hold? · email Julia about the call"
          value={question}
          onChange={e => setQuestion(e.target.value)}
          onKeyDown={e => e.key === 'Enter' && ask()}
        />
        <button className="btn btn-primary" disabled={busy} onClick={ask}>{busy ? 'Thinking…' : 'Ask AI'}</button>
      </div>
      {error && <div className="banner">{error}</div>}
      {busy && !result && <div className="answer"><p className="muted">Choosing what to do…</p></div>}
      {result && (
        <div className="answer">
          <button className="btn btn-ghost close" onClick={() => setResult(null)} aria-label="Dismiss">✕</button>
          <div className="muted small tool-line">
            AI {TOOL_LABEL[result.tool]}{args ? ` (${args})` : ''} · {result.seconds.toFixed(1)}s
            {result.searched && ` · ${result.searched.chunks} notes from ${result.searched.clients} clients`}
          </div>

          {result.tool === 'draft_email' ? (
            <DraftResult result={result} busy={busy} onDraft={onDraft} />
          ) : (
            <>
              {result.text && <Markdown text={result.text} />}
              {result.tool === 'answer' && busy && !result.text && <p className="muted">Writing…</p>}
              {result.clients.length > 0 && (
                <>
                  {result.tool === 'answer' && <div className="muted small">Based on notes from:</div>}
                  <ul className="ask-cards">
                    {result.clients.map(c => (
                      <li key={c.id}>
                        <button className="ask-card" onClick={() => onOpen(c.id)}>
                          <span className="card-name">{c.name}</span>
                          <span className={`pill p-${c.priority}`}>{PRIORITY_LABEL[c.priority]}</span>
                          <span className="muted">{c.why ?? c.next_action}</span>
                        </button>
                      </li>
                    ))}
                  </ul>
                </>
              )}
            </>
          )}
        </div>
      )}
    </div>
  )
}

function DraftResult({ result, busy, onDraft }: { result: Result; busy: boolean; onDraft: Props['onDraft'] }) {
  const client = result.clients[0]
  return (
    <div className="ask-draft">
      {client && <div className="muted small">To {client.name}</div>}
      <p className="pre">{result.draft ? `Subject: ${result.draft.subject}\n\n${result.draft.body}` : result.text || 'Writing…'}</p>
      {client && result.draft && !busy && (
        <button className="btn btn-primary" onClick={() => onDraft(client.id, result.draft!)}>
          Open in {client.name} to edit and send →
        </button>
      )}
    </div>
  )
}

// Minimal markdown: paragraphs, "- " bullets and **bold**. Enough for model answers.
function Markdown({ text }: { text: string }) {
  const inline = (s: string) =>
    s.split(/(\*\*[^*]+\*\*)/g).map((part, i) =>
      part.startsWith('**') ? <strong key={i}>{part.slice(2, -2)}</strong> : part)
  const blocks = text.trim().split(/\n{2,}/)
  return (
    <div className="md">
      {blocks.map((block, i) => {
        const lines = block.split('\n')
        if (lines.every(l => /^\s*[-*] /.test(l))) {
          return <ul key={i}>{lines.map((l, j) => <li key={j}>{inline(l.replace(/^\s*[-*] /, ''))}</li>)}</ul>
        }
        return <p key={i}>{lines.map((l, j) => <span key={j}>{inline(l.replace(/^\s*[-*] /, '• '))}<br /></span>)}</p>
      })}
    </div>
  )
}
