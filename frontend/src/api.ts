export type Priority = 'urgent' | 'soon' | 'waiting' | 'healthy'

export interface Contact { id: string; customer_id: string; name: string; email: string; role: string }
export interface Interaction {
  id: string; customer_id: string; contact_id: string | null
  type: string; occurred_at: string; notes: string
}
export interface Facts { last_interaction_at: string | null; days_since_last_interaction: number | null }
export interface Analysis {
  priority: Priority; reason: string; next_action: string
  contact_id: string | null; due_date: string | null; snooze_until: string | null
  summary: string; good_to_know: string[]
  evidence: string[]; source: string; updating: boolean
}
export interface QueueItem {
  id: string; name: string; status: string; facts: Facts
  contact: Contact | null; analysis: Analysis
}
export interface Account {
  id: string; name: string; status: string; created_at: string
  contacts: Contact[]; interactions: Interaction[]; facts: Facts; analysis: Analysis
  history_summary: { notes_covered: number; summary: string; key_facts: string[] } | null
}
export interface Draft { subject: string; body: string; contact_id: string | null; warnings: string[] }
export interface ClientCard { id: string; name: string; status: string; priority: Priority; next_action: string; why: string | null }
export type Tool = 'list_by_status' | 'find_clients' | 'answer' | 'draft_email'
// Ask and drafts stream one event per line: the tool the AI picked (already run by the
// backend), then text as the AI writes it, then the finished draft for emails.
export type StreamEvent =
  | { type: 'tool'; tool: Tool; args: Record<string, string | null>; clients: ClientCard[]; searched?: { chunks: number; clients: number } }
  | { type: 'text'; text: string }
  | { type: 'draft'; draft: Draft }
  | { type: 'error'; message: string }
export interface NewClient { name: string; status: 'prospect' | 'customer'; contact_name: string; contact_email: string; contact_role: string }
export interface Meta { today: string; provider: string; model: string; email_provider: string }

async function req<T>(path: string, init?: RequestInit): Promise<T> {
  const r = await fetch(`/api${path}`, {
    headers: { 'Content-Type': 'application/json' }, ...init,
  })
  if (!r.ok) {
    const body = await r.json().catch(() => ({}))
    throw new Error(body.detail ?? `Request failed (${r.status})`)
  }
  return r.json()
}

const post = <T,>(path: string, body: unknown = {}) =>
  req<T>(path, { method: 'POST', body: JSON.stringify(body) })

// POST, then call onEvent for each JSON line as it arrives.
async function stream(path: string, body: unknown, onEvent: (e: StreamEvent) => void) {
  const r = await fetch(`/api${path}`, {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
  })
  if (!r.ok || !r.body) {
    const err = await r.json().catch(() => ({}))
    throw new Error(err.detail ?? `Request failed (${r.status})`)
  }
  const reader = r.body.getReader()
  const decoder = new TextDecoder()
  let buffer = ''
  for (;;) {
    const { done, value } = await reader.read()
    if (done) break
    buffer += decoder.decode(value, { stream: true })
    const lines = buffer.split('\n')
    buffer = lines.pop() ?? ''
    for (const line of lines) {
      if (!line.trim()) continue
      const event = JSON.parse(line) as StreamEvent
      if (event.type === 'error') throw new Error(event.message)
      onEvent(event)
    }
  }
}

export const api = {
  meta: () => req<Meta>('/meta'),
  today: () => req<QueueItem[]>('/today'),
  account: (id: string) => req<Account>(`/accounts/${id}`),
  search: (q: string) => req<Record<string, string[]>>(`/search?q=${encodeURIComponent(q)}`),
  reanalyze: (id: string) => post<{ queued: boolean }>(`/accounts/${id}/reanalyze`),
  draft: (id: string, onEvent: (e: StreamEvent) => void, instructions?: string) =>
    stream(`/accounts/${id}/draft`, { instructions }, onEvent),
  log: (id: string, body: { contact_id?: string | null; type: string; occurred_at?: string; notes: string }) =>
    post<{ id: string }>(`/accounts/${id}/interactions`, body),
  send: (id: string, body: { contact_id: string | null; subject: string; body: string }) =>
    post<{ provider: string; status: string; to: string }>(`/accounts/${id}/send`, body),
  createClient: (body: NewClient) => post<{ id: string }>('/clients', body),
  ask: (question: string, onEvent: (e: StreamEvent) => void) => stream('/ask', { question }, onEvent),
}
