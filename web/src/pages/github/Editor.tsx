import { BookMarked, Hourglass, Lock, Plus, Star, Trash2, TriangleAlert, Undo2 } from "lucide-react"
import { useState } from "react"

import { Button } from "@/components/ui/button"
import { Dialog, DialogContent, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog"
import { plural } from "@/lib/format"
import { cn } from "@/lib/utils"
import { Pill } from "@/modules/agents/bits"
import { Card } from "../Card"
import { connectApi } from "./api"
import { Actions, ChoiceRow, Err, OwnerAvatar, WideHead } from "./parts"
import { RepoList } from "./Picker"
import type { AgentEditorView, AgentRepo, Step } from "./types"

type Row = {
  id: number; name: string; owner: string; installation: number | null
  grant?: AgentRepo; visible: boolean; pastCap: boolean
}
type Ids = Set<number>
const flip = (set: Ids, id: number, on: boolean) => new Set(on ? [...set, id] : [...set].filter((x) => x !== id))
const ownerOf = (name: string) => name.split("/")[0]!
const repoName = (name: string) => name.split("/").slice(1).join("/")

/** One agent's repos: add, finish setup, remove and choose the working repo, then save in one go. */
export function Editor({ view, step }: { view: AgentEditorView; step: Step }) {
  const visible = new Map(view.visible_repos.map((r) => [r.id, r]))
  const available = new Map(view.available.map((r) => [r.id, r]))
  const repairable = new Set(view.repairable_repo_ids), pastCap = new Set(view.past_cap_repo_ids)
  const [adds, setAdds] = useState<Ids>(new Set())
  const [fixes, setFixes] = useState<Ids>(new Set())
  const [removed, setRemoved] = useState<Ids>(new Set())
  const [working, setWorking] = useState<number | null>(view.working_repo_id)
  const [explicit, setExplicit] = useState(false)
  // The ticks of the open "Add repos" dialog; the requested repo starts ticked and opens it.
  const [draft, setDraft] = useState<Ids | null>(() => (view.requested_repo_id == null ? null : new Set([view.requested_repo_id])))
  const [choosing, setChoosing] = useState(false)
  const [follow, setFollow] = useState<number | null>(null)

  const rows: Row[] = [
    ...view.grants.map((g): Row => ({ id: g.repo_id, name: g.full_name, owner: ownerOf(g.full_name), installation: g.installation_id, grant: g, visible: visible.has(g.repo_id), pastCap: pastCap.has(g.repo_id) })),
    ...[...adds].flatMap((id) => available.get(id) ?? []).map((r): Row => ({ id: r.id, name: r.full_name, owner: ownerOf(r.full_name), installation: r.installation_id, visible: true, pastCap: false })),
  ]
  const kept = rows.filter((r) => !removed.has(r.id))
  const eligible = kept.filter((r) => r.visible && (!r.grant || r.grant.status === "active"))
  const owners = [...new Set([...view.owner_order, ...rows.map((r) => r.owner)])].filter((o) => rows.some((r) => r.owner.toLowerCase() === o.toLowerCase()))
  const accounts = new Set(kept.map((r) => (r.installation ? `i:${r.installation}` : `o:${r.owner.toLowerCase()}`)))
  const over = accounts.size - view.owner_limit
  // An offer shows once the agent would hold every repo of that one org, as on the MCP page.
  const keptIds = new Set(kept.map((r) => r.id))
  const offers = view.follow_offers.filter((o) =>
    kept.length > 0 && kept.every((r) => r.owner.toLowerCase() === o.owner.toLowerCase()) && o.repo_ids.every((id) => keptIds.has(id)))
  if (follow != null && !offers.some((o) => o.installation_id === follow)) setFollow(null)
  const workChanged = working !== view.working_repo_id
  const changed = adds.size + fixes.size + removed.size > 0 || workChanged || follow != null
  const current = rows.find((r) => r.id === working)

  const drop = (id: number) => { if (working === id) { setWorking(null); if (id !== view.working_repo_id) setExplicit(true) } }
  const remove = (r: Row) => {
    setFixes(flip(fixes, r.id, false)); drop(r.id)
    if (r.grant) setRemoved(flip(removed, r.id, true)); else setAdds(flip(adds, r.id, false))
  }
  const undo = (id: number) => { setRemoved(flip(removed, id, false)); if (!explicit && id === view.working_repo_id) setWorking(id) }
  const discard = () => { setAdds(new Set()); setFixes(new Set()); setRemoved(new Set()); setWorking(view.working_repo_id); setExplicit(false); setFollow(null) }
  const confirmAdds = (next: Ids) => {
    setAdds(next)
    if (working != null && !view.grants.some((g) => g.repo_id === working) && !next.has(working)) { setWorking(null); setExplicit(true) }
    setDraft(null)
  }
  const keepWorking = !explicit && !(workChanged && !(view.working_repo_id != null && removed.has(view.working_repo_id)))
  const save = () => step.run(() => connectApi.save({
    state: view.state, invitation: view.invitation_hash, receipt: view.receipt,
    snapshot: view.snapshot, snapshot_sig: view.snapshot_sig, visibility: view.visibility, visibility_sig: view.visibility_sig,
    repo: [...adds, ...fixes].map(String), remove: [...removed].map(String), access: ["write"],
    working: [keepWorking ? "keep" : working == null ? "clear" : String(working)],
    repo_access: Object.fromEntries([...fixes].map((id) => [String(id), ["write"]])),
    follow_org: follow == null ? [] : [String(follow)],
  }))

  const groups = view.installations.map((item) => ({
    item, repos: view.available.filter((r) => r.installation_id === item.id),
  }))
  const pending = view.pending_installation?.found ? view.pending_installation.account_login : undefined
  const none = rows.length === 0

  return (
    <Card wide>
      <WideHead title={`${view.agent_name}'s GitHub repos`} platform={view.platform} workspace={view.workspace_label} />
      {view.pending_installation?.found && (
        <div className="mt-6 flex items-center gap-2 rounded-lg border bg-muted px-3 py-2 text-sm">
          <Hourglass className="size-4 shrink-0 text-muted-foreground" />
          <span className="min-w-0 flex-1">{pending ? `${/^[aeiou]/i.test(pending) ? "An" : "A"} ${pending} owner` : "A GitHub owner"} must approve Daimon</span>
          <Button variant="outline" size="xs" disabled={step.pending} onClick={() => step.run(() => connectApi.view(view.state))}>Check again</Button>
        </div>
      )}
      {!none && (
        <div className="mt-6 flex min-h-12 items-center gap-2 rounded-lg border px-3 py-2 text-sm">
          <Star className="size-4 shrink-0 fill-current text-muted-foreground" />
          <span className="text-muted-foreground">Working repo</span>
          <span className="min-w-0 flex-1 truncate font-medium">
            {current ? (current.visible ? current.name : "Hidden repo") : <span className="font-normal text-faint">None chosen</span>}
          </span>
          <Button variant="outline" size="xs" disabled={eligible.length === 0} onClick={() => setChoosing(true)}>{current ? "Change" : "Choose"}</Button>
        </div>
      )}
      <div className="mt-6 flex items-center gap-2">
        <h2 className="flex-1 text-xs font-medium text-muted-foreground">Repos</h2>
        <Button variant="outline" size="xs" onClick={() => setDraft(new Set(adds))}><Plus />Add repos</Button>
      </div>
      {none && <p className="mt-2 text-sm text-muted-foreground">No repos connected yet</p>}
      <div className="mt-2 space-y-4">
        {owners.map((login) => {
          const item = view.installations.find((i) => i.owner_login.toLowerCase() === login.toLowerCase())
          const own = rows.filter((r) => r.owner.toLowerCase() === login.toLowerCase())
          return (
            <section key={login}>
              <h3 className="mb-1.5 flex items-center gap-2 text-sm font-medium">
                <OwnerAvatar item={item} />{login}
                {item?.state === "suspended" && <span className="flex items-center gap-1 text-xs font-normal text-warning"><TriangleAlert className="size-3" />Paused on GitHub</span>}
                {item?.fetch_state === "error" && <span className="flex items-center gap-1 text-xs font-normal text-warning"><TriangleAlert className="size-3" />GitHub list unavailable</span>}
              </h3>
              <div className="divide-y rounded-lg border">
                {own.map((r) => (
                  <RepoRow key={r.id} row={r} working={working === r.id} removed={removed.has(r.id)} added={adds.has(r.id)} fixing={fixes.has(r.id)}
                    repairable={repairable.has(r.id)} needsWrite={!!view.needs_write[r.id]} onRemove={() => remove(r)} onUndo={() => undo(r.id)}
                    onFix={(on) => setFixes(flip(fixes, r.id, on))} />
                ))}
              </div>
            </section>
          )
        })}
      </div>
      {offers.length > 0 && (
        <div className="mt-4 space-y-1.5">
          {offers.map((o) => (
            <ChoiceRow key={o.installation_id} checked={follow === o.installation_id} onChange={() => setFollow(follow === o.installation_id ? null : o.installation_id)}>
              <span className="font-medium">Also give {view.agent_name} new repos added to {o.owner}</span>
              <span className="w-full text-xs text-muted-foreground">It can read and change them.</span>
            </ChoiceRow>
          ))}
        </div>
      )}
      {view.following && <p className="mt-4 text-sm text-muted-foreground">{view.agent_name} gets new repos added to its org on its own.</p>}

      <div className="mt-6 flex min-h-6 flex-wrap items-center gap-x-2 gap-y-1">
        {changed ? (
          <>
            {adds.size > 0 && <Pill>+{adds.size} to add</Pill>}
            {fixes.size > 0 && <Pill>{fixes.size} to update</Pill>}
            {removed.size > 0 && <Pill className="text-destructive">-{removed.size} to remove</Pill>}
            {workChanged && <Pill><Star className="mr-1 size-3 fill-current" />{current ? repoName(current.name) : "No working repo"}</Pill>}
            <button type="button" className="ml-auto text-xs text-link hover:underline" onClick={discard}>Discard changes</button>
          </>
        ) : null}
      </div>
      <Actions className="mt-3">
        <Button variant="outline" size="lg" disabled={step.pending} onClick={() => step.run(() => connectApi.cancel(view.state))}>Cancel</Button>
        <Button size="lg" disabled={!changed || over > 0 || step.pending} onClick={save}>{step.pending ? "Saving" : "Save changes"}</Button>
      </Actions>
      {changed && over > 0 && <Err>{view.agent_name} can use repos from up to {view.owner_limit} GitHub accounts. These changes use {accounts.size}.</Err>}
      <Err>{step.error ?? view.selection_error}</Err>

      <Dialog open={draft !== null} onOpenChange={(open) => { if (!open) setDraft(null) }}>
        <DialogContent className="flex max-h-[85dvh] flex-col gap-4 sm:max-w-lg" aria-describedby={undefined}>
          <DialogHeader><DialogTitle>Add repos</DialogTitle></DialogHeader>
          {draft && (
            <RepoList
              className="flex-1" groups={groups} first={view.requested_repo_id} checked={draft} onChange={setDraft}
              note={(id) => (id in view.needs_write ? "Required" : undefined)}
              onRetry={() => step.run(() => connectApi.view(view.state))}
            />
          )}
          {view.install_url && <p className="text-sm text-muted-foreground">Only accounts and orgs where the Daimon GitHub App is installed are listed. Repo in another account or org? <a href={view.install_url} className="text-link hover:underline">Install the app there</a></p>}
          <DialogFooter>
            <Button disabled={!draft || (draft.size === 0 && adds.size === 0)} onClick={() => draft && confirmAdds(draft)}>{draft?.size ? `Add ${plural(draft.size, "repo")}` : "Done"}</Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      <Dialog open={choosing} onOpenChange={setChoosing}>
        <DialogContent className="max-h-[85dvh] overflow-y-auto sm:max-w-md" aria-describedby={undefined}>
          <DialogHeader><DialogTitle>Working repo</DialogTitle></DialogHeader>
          <div role="radiogroup" aria-label="Working repo" className="space-y-1.5">
            {[...eligible.map((r) => ({ id: r.id as number | null, name: r.name })), { id: null, name: "No working repo" }].map((o) => (
              <ChoiceRow key={o.id ?? "none"} radio checked={working === o.id} onChange={() => { setWorking(o.id); setExplicit(true); setChoosing(false) }}>
                {o.id === null ? <span className="text-muted-foreground">{o.name}</span> : <span className="truncate font-medium">{o.name}</span>}
              </ChoiceRow>
            ))}
          </div>
        </DialogContent>
      </Dialog>
    </Card>
  )
}

function RepoRow({ row, working, removed, added, fixing, repairable, needsWrite, onRemove, onUndo, onFix }: {
  row: Row; working: boolean; removed: boolean; added: boolean; fixing: boolean; repairable: boolean; needsWrite: boolean
  onRemove: () => void; onUndo: () => void; onFix: (on: boolean) => void
}) {
  const g = row.grant
  const facts = [
    g && g.status !== "active" && "Unavailable",
    row.pastCap ? "Past the 2,000-repo limit" : !row.visible && "Not visible on GitHub",
    needsWrite && !fixing && "Needs write to finish setup",
  ].filter(Boolean)
  const Icon = row.visible ? BookMarked : Lock
  const label = row.visible ? row.name : "hidden repo"
  return (
    <div className="px-3 py-2 text-sm">
      <div className="flex min-h-7 flex-wrap items-center gap-x-2 gap-y-1">
        <Icon className="size-4 shrink-0 text-muted-foreground" />
        <span className={cn("min-w-0 max-w-full truncate font-medium", removed && "text-muted-foreground line-through")}>{row.visible ? repoName(row.name) : "Hidden repo"}</span>
        {working && !removed && <Star className="size-3.5 shrink-0 fill-current text-muted-foreground" aria-label="Working repo" />}
        {row.visible && <Pill>{(g?.max_access ?? "write") === "write" && !(needsWrite && !fixing) ? "Can change" : "Read only"}</Pill>}
        {g?.scope === "shared" && <Pill>Shared</Pill>}
        {added && <Pill className="text-success">To add</Pill>}
        {fixing && <Pill className="text-success">To update</Pill>}
        {removed && <Pill className="text-destructive">To remove</Pill>}
        <span className="-mr-1.5 ml-auto flex shrink-0 items-center gap-1">
          {!removed && repairable && row.visible && (fixing
            ? <Button variant="ghost" size="xs" aria-label={`Undo update of ${label}`} onClick={() => onFix(false)}><Undo2 />Undo</Button>
            : <Button variant="outline" size="xs" aria-label={`Finish setup of ${label}`} onClick={() => onFix(true)}>Finish setup</Button>)}
          {removed
            ? <Button variant="ghost" size="xs" aria-label={`Undo removal of ${label}`} onClick={onUndo}><Undo2 />Undo</Button>
            : <Button variant="ghost" size="xs" aria-label={`Remove ${label}`} onClick={onRemove}><Trash2 />Remove</Button>}
        </span>
      </div>
      {facts.length > 0 && (
        <div className="mt-0.5 space-y-0.5 pl-6 text-xs text-warning">
          {facts.map((f) => <p key={String(f)} className="flex items-center gap-1"><TriangleAlert className="size-3 shrink-0" />{f}</p>)}
        </div>
      )}
    </div>
  )
}
