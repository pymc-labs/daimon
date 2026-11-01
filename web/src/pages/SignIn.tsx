import { useMutation, useQueryClient } from "@tanstack/react-query"
import { useNavigate, useSearchParams } from "react-router"

import { BrandIcon } from "@/components/brand"
import { ErrorState, Rows } from "@/components/QueryState"
import { Button } from "@/components/ui/button"
import { api, ApiError } from "@/lib/api"
import { useProviders } from "@/lib/hooks"
import type { Me, Provider } from "@/lib/types"
import { safeNext } from "@/lib/safe-next"
import { Card } from "./Card"

/** After sign-in: pick a workspace when there is more than one. */
async function landing(next: string) {
  const me = await api.get<Me>("/api/me")
  return me.workspaces.length > 1 && next === "/chat" ? `/workspaces?next=${encodeURIComponent(next)}` : next
}

export function SignIn() {
  const [params] = useSearchParams()
  const next = safeNext(params.get("next"))
  const navigate = useNavigate()
  const qc = useQueryClient()
  const providers = useProviders()
  const dev = useMutation({
    mutationFn: async (provider: string) => { await api.post("/auth/mock", { provider }); qc.clear(); return landing(next) },
    onSuccess: (to) => navigate(to),
    meta: { inline: true },
  })
  const go = (p: Provider) => {
    if (p.kind === "dev") dev.mutate(p.id)
    else window.location.assign(`/auth/${p.id}/start?next=${encodeURIComponent(next)}`)
  }
  const error = params.get("error") ?? (dev.error instanceof ApiError ? dev.error.message : null)
  return (
    <Card>
      <h1 className="mt-5 text-lg font-semibold">Sign in to Daimon</h1>
      <div className="mt-6 space-y-2">
        {providers.isError ? <ErrorState error={providers.error} onRetry={() => void providers.refetch()} className="py-4" />
          : !providers.data ? <Rows n={2} />
          : !providers.data.length ? <p className="text-sm text-muted-foreground">No way to sign in is set up. Ask whoever runs Daimon.</p>
          : providers.data.map((p) => (
            <Button key={p.id} variant="outline" size="lg" className="w-full" disabled={dev.isPending} onClick={() => go(p)}>
              <BrandIcon brand={p.id} icon={p.icon} />Continue with {p.label}
            </Button>
          ))}
      </div>
      {error && <p role="alert" className="mt-4 text-sm text-destructive">{error}</p>}
    </Card>
  )
}
