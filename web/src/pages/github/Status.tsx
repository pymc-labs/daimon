import { Check, CircleCheck, Clock, Copy, Hourglass, TriangleAlert } from "lucide-react"
import { useState } from "react"

import { BrandIcon } from "@/components/brand"
import { Pill } from "@/modules/agents/bits"
import { Button } from "@/components/ui/button"
import { plural } from "@/lib/format"
import { Card } from "../Card"
import { connectApi, stateOf } from "./api"
import { Actions, BackButton, Err, GithubMark, Mark, OwnerAvatar, Title } from "./parts"
import type { AgentRepo, AlreadyConnectedView, CancelledView, ConnectInstallation, DoneView, ErrorView, ExpiredView, InstallView, NoReposView, Outcome, PendingView, SavedAgentView, Step, TransientErrorView } from "./types"

const repos = (n: number, agent: string | null) => `${plural(n, "repo")} connected${agent ? ` to ${agent} only` : ""}`

/** Run a step from a server-built link when it carries the flow's state, else just follow the link. */
const inPlace = (step: Step, url: string, run: (state: string) => Promise<Outcome>) => {
  const state = stateOf(url)
  if (state) step.run(() => run(state)); else location.assign(url)
}

export function ErrorPage({ view }: { view: ErrorView }) {
  const action = view.action ?? (view.retry_url ? null : "Ask the requester for a new setup link.")
  return (
    <Card mark={<Mark icon={TriangleAlert} tone="warn" />}>
      <Title title={view.message} sub={action} />
      {view.retry_url && <Button asChild size="lg" className="mt-6 w-full"><a href={view.retry_url}>Try again</a></Button>}
    </Card>
  )
}

/** An upstream failure: re-post the ticked repos when the server kept them, else try the step again. */
export function TransientErrorPage({ view, step, onRetry }: { view: TransientErrorView; step: Step; onRetry: () => void }) {
  const again = view.retry_selection
    ? () => step.run(() => connectApi.save(view.retry_selection!))
    : onRetry
  return (
    <Card mark={<Mark icon={TriangleAlert} tone="warn" />}>
      <Title title={view.message} />
      {view.retry_selection || !view.retry_url
        ? <Button size="lg" className="mt-6 w-full" disabled={step.pending} onClick={again}>Try again</Button>
        : <Button asChild size="lg" className="mt-6 w-full"><a href={view.retry_url}>Try again</a></Button>}
      <Err>{step.error}</Err>
    </Card>
  )
}

export function ExpiredPage({ view }: { view: ExpiredView }) {
  return (
    <Card mark={<Mark icon={Clock} tone="warn" />}>
      <Title title="This link has expired" sub={`Ask ${view.requester_label} for a new link`} />
    </Card>
  )
}

export function CancelledPage({ view }: { view: CancelledView }) {
  return (
    <Card mark={<Mark icon={CircleCheck} />}>
      <Title title="No changes saved" sub="Your GitHub repos were not changed" />
      <BackButton back={view.back} />
    </Card>
  )
}

export function AlreadyConnectedPage({ view }: { view: AlreadyConnectedView }) {
  return (
    <Card mark={<Mark icon={CircleCheck} tone="ok" />}>
      <Title title="Already connected" sub={view.count != null && view.agent_name ? repos(view.count, view.agent_name) : "GitHub repos are connected"} />
      <BackButton back={view.back} />
    </Card>
  )
}

export function InstallPage({ view, step }: { view: InstallView; step: Step }) {
  return (
    <Card mark={<Mark icon={GithubMark} />}>
      <Title title="Connect GitHub" sub="Daimon needs the GitHub App to see your repos" />
      <Actions>
        <Button variant="outline" size="lg" disabled={step.pending} onClick={() => inPlace(step, view.cancel_url, connectApi.cancel)}>Cancel</Button>
        <Button asChild size="lg"><a href={view.install_url}><BrandIcon brand="github" mono />Continue to GitHub</a></Button>
      </Actions>
      <Err>{step.error}</Err>
    </Card>
  )
}

export function PendingPage({ view, step }: { view: PendingView; step: Step }) {
  const who = view.organization ? `${/^[aeiou]/i.test(view.organization) ? "An" : "A"} ${view.organization} owner` : "A GitHub owner"
  return (
    <Card mark={<Mark icon={Hourglass} />}>
      <Title title="Waiting for GitHub approval" sub={`${who} must approve Daimon`} />
      <Actions>
        <Button variant="outline" size="lg" disabled={step.pending} onClick={() => inPlace(step, view.cancel_url, connectApi.cancel)}>Cancel</Button>
        <Button size="lg" disabled={step.pending} onClick={() => inPlace(step, view.check_url, connectApi.view)}>Check again</Button>
      </Actions>
      <Err>{step.error}</Err>
    </Card>
  )
}

export function NoReposPage({ view }: { view: NoReposView }) {
  const [copied, setCopied] = useState(false)
  return (
    <Card mark={<Mark icon={TriangleAlert} tone="warn" />}>
      <Title title="No repos available" sub={view.share_link ? "Send the setup link to someone with write access" : "No repos are available with this account"} />
      <Actions>
        <Button asChild variant={view.share_link ? "outline" : "default"} size="lg"><a href={view.install_url}>Open GitHub setup</a></Button>
        {view.share_link && (
          <Button size="lg" onClick={() => { void navigator.clipboard?.writeText(view.share_link); setCopied(true) }}>
            {copied ? <Check /> : <Copy />}{copied ? "Link copied" : "Copy setup link"}
          </Button>
        )}
      </Actions>
    </Card>
  )
}

function Owners({ grants, visible, installations }: { grants: AgentRepo[]; visible: Set<number>; installations: ConnectInstallation[] }) {
  const owner = (r: AgentRepo) => r.full_name.split("/")[0]!
  const groups = new Map<string, AgentRepo[]>()
  for (const r of grants) groups.set(owner(r), [...(groups.get(owner(r)) ?? []), r])
  const order = [...groups.keys()].sort((a, b) => Number(!groups.get(a)!.some((r) => r.is_working_repo)) - Number(!groups.get(b)!.some((r) => r.is_working_repo)) || a.localeCompare(b, undefined, { sensitivity: "base" }))
  return (
    <div className="mt-6 space-y-4 text-left">
      {order.map((login) => (
        <section key={login}>
          <h2 className="mb-1.5 flex items-center gap-2 text-sm font-medium"><OwnerAvatar item={installations.find((i) => i.owner_login.toLowerCase() === login.toLowerCase())} />{login}</h2>
          <div className="divide-y rounded-lg border text-sm">
            {groups.get(login)!.map((r) => (
              <div key={r.repo_id} className="flex min-h-10 items-center gap-2 px-3 py-2">
                <span className="truncate font-medium">{visible.has(r.repo_id) ? r.full_name.split("/").slice(1).join("/") : "Hidden repo"}</span>
                {r.is_working_repo && <Pill>Working repo</Pill>}
              </div>
            ))}
          </div>
        </section>
      ))}
    </div>
  )
}

export function SavedAgentPage({ view }: { view: SavedAgentView }) {
  return (
    <Card mark={<Mark icon={CircleCheck} tone="ok" />}>
      <Title title="Repos saved" sub={repos(view.grants.length, view.agent_name)} />
      <Owners grants={view.grants} visible={new Set(view.visible_repo_ids)} installations={view.installations} />
      <BackButton back={view.back} />
    </Card>
  )
}

export function DonePage({ view }: { view: DoneView }) {
  const sub = view.agent_name ? null : view.same_person ? "Choose which agents use them in GitHub setup" : `${view.requester_label} can now choose which agents use them`
  return (
    <Card mark={<Mark icon={CircleCheck} tone="ok" />}>
      <Title title="Repos connected" sub={view.agent_name ? repos(view.count, view.agent_name) : <><span className="block">{plural(view.count, "repo")} connected</span><span className="block">{sub}</span></>} />
      <BackButton back={view.back} />
    </Card>
  )
}
