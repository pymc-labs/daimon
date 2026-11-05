import { Star } from "lucide-react"

import { BrandIcon } from "@/components/brand"
import { Ago, Mono, StatusDot } from "@/components/common"
import { PanelMenu } from "@/components/panel"
import { Button } from "@/components/ui/button"
import { useConfirm } from "@/lib/confirm"
import { cn } from "@/lib/utils"
import { Pill } from "../bits"
import { Section, type Props } from "../inspector/parts"
import type { Agent, SyncedSkill } from "../types"
import { hasTrouble, repoTrouble, useAgentGithub, useGithubWrites, type GithubRepo } from "./github"
import { Out } from "./Out"

const SYNC = { pending: "Waiting", running: "Syncing", done: "Synced" } as const
const MARK: Record<SyncedSkill["action"], string> = { created: "Added", updated: "Updated", skipped: "Skipped", failed: "Failed" }

/** The canvas card's body: the working repo, then the sync. */
export function GithubFacts({ agent }: { agent: Agent }) {
  const q = useAgentGithub(agent.id, agent.can_view_details)
  const g = q.data
  if (!g) return <div className="p-3 text-xs text-faint">{q.isError ? "Unavailable" : ""}</div>
  const repo = g.repos.find((r) => r.working)?.name
  return (
    <div className="space-y-1 p-3 text-xs">
      <div className="flex items-center gap-1.5">
        {repo ? <Mono className="truncate text-foreground/80">{repo}</Mono> : <span className="text-faint">Not connected</span>}
        {hasTrouble(g) && <span className="ml-auto" role="img" aria-label="Needs a look"><StatusDot status="failing" /></span>}
      </div>
      {g.sync && <div className="text-faint">{g.sync.error ? "Sync failed" : g.sync.state === "done" ? <>Synced <Ago iso={g.sync.at} className="text-faint" /></> : SYNC[g.sync.state]}</div>}
    </div>
  )
}

function RepoRow({ repo, skills, manage, busy, onWork, onRemove, onSkills }: { repo: GithubRepo; skills: number; manage: boolean; busy: boolean; onWork: () => void; onRemove: () => void; onSkills: () => void }) {
  const trouble = repoTrouble(repo)
  return (
    <div className="px-3 py-2 text-sm">
      <div className="flex min-h-6 items-center gap-1.5">
        {repo.working && <Star className="size-3.5 shrink-0 fill-current text-muted-foreground" aria-label="Working repo" />}
        <Out href={repo.url} className="truncate font-medium">{repo.name}</Out>
        <Pill>{repo.access === "write" ? "Can change" : "Read only"}</Pill>
        {repo.shared && <Pill>Shared</Pill>}
        <span className="-mr-1.5 ml-auto flex items-center gap-1">
          {skills > 0 && <button type="button" className="rounded px-1.5 text-xs text-link hover:underline" onClick={onSkills}>{skills === 1 ? "1 skill" : `${skills} skills`}</button>}
          {manage && (
            <PanelMenu label={`Actions for ${repo.name}`} items={[
              ...(repo.working ? [] : [{ label: "Set as working repo", onSelect: onWork, disabled: busy }]),
              { label: "Remove", onSelect: onRemove, destructive: true, disabled: busy },
            ]} />
          )}
        </span>
      </div>
      {(trouble.length > 0 || repo.granted_by || repo.granted_at) && (
        <div className="mt-0.5 flex flex-wrap items-center gap-x-3 text-xs text-muted-foreground">
          {trouble.length > 0 && <span className="text-destructive"><StatusDot status="failing" label={trouble.join(", ")} /></span>}
          <span>{repo.granted_by ? `Added by ${repo.granted_by}` : "Added"}{repo.granted_at && <>, <Ago iso={repo.granted_at} /></>}</span>
        </div>
      )}
    </div>
  )
}

/** The agent's GitHub link: repos, how many skills each holds, and the last sync. */
export function GithubPanel({ agent, select }: Props) {
  const confirm = useConfirm()
  const q = useAgentGithub(agent.id, agent.can_view_details)
  const w = useGithubWrites(agent.id)
  const g = q.data

  if (!g) return <Section title="GitHub"><div className="text-sm text-muted-foreground">{q.isError ? q.error.message : "Loading"}</div></Section>
  const manage = g.configured && g.can_manage
  const connect = g.configured && g.can_connect
  const busy = w.setWorking.isPending || w.remove.isPending
  const remove = async (name: string) => {
    if (await confirm({ title: `Remove ${name}?`, action: "Remove", destructive: true })) w.remove.mutate(name)
  }
  const showSync = !!g.sync || (manage && g.repos.length > 0)

  return (
    <>
      <Section
        title="Repos"
        action={connect
          ? <Button variant="outline" size="xs" disabled={w.connect.isPending} onClick={() => w.connect.mutate()}><BrandIcon brand="github" className="size-3" />Connect</Button>
          : g.configured && !manage ? <Pill>View only</Pill> : undefined}
      >
        {g.repos.length ? (
          <div className="divide-y rounded-lg border">
            {g.repos.map((r) => (
              <RepoRow key={r.name} repo={r} skills={g.skills.filter((s) => s.origin === "repo" && s.repo?.toLowerCase() === r.name.toLowerCase()).length} manage={manage} busy={busy} onWork={() => w.setWorking.mutate(r.name)} onRemove={() => void remove(r.name)} onSkills={() => select("skills")} />
            ))}
          </div>
        ) : <div className="text-sm text-muted-foreground">{g.configured ? "None" : "GitHub isn't set up on this server"}</div>}
      </Section>
      {showSync && (
        <Section
          title="Sync"
          action={manage && <Button variant="outline" size="xs" disabled={w.resync.isPending || g.sync?.state === "running"} onClick={() => w.resync.mutate()}>Resync now</Button>}
        >
          <div className="space-y-2 text-sm">
            {g.sync ? (
              <div className="flex items-center gap-2">
                <StatusDot status={g.sync.error ? "failing" : g.sync.state === "done" ? "healthy" : "setup"} label={g.sync.error ? "Failed" : SYNC[g.sync.state]} />
                <Ago iso={g.sync.at} className="ml-auto text-xs text-muted-foreground" />
              </div>
            ) : <div className="text-muted-foreground">Not yet</div>}
            {g.sync?.error && <div className="break-words text-destructive">{g.sync.error}</div>}
            {w.resync.data && (
              <ul className="space-y-1">
                {w.resync.data.results.map((r) => (
                  <li key={r.name} className="flex items-baseline gap-2">
                    <span className="font-medium">{r.name}</span>
                    <span className={cn("text-xs text-muted-foreground", r.action === "failed" && "text-destructive")}>{MARK[r.action]}{r.detail ? `, ${r.detail}` : ""}</span>
                  </li>
                ))}
                {!w.resync.data.results.length && <li className="text-muted-foreground">No skills found</li>}
              </ul>
            )}
          </div>
        </Section>
      )}
    </>
  )
}
