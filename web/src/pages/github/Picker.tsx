import { BookMarked, Lock, Search, TriangleAlert } from "lucide-react"
import { useState } from "react"

import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { plural } from "@/lib/format"
import { cn } from "@/lib/utils"
import { Pill } from "@/modules/agents/bits"
import { Card } from "../Card"
import { connectApi } from "./api"
import { Actions, ChoiceRow, Err, OwnerAvatar, WideHead } from "./parts"
import type { ConnectInstallation, ConnectRepo, Step, WorkspacePickerView } from "./types"

/** Rows drawn at once; a search narrows a bigger owner. */
const SHOWN = 200

const WARNING = (i: ConnectInstallation) =>
  i.state === "suspended" ? "Paused on GitHub" : i.fetch_state === "error" ? "GitHub list unavailable" : i.truncated ? "Limited to 2,000 repos" : null

/** Repos grouped by GitHub owner, with a filter, bulk select and a selected-only view. `fixed` repos show ticked and cannot change. */
export function RepoList({ groups: given, first, checked, fixed, note, onChange, onRetry, className }: {
  groups: { item: ConnectInstallation; repos: ConnectRepo[] }[]
  /** The requested repo: listed at the top, in the top group. */
  first?: number | null
  checked: Set<number>; fixed?: Set<number>
  note: (id: number) => string | undefined
  onChange: (next: Set<number>) => void
  onRetry?: () => void
  className?: string
}) {
  const groups = first == null ? given : given
    .map((g) => ({ ...g, repos: [...g.repos].sort((a, b) => Number(b.id === first) - Number(a.id === first)) }))
    .sort((a, b) => Number(b.repos.some((r) => r.id === first)) - Number(a.repos.some((r) => r.id === first)))
  const [query, setQuery] = useState("")
  const [pressed, setOnly] = useState(false)
  const q = query.trim().toLowerCase()
  const picked = [...checked].filter((id) => !fixed?.has(id)).length
  const only = pressed && picked > 0
  const match = (r: ConnectRepo) => (!only || checked.has(r.id)) && r.full_name.toLowerCase().includes(q)
  const matches = groups.map((g) => ({ ...g, repos: g.repos.filter(match) }))
  const selectable = matches.flatMap((g) => g.repos).filter((r) => !fixed?.has(r.id))
  const total = matches.reduce((n, g) => n + g.repos.length, 0)
  const allOn = selectable.length > 0 && selectable.every((r) => checked.has(r.id))
  const set = (ids: number[], on: boolean) => onChange(new Set(on ? [...checked, ...ids] : [...checked].filter((id) => !ids.includes(id))))
  const drawn = matches.map((g, i) => g.repos.slice(0, Math.max(SHOWN - matches.slice(0, i).reduce((n, x) => n + x.repos.length, 0), 0)))
  return (
    <div className={cn("flex min-h-0 flex-col gap-3", className)}>
      <div className="relative">
        <Search className="pointer-events-none absolute top-2 left-2.5 size-4 text-muted-foreground" />
        <Input value={query} onChange={(e) => setQuery(e.target.value)} placeholder="Search repos" aria-label="Search repos" className="pl-8" />
      </div>
      <div className="flex min-h-6 flex-wrap items-center gap-x-3 gap-y-1 text-xs text-muted-foreground">
        <span aria-live="polite">{total > SHOWN ? `Showing ${SHOWN} of ${plural(total, "repo")}` : plural(total, "repo")}</span>
        <span className="ml-auto flex gap-1">
          <Button variant="ghost" size="xs" disabled={picked === 0} aria-pressed={only} className={only ? "bg-accent" : ""} onClick={() => setOnly(!only)}>Selected ({picked})</Button>
          <Button variant="ghost" size="xs" disabled={picked === 0} onClick={() => onChange(new Set(fixed))}>Clear</Button>
          <Button variant="ghost" size="xs" disabled={selectable.length === 0 || allOn} onClick={() => set(selectable.map((r) => r.id), true)}>Select all {selectable.length}</Button>
        </span>
      </div>
      <div className="max-h-[50vh] min-h-0 space-y-4 overflow-y-auto pr-1">
        {matches.map(({ item }, i) => {
          const warning = WARNING(item)
          const rows = drawn[i]!
          if (!rows.length && !(warning && !q && !only)) return null
          return (
            <section key={item.id}>
              <h3 className="mb-1.5 flex items-center gap-2 text-sm font-medium">
                <OwnerAvatar item={item} />{item.owner_login}
                {warning && <span className="flex items-center gap-1 text-xs font-normal text-warning"><TriangleAlert className="size-3" />{warning}</span>}
                {item.fetch_state === "error" && onRetry && <Button variant="ghost" size="xs" className="ml-auto" onClick={onRetry}>Try again</Button>}
              </h3>
              <div className="space-y-1.5">
                {rows.map((r) => {
                  const on = checked.has(r.id), n = fixed?.has(r.id) ? "Already connected" : note(r.id)
                  return (
                    <ChoiceRow key={r.id} checked={on} disabled={fixed?.has(r.id)} onChange={() => set([r.id], !on)}>
                      {r.private ? <Lock className="size-4 shrink-0 text-muted-foreground" /> : <BookMarked className="size-4 shrink-0 text-muted-foreground" />}
                      <span className="truncate font-medium">{r.full_name.split("/").slice(1).join("/")}</span>
                      {r.archived && <Pill>Archived</Pill>}
                      {n && <Pill>{n}</Pill>}
                    </ChoiceRow>
                  )
                })}
              </div>
            </section>
          )
        })}
        {total === 0 && <p className="py-6 text-center text-sm text-muted-foreground">{q || only ? "No matching repos" : "No repos to add"}</p>}
      </div>
    </div>
  )
}

/** A workspace's (or a first agent's) repo picker: tick repos, then connect them. */
export function Picker({ view, step }: { view: WorkspacePickerView; step: Step }) {
  const added = new Set(view.already_added_repo_ids)
  const [checked, setChecked] = useState(() => new Set([...added, ...Object.keys(view.needs_write).map(Number), ...(view.requested_repo_id == null ? [] : [view.requested_repo_id])]))
  const groups = view.installations.map((item) => ({ item, repos: item.repos.filter((r) => r.permission !== null) }))
  const ids = [...checked].filter((id) => !added.has(id))
  const submit = () => step.run(() => connectApi.save({
    state: view.state, invitation: view.invitation_hash, receipt: view.receipt, repo: ids.map(String), access: ["write"],
  }))
  return (
    <Card wide>
      <WideHead title={view.agent_name ? `Connect repos to ${view.agent_name}` : "Connect GitHub repos"} platform={view.platform} workspace={view.workspace_label} />
      <RepoList
        className="mt-6" groups={groups} first={view.requested_repo_id} checked={checked} fixed={added} onChange={setChecked}
        note={(id) => (id in view.needs_write ? (view.needs_write[id] ? "Needs write" : "Needed") : undefined)}
        onRetry={() => step.run(() => connectApi.view(view.state))}
      />
      {view.install_url && (
        <p className="mt-4 flex items-center gap-2 text-sm text-muted-foreground">Missing a repo?<a href={view.install_url} className="text-link hover:underline">Open GitHub setup</a></p>
      )}
      <Actions>
        <Button variant="outline" size="lg" disabled={step.pending} onClick={() => step.run(() => connectApi.cancel(view.state))}>Cancel</Button>
        <Button size="lg" disabled={ids.length === 0 || step.pending} onClick={submit}>{step.pending ? "Connecting" : "Connect repos"}</Button>
      </Actions>
      <Err>{step.error ?? view.selection_error}</Err>
    </Card>
  )
}
