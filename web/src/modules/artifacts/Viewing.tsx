import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { AppWindow } from "lucide-react"
import { useState } from "react"

import { Button } from "@/components/ui/button"
import { api } from "@/lib/api"
import { cn } from "@/lib/utils"
import { KEY } from "./bits"

/** A version's thumbnail, or the page icon until a manager's preview captures one. */
export function Thumb({ id, version, className }: { id: string; version: number; className?: string }) {
  const src = `/api/artifacts/${id}/versions/${version}/thumbnail`
  const [failed, setFailed] = useState<string | null>(null)
  return (
    <span className={cn("grid aspect-[16/10] shrink-0 place-items-center overflow-hidden rounded-md border bg-muted text-muted-foreground", className)}>
      {failed === src ? <AppWindow className="size-4" /> : <img src={src} alt="" loading="lazy" className="size-full object-cover object-top" onError={() => setFailed(src)} />}
    </span>
  )
}

/** Whether this person allowed the page to ask its agent as them, and a way to take it back. */
export function useAskConsent(id: string, enabled = true) {
  const qc = useQueryClient()
  const key = [...KEY, "consent", id]
  const state = useQuery({ queryKey: key, queryFn: () => api.get<{ allowed: boolean }>(`/api/artifacts/${id}/consent`), enabled: !!id && enabled })
  const revoke = useMutation({ mutationFn: () => api.del(`/api/artifacts/${id}/consent`), onSuccess: () => qc.setQueryData(key, { allowed: false }) })
  return { allowed: !!state.data?.allowed, revoke }
}

/** The ask thread's header: the page may ask as this person until they revoke it. */
export function RevokeAsks({ id }: { id: string }) {
  const { allowed, revoke } = useAskConsent(id)
  if (!allowed) return null
  return <Button variant="ghost" size="sm" className="text-xs text-muted-foreground" disabled={revoke.isPending} onClick={() => revoke.mutate()}>Revoke asks</Button>
}
