import { QueryClient } from "@tanstack/react-query"
import { renderHook, waitFor } from "@testing-library/react"
import { afterEach, describe, expect, it, vi } from "vitest"

import type { Me } from "@/lib/types"
import { events, json } from "@/test/render"
import type { Msg, SessionDetail } from "./types"
import { hydrate, loadEarlier, send, useThread } from "./turns"

const reply = (content: string, extra: Partial<Msg> = {}): Msg => ({ id: "m2", role: "assistant", content, created_at: "2030-01-01T00:00:00Z", ...extra })
const detail = (messages: Msg[], resume_from?: string) => ({ session: { id: "s" }, messages, resume_from }) as unknown as SessionDetail

/** Routes `fetch` by method and path; each route answers in turn, the last one repeats. */
function server(routes: Record<string, (() => Response)[]>) {
  const calls: Record<string, number> = {}
  const f = vi.fn(async (path: string, init?: RequestInit) => {
    const key = `${init?.method ?? "GET"} ${path}`
    const list = routes[key] ?? [() => json({ detail: "Not found" }, 404)]
    const i = calls[key] = (calls[key] ?? -1) + 1
    return list[Math.min(i, list.length - 1)]!()
  })
  vi.stubGlobal("fetch", f)
  return f
}
afterEach(() => vi.unstubAllGlobals())

const user: Msg = { id: "m1", role: "user", content: "hi", created_at: "2030-01-01T00:00:00Z" }

describe("a turn that loses its connection", () => {
  it("shows Reconnecting, re-attaches from the last event and finishes the reply", async () => {
    const qc = new QueryClient()
    const sid = "s-reattach"
    hydrate(sid, { messages: [] })
    const f = server({
      [`POST /api/chat/sessions/${sid}/messages`]: [() => events([{ type: "delta", text: "Hel" }], { drop: true })],
      [`GET /api/chat/sessions/${sid}`]: [() => json(detail([user, reply("Hel", { streaming: true })], "1"))],
      [`GET /api/chat/sessions/${sid}/stream`]: [() => events([{ type: "delta", text: "lo" }, { type: "done", message: reply("Hello") }])],
    })
    const { result } = renderHook(() => useThread(sid))
    void send(qc, sid, "hi")
    await waitFor(() => expect(result.current.reconnecting).toBe(true))
    await waitFor(() => expect(result.current.busy).toBe(false), { timeout: 4000 })
    expect(result.current.reconnecting).toBe(false)
    expect(result.current.messages.at(-1)).toMatchObject({ content: "Hello" })
    const attach = f.mock.calls.find(([p]) => p === `/api/chat/sessions/${sid}/stream`)!
    expect(((attach[1] as RequestInit).headers as Record<string, string>)["Last-Event-ID"]).toBe("1")
  })

  it("does not call a dropped reply stopped when the server still runs it", async () => {
    const qc = new QueryClient()
    const sid = "s-twice"
    hydrate(sid, { messages: [] })
    server({
      [`POST /api/chat/sessions/${sid}/messages`]: [() => events([{ type: "delta", text: "a" }], { drop: true })],
      [`GET /api/chat/sessions/${sid}`]: [() => json(detail([user, reply("a", { streaming: true })], "1"))],
      // The first re-attach drops again; the second finishes.
      [`GET /api/chat/sessions/${sid}/stream`]: [
        () => events([{ type: "delta", text: "b" }], { drop: true }),
        () => events([{ type: "done", message: reply("ab") }]),
      ],
    })
    const { result } = renderHook(() => useThread(sid))
    void send(qc, sid, "hi")
    await waitFor(() => expect(result.current.busy).toBe(true))
    await waitFor(() => expect(result.current.busy).toBe(false), { timeout: 6000 })
    const last = result.current.messages.at(-1)!
    expect(last.content).toBe("ab")
    expect(last.stopped).toBeUndefined()
    expect(last.card).toBeUndefined()
  })

  it("takes the saved reply when the stream is gone for good (404)", async () => {
    const qc = new QueryClient()
    const sid = "s-gone"
    hydrate(sid, { messages: [] })
    server({
      [`POST /api/chat/sessions/${sid}/messages`]: [() => events([{ type: "delta", text: "a" }], { drop: true })],
      [`GET /api/chat/sessions/${sid}`]: [
        () => json(detail([user, reply("a", { streaming: true })], "1")),
        () => json(detail([user, reply("a finished elsewhere")])),
      ],
      [`GET /api/chat/sessions/${sid}/stream`]: [() => json({ detail: "Not found" }, 404)],
    })
    const { result } = renderHook(() => useThread(sid))
    void send(qc, sid, "hi")
    await waitFor(() => expect(result.current.busy).toBe(true))
    await waitFor(() => expect(result.current.busy).toBe(false), { timeout: 4000 })
    expect(result.current.messages.at(-1)).toMatchObject({ content: "a finished elsewhere" })
  })
})

describe("a refused turn", () => {
  it("shows the server's words on the reply", async () => {
    const qc = new QueryClient()
    const sid = "s-refused"
    hydrate(sid, { messages: [] })
    server({ [`POST /api/chat/sessions/${sid}/messages`]: [() => json({ detail: "This agent isn't available to you. Ask an admin." }, 403)] })
    const { result } = renderHook(() => useThread(sid))
    await send(qc, sid, "hi")
    expect(result.current.messages.at(-1)?.card).toMatchObject({ kind: "refusal", body: "This agent isn't available to you. Ask an admin." })
  })
})

describe("the credit meter", () => {
  it("is read from the meter after a reply, without fetching the account again", async () => {
    const qc = new QueryClient()
    qc.setQueryData<Me>(["me"], { credit: { left_usd: 10, total_usd: 20, window: "month", resets_at: null } } as Me)
    const sid = "s-meter"
    hydrate(sid, { messages: [] })
    const f = server({
      [`POST /api/chat/sessions/${sid}/messages`]: [() => events([{ type: "done", message: reply("ok", { cost_usd: 0.25 }) }])],
      "GET /api/credits/meter": [() => json({ left_usd: 9.7, total_usd: 20, window: "month", resets_at: null })],
    })
    await send(qc, sid, "hi")
    await waitFor(() => expect(qc.getQueryData<Me>(["me"])?.credit?.left_usd).toBe(9.7))
    expect(f.mock.calls.some(([p]) => p === "/api/me")).toBe(false)
  })

  it("is read after a reply with no cost too, as an unpriced call still spends", async () => {
    const qc = new QueryClient()
    qc.setQueryData<Me>(["me"], { credit: { left_usd: 10, total_usd: 20, window: "month", resets_at: null } } as Me)
    const sid = "s-unpriced"
    hydrate(sid, { messages: [] })
    server({
      [`POST /api/chat/sessions/${sid}/messages`]: [() => events([{ type: "done", message: reply("ok") }])],
      "GET /api/credits/meter": [() => json({ left_usd: 4, total_usd: 20, window: "month", resets_at: null })],
    })
    await send(qc, sid, "hi")
    await waitFor(() => expect(qc.getQueryData<Me>(["me"])?.credit?.left_usd).toBe(4))
  })
})

describe("earlier messages", () => {
  const msg = (n: number): Msg => ({ id: `m${n}`, role: "user", content: `${n}`, created_at: "2030-01-01T00:00:00Z" })

  it("load in front of the newest page and survive a refetch of it", async () => {
    const sid = "s-earlier"
    hydrate(sid, { messages: [msg(3), msg(4)], next: 3 })
    const f = server({ [`GET /api/chat/sessions/${sid}?before=3`]: [() => json({ session: { id: sid }, messages: [msg(1), msg(2)] })] })
    const { result } = renderHook(() => useThread(sid))
    await loadEarlier(sid)
    expect(result.current.messages.map((m) => m.id)).toEqual(["m1", "m2", "m3", "m4"])
    expect(result.current.next).toBeUndefined()
    hydrate(sid, { messages: [msg(3), msg(4), msg(5)], next: 3 })
    await waitFor(() => expect(result.current.messages.map((m) => m.id)).toEqual(["m1", "m2", "m3", "m4", "m5"]))
    await loadEarlier(sid)
    expect(f).toHaveBeenCalledTimes(1)
  })
})
