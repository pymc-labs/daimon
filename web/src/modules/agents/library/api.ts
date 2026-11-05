import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { toast } from "sonner"

import { ApiError, api } from "@/lib/api"
import type { EnvironmentDoc, EnvironmentInput, LibrarySkill, SkillDetail, SkillFiles, SkillVersion, SyncedSkill } from "../types"

const fail = (e: Error) => toast.error(e.message)

export const useLibrarySkills = (enabled = true) =>
  useQuery({ queryKey: ["agents", "library", "skills"], queryFn: () => api.get<LibrarySkill[]>("/api/agents/skills"), enabled, retry: false })

export const useSkillVersions = (id: string) =>
  useQuery({ queryKey: ["agents", "library", "skills", id, "versions"], queryFn: () => api.get<SkillVersion[]>(`/api/agents/skills/${id}/versions`), retry: false })

export const useSkill = (id: string) =>
  useQuery({ queryKey: ["agents", "library", "skills", id], queryFn: () => api.get<SkillDetail>(`/api/agents/skills/${id}`), retry: false })

/** The files of one version of a library skill, the latest when `version` is null. */
/** Only a custom skill's files can be read; Anthropic's own skills have none to show. */
export const useLibraryFiles = (id: string, version: string | null, enabled: boolean) =>
  useQuery({
    queryKey: ["agents", "library", "skills", id, "files", version],
    queryFn: () => api.get<SkillFiles>(`/api/agents/skills/${id}/files${version ? `?version=${encodeURIComponent(version)}` : ""}`),
    retry: false,
    enabled,
  })

type Ref = { id: string; name: string; version: string }

export function useSkillWrites() {
  const qc = useQueryClient()
  const done = () => qc.invalidateQueries({ queryKey: ["agents", "library", "skills"] })
  return {
    sync: useMutation({
      mutationFn: (b: { url: string; branch: string; path: string }) => api.post<SyncedSkill[]>("/api/agents/skills/sync", b),
      onSuccess: done,
      onError: fail,
    }),
    remove: useMutation({ mutationFn: (id: string) => api.del(`/api/agents/skills/${id}`), onSuccess: done, onError: fail }),
    create: useMutation({ mutationFn: (b: { name: string; description: string }) => api.post<Ref>("/api/agents/skills", b), onSuccess: done, onError: fail }),
    save: useMutation({
      mutationFn: (b: { id: string; files: Record<string, string | null>; expected_version: string }) =>
        api.put<Ref>(`/api/agents/skills/${b.id}/files`, { files: b.files, expected_version: b.expected_version }),
      onSuccess: done,
      // A 409 is the page's to explain (Reload or Overwrite); the refetch gives it the new version.
      onError: (e) => (e instanceof ApiError && e.status === 409 ? done() : fail(e)),
    }),
    restore: useMutation({ mutationFn: (b: { id: string; version: string }) => api.post<Ref>(`/api/agents/skills/${b.id}/restore`, { version: b.version }), onSuccess: done, onError: fail }),
  }
}

export const useEnvironments = (enabled = true) =>
  useQuery({ queryKey: ["agents", "library", "environments"], queryFn: () => api.get<EnvironmentDoc[]>("/api/agents/environments"), enabled, retry: false })

/** A 428 means the change opens the network of a sealed channel's sandbox; `ask` says whether to go ahead. */
export type Ask = (message: string) => Promise<boolean>

export function useEnvironmentWrites(ask: Ask) {
  const qc = useQueryClient()
  const done = () => qc.invalidateQueries({ queryKey: ["agents", "library", "environments"] })
  const guarded = async <T,>(send: (confirm: boolean) => Promise<T>): Promise<T> => {
    try {
      return await send(false)
    } catch (e) {
      if (e instanceof ApiError && e.status === 428 && (await ask(e.message))) return send(true)
      throw e
    }
  }
  return {
    create: useMutation({ mutationFn: (spec: EnvironmentInput) => api.post<EnvironmentDoc>("/api/agents/environments", spec), onSuccess: done, onError: fail }),
    save: useMutation({
      mutationFn: (b: { id: string; spec: EnvironmentInput; expected_updated_at: string }) =>
        guarded((confirm) => api.put<EnvironmentDoc>(`/api/agents/environments/${b.id}`, { spec: b.spec, confirm_open_network: confirm, expected_updated_at: b.expected_updated_at })),
      onSuccess: done,
      onError: (e) => {
        if (e instanceof ApiError && e.status === 409) {
          toast.error("Changed meanwhile. Reloaded.")
          void done()
        } else if (!(e instanceof ApiError && e.status === 428)) fail(e)
      },
    }),
    archive: useMutation({
      mutationFn: (id: string) =>
        guarded((confirm) => api.del<{ note: string }>(`/api/agents/environments/${id}${confirm ? "?confirm_open_network=true" : ""}`)),
      onSuccess: done,
      onError: (e) => { if (!(e instanceof ApiError && e.status === 428)) fail(e) },
    }),
  }
}
