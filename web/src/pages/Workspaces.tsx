import { useMutation, useQueryClient } from "@tanstack/react-query"
import { ChevronRight } from "lucide-react"
import { useNavigate, useSearchParams } from "react-router"

import { BrandIcon } from "@/components/brand"
import { QueryState } from "@/components/QueryState"
import { api } from "@/lib/api"
import { plural } from "@/lib/format"
import { useMe } from "@/lib/hooks"
import { safeNext } from "@/lib/safe-next"
import { Card } from "./Card"

export function Workspaces() {
  const me = useMe()
  const [params] = useSearchParams()
  const navigate = useNavigate()
  const qc = useQueryClient()
  const pick = useMutation({
    mutationFn: (id: string) => api.post("/api/workspaces/switch", { id }),
    onSuccess: () => { qc.clear(); navigate(safeNext(params.get("next"))) },
  })
  return (
    <Card>
      <h1 className="mt-5 text-lg font-semibold">Choose a workspace</h1>
      <div className="mt-6 overflow-hidden rounded-lg border text-left">
        <QueryState query={me} className="p-3">
          {(m) => m.workspaces.map((w) => (
            <button key={w.id} disabled={pick.isPending} onClick={() => pick.mutate(w.id)} className="flex w-full items-center gap-3 border-b px-4 py-3 last:border-0 hover:bg-accent">
              <BrandIcon brand={w.platform} className="size-5" />
              <span className="min-w-0 flex-1">
                <span className="block truncate text-sm font-medium">{w.name}</span>
                <span className="block text-xs text-muted-foreground">{plural(w.members, "member")}</span>
              </span>
              <ChevronRight className="size-4 text-muted-foreground" />
            </button>
          ))}
        </QueryState>
      </div>
    </Card>
  )
}
