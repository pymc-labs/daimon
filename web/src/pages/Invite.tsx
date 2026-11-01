import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { Link, useNavigate, useParams } from "react-router"

import { AgentAvatar } from "@/components/common"
import { Button } from "@/components/ui/button"
import { api, ApiError } from "@/lib/api"
import { shortDate, usd } from "@/lib/format"
import { useMe } from "@/lib/hooks"
import type { AgentRef } from "@/lib/types"
import { landingOf } from "./landing"
import { Card } from "./Card"

export type Invite = {
  workspace: string; from: string; email: string; kind: "invite" | "owner" | "sign_in"
  agents: AgentRef[]; credit_usd: number; expires_at: string
}

const COPY = {
  invite: { title: (i: Invite) => `Join ${i.workspace}`, by: (i: Invite) => `${i.from} invited you`, action: "Accept and start" },
  owner: { title: (i: Invite) => `Become an owner of ${i.workspace}`, by: (i: Invite) => `${i.from} sent you this link`, action: "Accept" },
  sign_in: { title: (i: Invite) => `Sign in to ${i.workspace}`, by: (i: Invite) => `${i.from} sent you this link`, action: "Continue" },
} as const

const Row = ({ k, children }: { k: string; children: React.ReactNode }) => (
  <div className="flex justify-between px-4 py-2.5"><dt className="text-muted-foreground">{k}</dt><dd>{children}</dd></div>
)

export function InviteLanding() {
  const { token } = useParams()
  const navigate = useNavigate()
  const qc = useQueryClient()
  const me = useMe()
  const { data, error } = useQuery({ queryKey: ["invite", token], queryFn: () => api.get<Invite>(`/api/invites/${token}`), retry: false })
  const accept = useMutation({
    mutationFn: () => api.post(`/api/invites/${token}/accept`),
    onSuccess: () => { qc.clear(); navigate(landingOf(data!)) },
    meta: { inline: true },
  })
  if (error) return (
    <Card>
      <h1 className="mt-5 text-lg font-semibold">{error instanceof ApiError && error.status === 404 ? "Invite expired" : "Couldn't open this invite"}</h1>
      <p className="mt-1 text-sm text-muted-foreground">Ask the person who sent it for a new one.</p>
    </Card>
  )
  if (!data) return <div className="min-h-dvh bg-muted" role="status" aria-label="Loading" />
  const copy = COPY[data.kind] ?? COPY.invite
  const signInFirst = data.kind === "owner" && !me.data
  return (
    <Card mark={data.agents.length ? <div className="flex justify-center -space-x-2">{data.agents.map((a) => <AgentAvatar key={a.id} src={a.avatar} name={a.name} className="size-14 rounded-xl ring-4 ring-background" />)}</div> : undefined}>
      <h1 className="mt-5 text-lg font-semibold">{copy.title(data)}</h1>
      <p className="mt-1 text-sm text-muted-foreground">{copy.by(data)}</p>
      <dl className="mt-6 divide-y rounded-lg border text-left text-sm">
        {data.email && data.kind !== "owner" && <Row k="Email">{data.email}</Row>}
        {data.kind === "invite" && data.credit_usd > 0 && <Row k="Credit"><span className="tabular">{usd(data.credit_usd)}</span></Row>}
        <Row k="Expires">{shortDate(data.expires_at)}</Row>
      </dl>
      {accept.error && <p role="alert" className="mt-4 text-sm text-destructive">{accept.error.message}</p>}
      {signInFirst ? (
        <Button asChild size="lg" className="mt-6 w-full"><Link to={`/signin?next=${encodeURIComponent(`/invite/${token}`)}`}>Sign in to accept</Link></Button>
      ) : (
        <Button size="lg" className="mt-6 w-full" onClick={() => accept.mutate()} disabled={accept.isPending}>{copy.action}</Button>
      )}
    </Card>
  )
}
