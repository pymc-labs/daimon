import { api, qs } from "@/lib/api"
import type { Outcome, Selection } from "./types"

const base = "/api/github/connect"

export const connectApi = {
  start: (token: string) => api.post<Outcome>(`${base}/start`, { token }),
  callback: (state: string, code: string) => api.post<Outcome>(`${base}/callback`, { state, code }),
  setup: (state: string) => api.post<Outcome>(`${base}/setup`, { state }),
  cancel: (state: string) => api.post<Outcome>(`${base}/cancel`, { state }),
  view: (state: string) => api.get<Outcome>(`${base}/view${qs({ state })}`),
  save: (selection: Partial<Selection> & { state: string }) => api.post<Outcome>(`${base}/save`, selection),
}

/** The `state` a server-built link carries, so a step can run in place instead of leaving the page. */
export const stateOf = (url: string) => new URL(url, location.origin).searchParams.get("state")
