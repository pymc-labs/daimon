import { QueryClient, QueryClientProvider } from "@tanstack/react-query"
import { render } from "@testing-library/react"
import { MemoryRouter, Route, Routes } from "react-router"

import { ApiError } from "@/lib/api"

import { Where } from "./Where"

export const newClient = () => new QueryClient({
  defaultOptions: { queries: { retry: (n, e) => !(e instanceof ApiError && e.status < 500) && n < 1, retryDelay: 1 } },
})

/** Render a page at a route inside a fresh query client; `where` shows the current location. */
export function renderAt(path: string, routePath: string, element: React.ReactNode, client = newClient()) {
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={[path]}>
        <Routes><Route path={routePath} element={element} /><Route path="*" element={<Where />} /></Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  )
}

/** A JSON answer for a stubbed `fetch`; `statusText` is empty, as under HTTP/2. */
export const json = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), { status, statusText: "", headers: { "Content-Type": "application/json" } })

/** An event stream that sends the given frames and then closes, or errors when `drop` is set. */
export function events(frames: unknown[], { drop = false }: { drop?: boolean } = {}) {
  const enc = new TextEncoder()
  let n = 0
  const body = new ReadableStream({
    start(c) {
      for (const f of frames) c.enqueue(enc.encode(`id: ${++n}\ndata: ${JSON.stringify(f)}\n\n`))
      if (drop) c.error(new TypeError("network error")); else c.close()
    },
  })
  return new Response(body, { status: 200, headers: { "Content-Type": "text/event-stream" } })
}
