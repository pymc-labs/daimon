import { keepPreviousData, useInfiniteQuery, useQuery } from "@tanstack/react-query"
import { useEffect, useMemo } from "react"

import { api } from "./api"
import type { AgentRef, Capability, ChatSession, Group, Me, Person, Provider } from "./types"

export const useMe = () => useQuery({ queryKey: ["me"], queryFn: () => api.get<Me>("/api/me"), staleTime: 60_000, refetchOnWindowFocus: true })
export const useProviders = () => useQuery({ queryKey: ["auth", "providers"], queryFn: () => api.get<Provider[]>("/auth/providers") })

/** A list the server pages: `data` is every row loaded so far, `loadMore` fetches the next page. */
export function usePaged<T>(key: readonly unknown[], path: string) {
  const q = useInfiniteQuery({
    queryKey: key,
    initialPageParam: null as string | null,
    queryFn: ({ pageParam }) => api.page<T>(path, pageParam),
    getNextPageParam: (last) => last.next,
  })
  const data = useMemo(() => q.data?.pages.flatMap((p) => p.rows), [q.data])
  return { data, error: q.error, isError: q.isError, refetch: q.refetch, hasMore: q.hasNextPage, loadMore: q.fetchNextPage, loadingMore: q.isFetchingNextPage }
}
export type Paged<T> = ReturnType<typeof usePaged<T>>

export const useSessions = () => usePaged<ChatSession>(["chat", "sessions"], "/api/chat/sessions")
/** Agents the signed-in person may chat with on the web. */
export const useChatAgents = () =>
  useQuery({ queryKey: ["chat", "agents"], queryFn: () => api.get<AgentRef[]>("/api/chat/agents"), staleTime: 60_000 })

/** The workspace's groups, for the admin pickers. */
export const useGroups = () =>
  useQuery({ queryKey: ["admin", "groups"], queryFn: () => api.get<Group[]>("/api/admin/groups"), staleTime: 60_000 })

/** The first few people whose name or email contains `q`, for the admin pickers. */
export const usePeopleSearch = (q: string) =>
  useQuery({
    queryKey: ["admin", "people", "search", q],
    queryFn: () => api.get<Pick<Person, "id" | "name" | "email">[]>(`/api/admin/people?limit=8&q=${encodeURIComponent(q)}`),
    placeholderData: keepPreviousData,
  })

export function useCan() {
  const { data: me } = useMe()
  return (cap: Capability) => !!me?.capabilities.includes(cap)
}

/** Global key handler; ignores keys typed into inputs unless `inInputs`. */
export function useKey(handler: (e: KeyboardEvent) => void, deps: unknown[], inInputs = false) {
  useEffect(() => {
    const on = (e: KeyboardEvent) => {
      const t = e.target as HTMLElement
      if (!inInputs && (t.isContentEditable || ["INPUT", "TEXTAREA", "SELECT"].includes(t.tagName))) return
      handler(e)
    }
    window.addEventListener("keydown", on)
    return () => window.removeEventListener("keydown", on)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, deps)
}

