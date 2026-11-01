import { afterEach, describe, expect, it, vi } from "vitest"

import { ApiError, api, stream, upload } from "./api"
import { events, json } from "@/test/render"

const fetchStub = (res: () => Response) => vi.stubGlobal("fetch", vi.fn(async () => res()))
afterEach(() => vi.unstubAllGlobals())

describe("refusal text", () => {
  it("reads the server's detail on a plain request", async () => {
    fetchStub(() => json({ detail: "Ask an admin or an owner." }, 403))
    await expect(api.get("/api/admin/people")).rejects.toMatchObject({ status: 403, message: "Ask an admin or an owner." })
  })

  it("reads it on a stream, where the status line is empty under HTTP/2", async () => {
    fetchStub(() => json({ detail: "Wait for the answer to finish." }, 409))
    const err = await stream("/api/chat/sessions/s1/messages", () => {}, { body: {} }).catch((e) => e)
    expect(err).toBeInstanceOf(ApiError)
    expect(err.message).toBe("Wait for the answer to finish.")
  })

  it("reads it on an upload", async () => {
    fetchStub(() => json({ detail: "This file is too large." }, 413))
    await expect(upload(new File(["x"], "a.csv"))).rejects.toMatchObject({ status: 413, message: "This file is too large." })
  })

  it("falls back to the status line when the body is not JSON", async () => {
    fetchStub(() => new Response("oops", { status: 500, statusText: "Server Error" }))
    await expect(api.get("/x")).rejects.toMatchObject({ status: 500, message: "Server Error" })
  })
})

describe("stream", () => {
  it("passes each event and returns the last event id", async () => {
    fetchStub(() => events([{ type: "delta", text: "a" }, { type: "delta", text: "b" }]))
    const seen: unknown[] = []
    const last = await stream("/s", (e) => seen.push(e))
    expect(seen).toEqual([{ type: "delta", text: "a" }, { type: "delta", text: "b" }])
    expect(last).toBe("2")
  })

  it("sends Last-Event-ID to pick up where it left off", async () => {
    const f = vi.fn(async () => events([]))
    vi.stubGlobal("fetch", f)
    await stream("/s", () => {}, { lastEventId: "7" })
    const init = (f.mock.calls[0] as unknown as [string, RequestInit])[1]
    expect((init.headers as Record<string, string>)["Last-Event-ID"]).toBe("7")
  })
})

describe("paged lists", () => {
  it("returns the rows and the cursor the server names, and sends it back for the next page", async () => {
    const f = vi.fn()
      .mockResolvedValueOnce(new Response("[1,2]", { headers: { "X-Next-Cursor": "c1" } }))
      .mockResolvedValueOnce(new Response("[3]"))
    vi.stubGlobal("fetch", f)
    const first = await api.page<number>("/api/admin/audit")
    const last = await api.page<number>("/api/admin/audit", first.next)
    expect(first).toEqual({ rows: [1, 2], next: "c1" })
    expect(last).toEqual({ rows: [3], next: null })
    expect(f.mock.calls.map(([p]) => p)).toEqual(["/api/admin/audit", "/api/admin/audit?cursor=c1"])
  })
})
