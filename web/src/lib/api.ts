export class ApiError extends Error {
  status: number
  constructor(status: number, message: string) {
    super(message)
    this.status = status
  }
}

// A deploy restarts the single server (~10 s): retry what is safe to repeat on network errors and 502/503/504.
const BACKOFF = [500, 1000, 2000, 4000, 8000]
const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms))

async function fetchRetry(path: string, init: RequestInit, retry: boolean): Promise<Response> {
  for (let i = 0; ; i++) {
    let res: Response | undefined
    try {
      res = await fetch(path, init)
    } catch (e) {
      if (!retry || i >= BACKOFF.length || init.signal?.aborted) throw e
    }
    if (res && !(retry && i < BACKOFF.length && [502, 503, 504].includes(res.status))) return res
    await sleep(BACKOFF[i]!)
    if (init.signal?.aborted) throw new DOMException("Aborted", "AbortError")
  }
}

// The server refuses writes without it: a cross-site form cannot set a custom header.
const CSRF = { "X-Daimon-Web": "1" }

/** The server's refusal text: the JSON `detail` when there is one, else the status line (empty under HTTP/2). */
async function errorOf(res: Response): Promise<ApiError> {
  let detail = res.statusText
  try {
    const j = await res.json()
    if (typeof j.detail === "string") detail = j.detail
  } catch { /* not json */ }
  return new ApiError(res.status, detail)
}

async function send(method: string, path: string, body: unknown, retry: boolean): Promise<Response> {
  const res = await fetchRetry(path, {
    method,
    credentials: "include",
    headers: body === undefined ? CSRF : { ...CSRF, "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  }, retry)
  if (!res.ok) throw await errorOf(res)
  return res
}

async function request<T>(method: string, path: string, body?: unknown, retry = method === "GET"): Promise<T> {
  return (await send(method, path, body, retry)).json() as Promise<T>
}

/** One page of a list: its rows, and the cursor of the next page when more exist. */
export type Page<T> = { rows: T[]; next: string | null }

export const api = {
  get: <T>(path: string) => request<T>("GET", path),
  /** `path` carries no query string. */
  page: async <T>(path: string, cursor?: string | null): Promise<Page<T>> => {
    const res = await send("GET", path + qs({ cursor }), undefined, true)
    return { rows: (await res.json()) as T[], next: res.headers.get("X-Next-Cursor") }
  },
  post: <T>(path: string, body: unknown = {}, retry = false) => request<T>("POST", path, body, retry),
  patch: <T>(path: string, body: unknown) => request<T>("PATCH", path, body, true),
  put: <T>(path: string, body: unknown) => request<T>("PUT", path, body, true),
  del: <T>(path: string) => request<T>("DELETE", path),
}

/** Read a server-sent event stream: calls onEvent per `data:` frame and returns the last `id:` seen. `retry` only for idempotent requests. */
export async function stream(
  path: string, onEvent: (e: any) => void,
  { body, signal, retry = false, lastEventId }: { body?: unknown; signal?: AbortSignal; retry?: boolean; lastEventId?: string } = {},
): Promise<string | undefined> {
  const headers: Record<string, string> = { ...CSRF }
  if (body !== undefined) headers["Content-Type"] = "application/json"
  if (lastEventId) headers["Last-Event-ID"] = lastEventId
  const res = await fetchRetry(path, {
    method: body === undefined ? "GET" : "POST", credentials: "include", signal, headers,
    body: body === undefined ? undefined : JSON.stringify(body),
  }, retry)
  if (!res.ok) throw await errorOf(res)
  if (!res.body) throw new ApiError(res.status, "")
  // An answer that starts no turn is one JSON event, not a stream.
  if (res.headers.get("content-type")?.includes("application/json")) { onEvent(await res.json()); return lastEventId }
  const reader = res.body.getReader()
  const decoder = new TextDecoder()
  let buf = ""
  let last = lastEventId
  for (;;) {
    const { done, value } = await reader.read()
    if (done) break
    buf += decoder.decode(value, { stream: true })
    let i
    while ((i = buf.indexOf("\n\n")) >= 0) {
      const frame = buf.slice(0, i)
      buf = buf.slice(i + 2)
      let data = ""
      for (const line of frame.split("\n")) {
        if (line.startsWith("id:")) last = line.slice(3).trim()
        else if (line.startsWith("data:")) data += line.slice(line[5] === " " ? 6 : 5)
      }
      if (data) onEvent(JSON.parse(data))
    }
  }
  return last
}

/** Upload one file for a later message; the server keeps it until the message uses it. */
export async function upload(file: File): Promise<{ id: string; name: string; size: number }> {
  const form = new FormData()
  form.append("file", file)
  const res = await fetch("/api/chat/uploads", { method: "POST", credentials: "include", headers: CSRF, body: form })
  if (!res.ok) throw await errorOf(res)
  return res.json()
}

export function qs(params: Record<string, string | number | undefined | null | string[]>) {
  const p = new URLSearchParams()
  for (const [k, v] of Object.entries(params)) {
    if (v === undefined || v === null || v === "" || (Array.isArray(v) && !v.length)) continue
    p.set(k, Array.isArray(v) ? v.join(",") : String(v))
  }
  const s = p.toString()
  return s ? `?${s}` : ""
}
