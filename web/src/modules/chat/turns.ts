// Turn store: threads and running streams live outside React, so a turn survives route changes
// (the shell remounts the page when a new chat gets its id) and keeps streaming while you look elsewhere.
import { useMutation, useQueryClient, type QueryClient } from "@tanstack/react-query"
import { useSyncExternalStore } from "react"
import { useMatch, useNavigate } from "react-router"
import { toast } from "sonner"

import { api, ApiError, qs, stream, upload } from "@/lib/api"
import { useConfirm } from "@/lib/confirm"
import type { ChatSession, CreditMeter, Me } from "@/lib/types"
import type { Card, CardAnswer, Msg, Notice, SessionDetail, StreamEvent } from "./types"

/** `monitoring` is set when admins began reading this chat after the page opened. */
type Thread = { messages: Msg[]; busy: boolean; reconnecting: boolean; next?: number; loadingEarlier: boolean; monitoring?: Notice }
const threads = new Map<string, Thread>()
const ctrls = new Map<string, AbortController>()
const subs = new Set<() => void>()
const EMPTY: Thread = { messages: [], busy: false, reconnecting: false, loadingEarlier: false }

const set = (sid: string, fn: (t: Thread) => Thread) => {
  threads.set(sid, fn(threads.get(sid) ?? EMPTY))
  subs.forEach((f) => f())
}
export const useThread = (sid?: string) =>
  useSyncExternalStore((f) => { subs.add(f); return () => { subs.delete(f) } }, () => (sid && threads.get(sid)) || EMPTY)

/** The newest page of a chat, with any earlier messages already loaded kept in front of it. */
export function withDetail(t: Thread, d: Pick<SessionDetail, "messages" | "next">): Thread {
  const at = d.messages.length ? t.messages.findIndex((m) => m.id === d.messages[0]!.id) : -1
  return at > 0 ? { ...t, messages: [...t.messages.slice(0, at), ...d.messages] } : { ...t, messages: d.messages, next: d.next }
}
export const hydrate = (sid: string, d: Pick<SessionDetail, "messages" | "next">) => { if (!threads.get(sid)?.busy) set(sid, (t) => withDetail(t, d)) }

/** Fetch the page before the oldest message shown and put it in front. */
export async function loadEarlier(sid: string) {
  const t = threads.get(sid)
  if (!t || t.next === undefined || t.loadingEarlier) return
  set(sid, (cur) => ({ ...cur, loadingEarlier: true }))
  try {
    const d = await api.get<SessionDetail>(`/api/chat/sessions/${sid}${qs({ before: t.next })}`)
    set(sid, (cur) => {
      const known = new Set(cur.messages.map((m) => m.id))
      return { ...cur, messages: [...d.messages.filter((m) => !known.has(m.id)), ...cur.messages], next: d.next }
    })
  } finally {
    set(sid, (cur) => ({ ...cur, loadingEarlier: false }))
  }
}

/** Forget a deleted chat and cut its stream. */
export function evict(sid: string) {
  ctrls.get(sid)?.abort()
  ctrls.delete(sid)
  threads.delete(sid)
  subs.forEach((f) => f())
}

/** Delete a thread once confirmed, leaving it if it is open. */
export function useDeleteThread() {
  const qc = useQueryClient()
  const navigate = useNavigate()
  const confirm = useConfirm()
  const open = useMatch("/chat/:id")?.params.id
  const del = useMutation({
    mutationFn: (s: ChatSession) => api.del(`/api/chat/sessions/${s.id}`),
    onSuccess: (_, s) => {
      evict(s.id)
      qc.removeQueries({ queryKey: ["chat", "session", s.id] })
      qc.invalidateQueries({ queryKey: ["chat", "sessions"] })
      if (s.id === open) navigate("/chat")
      toast("Thread deleted")
    },
  })
  return async (s: ChatSession) => { if (await confirm({ title: "Delete thread?", body: s.title, action: "Delete", destructive: true })) del.mutate(s) }
}

/** Forget every chat and cut every stream: they belong to the workspace just left. */
export const reset = () => [...threads.keys()].forEach(evict)

/** Ask the server to stop the turn; the stream then ends with the stopped reply. Cut it locally if that fails or stalls. */
export async function stop(sid: string) {
  const ctrl = ctrls.get(sid)
  if (!ctrl) return
  try { await api.post(`/api/chat/sessions/${sid}/stop`) } catch { ctrl.abort() }
  setTimeout(() => { if (ctrls.get(sid) === ctrl) ctrl.abort() }, 10_000)
}

const patchLast = (sid: string, fn: (m: Msg) => Msg) =>
  set(sid, (t) => (t.messages.length ? { ...t, messages: [...t.messages.slice(0, -1), fn(t.messages[t.messages.length - 1]!)] } : t))
const placeholder = (): Msg => ({ id: `tmp-${crypto.randomUUID()}`, role: "assistant", content: "", steps: [], created_at: new Date().toISOString(), streaming: true })

/** A request the server refused says why in its own words; a dropped connection has none. */
export function refusal(e: unknown): Card {
  if (e instanceof ApiError && e.message) return { id: "c_err", kind: "refusal", title: "Not sent.", body: e.message }
  return { id: "c_err", kind: "refusal", title: "Not sent.", body: "Connection lost.", help: "Try again in a moment." }
}

function apply(sid: string, e: StreamEvent) {
  if (e.type === "step") {
    patchLast(sid, (m) => {
      const steps = [...(m.steps ?? [])]
      const i = steps.findIndex((s) => s.id === e.step.id)
      if (i >= 0) steps[i] = e.step
      else steps.push(e.step)
      return { ...m, steps }
    })
  } else if (e.type === "delta") patchLast(sid, (m) => ({ ...m, content: m.content + e.text }))
  else if (e.type === "text") patchLast(sid, (m) => ({ ...m, content: e.text }))
  else if (e.type === "card") patchLast(sid, (m) => ({ ...m, card: e.card }))
  else if (e.type === "done") patchLast(sid, () => e.message)
  else if (e.type === "monitoring") set(sid, (t) => ({ ...t, monitoring: e.notice }))
  else if (e.type === "settled") {
    // A card answered with no follow-up: the card's message is updated, and the placeholder goes.
    set(sid, (t) => ({ ...t, messages: t.messages.slice(0, -1).map((m) => (m.id === e.message?.id ? e.message : m)) }))
  }
}

const RETRY = [500, 1000, 2000, 4000, 8000]
const sleep = (ms: number, signal: AbortSignal) =>
  new Promise<void>((resolve) => {
    const t = setTimeout(resolve, ms)
    signal.addEventListener("abort", () => { clearTimeout(t); resolve() }, { once: true })
  })

/** The saved state of a chat, in place of whatever the stream showed. */
async function refetchThread(sid: string) {
  const d = await api.get<SessionDetail>(`/api/chat/sessions/${sid}`)
  set(sid, (t) => withDetail(t, d))
  return d
}

/** The meter moves by what a reply cost: ask for it alone, not for all of `me`. */
async function refreshMeter(qc: QueryClient) {
  try {
    const credit = await api.get<CreditMeter | null>("/api/credits/meter")
    qc.setQueryData<Me>(["me"], (m) => m && { ...m, credit })
  } catch { /* the next visit reads it */ }
}

type RunOpts = { body?: () => Promise<unknown>; from?: string; retry?: boolean; sent?: (d: SessionDetail) => boolean }

/**
 * Stream one turn into the thread. `body` posts; without it, re-attach to the running turn after event `from`.
 * A dropped stream re-attaches in a loop with backoff while the server says the turn runs.
 */
async function run(qc: QueryClient, sid: string, path: string, { body, from, retry = false, sent }: RunOpts) {
  const ctrl = new AbortController()
  const { signal } = ctrl
  ctrls.set(sid, ctrl)
  set(sid, (t) => ({ ...t, busy: true }))
  let first = true
  let done = false
  let last = from
  let accepted = !body
  const on = (e: StreamEvent) => {
    if (threads.get(sid)?.reconnecting) set(sid, (t) => ({ ...t, reconnecting: false }))
    if (first) { first = false; qc.invalidateQueries({ queryKey: ["chat", "sessions"] }) }   // the server now says running
    if (e.type === "done" || e.type === "settled") done = true
    if (e.type === "done") void refreshMeter(qc)
    apply(sid, e)
  }
  const attach = async () => { last = await stream(`/api/chat/sessions/${sid}/stream`, on, { signal, retry: true, lastEventId: last }) }
  try {
    try {
      const onOpen = () => { accepted = true }
      last = await stream(path, on, { body: body && await body(), signal, retry: retry || !body, lastEventId: last, onOpen })
    } catch (e) {
      // Refused or aborted: no reply to re-attach to. A post with no answer may still have reached the server: ask it.
      if (e instanceof ApiError || signal.aborted) throw e
      if (!accepted) {
        const d = await api.get<SessionDetail>(`/api/chat/sessions/${sid}`).catch(() => undefined)
        if (!d || (d.resume_from === undefined && !sent?.(d))) throw e
        if (d.resume_from === undefined) { set(sid, (t) => withDetail(t, d)); done = true }
      }
    }
    for (let i = 0; !done && !signal.aborted; i++) {
      set(sid, (t) => ({ ...t, reconnecting: true }))
      await sleep(RETRY[Math.min(i, RETRY.length - 1)]!, signal)
      if (signal.aborted) break
      let detail: SessionDetail
      try { detail = await api.get<SessionDetail>(`/api/chat/sessions/${sid}`) } catch (e) {
        if (e instanceof ApiError && e.status === 404) { evict(sid); return }
        if (e instanceof ApiError) throw e
        if (i >= 40) throw e
        continue
      }
      if (detail.resume_from === undefined) { set(sid, (t) => withDetail(t, detail)); done = true; break }
      if (last === undefined) { set(sid, (t) => withDetail(t, detail)); last = detail.resume_from }
      try { await attach() } catch (e) {
        if (e instanceof ApiError && e.status === 404) { await refetchThread(sid).catch(() => undefined); done = true; break }
        if (e instanceof ApiError || signal.aborted) throw e
        if (i >= 40) throw e
      }
    }
  } catch (e) {
    if (signal.aborted) patchLast(sid, (m) => ({ ...m, streaming: false, stopped: true, steps: m.steps?.map((s) => (s.status === "running" ? { ...s, status: "error" as const } : s)) }))
    else patchLast(sid, (m) => ({ ...m, streaming: false, card: refusal(e) }))
  }
  patchLast(sid, (m) => (m.streaming ? { ...m, streaming: false } : m))
  if (ctrls.get(sid) === ctrl) ctrls.delete(sid)
  if (threads.has(sid)) set(sid, (t) => ({ ...t, busy: false, reconnecting: false }))
  qc.invalidateQueries({ queryKey: ["chat", "sessions"] })
}

/** Re-attach to a turn still running on the server (after a reload or from another tab). */
export function resume(qc: QueryClient, sid: string, from: string) {
  if (!ctrls.has(sid)) void run(qc, sid, `/api/chat/sessions/${sid}/stream`, { from })
}

export function send(qc: QueryClient, sid: string, content: string, files: File[] = []) {
  const id = crypto.randomUUID()
  const shown = files.map((f) => ({ name: f.name, size: f.size }))
  set(sid, (t) => ({
    ...t,
    messages: [...t.messages, { id, role: "user", content, created_at: new Date().toISOString(), attachments: shown.length ? shown : undefined }, placeholder()],
  }))
  // Files upload first; the message names them by id. The message id makes a retried post safe.
  const body = async () => ({ id, content, attachments: await Promise.all(files.map(upload)) })
  return run(qc, sid, `/api/chat/sessions/${sid}/messages`, { body, retry: true, sent: (d) => d.messages.some((m) => m.id === id) })
}

export function answer(qc: QueryClient, sid: string, cardId: string, a: CardAnswer) {
  set(sid, (t) => ({
    ...t,
    messages: [...t.messages.map((m) => (m.card?.id === cardId ? { ...m, card: settle(m.card, a) } : m)), placeholder()],
  }))
  const sent = (d: SessionDetail) => d.messages.some((m) => m.card?.id === cardId && m.card.kind !== "refusal" && m.card.state !== "pending")
  return run(qc, sid, `/api/chat/sessions/${sid}/cards/${cardId}`, { body: async () => a, sent })
}

/** The refusal card's way back: the agent answers on Claude again; says what changed. */
export async function switchBack(qc: QueryClient, sid: string) {
  const { line } = await api.post<{ line: string }>(`/api/chat/sessions/${sid}/switch-back`)
  await refetchThread(sid).catch(() => undefined)
  void qc.invalidateQueries({ queryKey: ["chat", "sessions"] })
  return line
}

function settle(c: Card, a: CardAnswer): Card {
  if (c.kind === "approval") return { ...c, state: a.answer === "deny" ? "denied" : a.answer === "always" ? "always" : "approved" }
  if (c.kind === "credential") return { ...c, state: a.answer === "skip" ? "skipped" : "saved" }
  return c
}
