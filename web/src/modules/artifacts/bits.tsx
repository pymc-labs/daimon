import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { Building2, Globe, Users } from "lucide-react"

import { Tag } from "@/components/panel"
import { api } from "@/lib/api"
import { cn } from "@/lib/utils"
import type { ArtifactAccess, ArtifactChange, ArtifactDetail, ArtifactSummary } from "./types"

export const KEY = ["artifacts"] as const
export const useArtifact = (id: string) => useQuery({ queryKey: [...KEY, "detail", id], queryFn: () => api.get<ArtifactDetail>(`/api/artifacts/${id}`), enabled: !!id })

/** Save a change to one artifact's settings and refresh every view of it. */
export function useChange(id: string, onDone?: (a: ArtifactDetail) => void) {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (b: ArtifactChange) => api.patch<ArtifactDetail>(`/api/artifacts/${id}`, b),
    onSuccess: (a) => { void qc.invalidateQueries({ queryKey: KEY }); onDone?.(a) },
  })
}

export const ACCESS = {
  workspace: { icon: Building2, label: "Workspace" },
  people: { icon: Users, label: "Specific people" },
  public: { icon: Globe, label: "Anyone with the link" },
} as const

/** Who can open a page, in the list's words. */
export function AccessLabel({ access, count, className }: { access: ArtifactAccess; count: number; className?: string }) {
  const { icon: Icon, label } = ACCESS[access]
  return (
    <span className={cn("inline-flex items-center gap-1.5 whitespace-nowrap", className)}>
      <Icon className="size-3.5 shrink-0 text-muted-foreground" />
      {access === "people" ? `${count} ${count === 1 ? "person" : "people"}` : label}
    </span>
  )
}

/** What the page may use: small tags. */
export function UseTags({ a }: { a: Pick<ArtifactSummary, "data" | "events" | "ask"> }) {
  const tags = [a.data !== "off" && (a.data === "write" ? "Data write" : "Data"), a.events && "Events", a.ask && "Ask"].filter(Boolean) as string[]
  return tags.length ? <span className="flex flex-wrap gap-1">{tags.map((t) => <Tag key={t}>{t}</Tag>)}</span> : <span className="text-faint">None</span>
}

/** "21 days left", "Expired". */
export function expiry(iso: string | null, now = Date.now()) {
  if (!iso) return null
  const d = Math.ceil((new Date(iso).getTime() - now) / 86_400_000)
  return d <= 0 ? "Expired" : d === 1 ? "1 day left" : `${d} days left`
}

export const fullUrl = (url: string) => `${location.origin}${url}`
