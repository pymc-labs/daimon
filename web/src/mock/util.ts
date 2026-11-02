import { delay, HttpResponse } from "msw"

/** Mock latency: short enough to iterate, long enough to see loading states. */
export const wait = (ms = 180) => delay(ms)

export const unauthorized = (detail = "Sign in") => HttpResponse.json({ detail }, { status: 401 })
export const notFound = () => HttpResponse.json({ detail: "This doesn't exist." }, { status: 404 })
export const forbidden = (detail = "Ask an admin or an owner.") => HttpResponse.json({ detail }, { status: 403 })

/** A server-sent event stream from an async generator of JSON events (`id: n\ndata: {...}\n\n` frames). */
export function sse(events: () => AsyncGenerator<unknown>) {
  const enc = new TextEncoder()
  let n = 0
  const body = new ReadableStream({
    async start(ctrl) {
      for await (const e of events()) ctrl.enqueue(enc.encode(`id: ${++n}\ndata: ${JSON.stringify(e)}\n\n`))
      ctrl.close()
    },
  })
  return new HttpResponse(body, { headers: { "Content-Type": "text/event-stream", "Cache-Control": "no-store" } })
}

/** A list as the server pages it: a plain array of at most `max` rows (`?limit=` lowers it), `X-Next-Cursor` when more follow. */
export function paged<T>(request: Request, rows: T[], max = 100) {
  const q = new URL(request.url).searchParams
  const limit = Math.min(Number(q.get("limit")) || max, 200)
  const from = Number(q.get("cursor")) || 0
  const more = from + limit < rows.length
  return HttpResponse.json(rows.slice(from, from + limit), more ? { headers: { "X-Next-Cursor": String(from + limit) } } : undefined)
}

export const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms))
