import { Plus, RefreshCw } from "lucide-react"
import { useState } from "react"
import { useNavigate } from "react-router"

import { QueryState } from "@/components/QueryState"
import { AgentAvatar, Ago } from "@/components/common"
import { FilterBar, SearchInput } from "@/components/filters"
import { Page } from "@/components/shell/Page"
import { Button } from "@/components/ui/button"
import { Dialog, DialogContent, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog"
import { Input } from "@/components/ui/input"
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table"
import { rowProps, useParam } from "@/lib/filters"
import { plural } from "@/lib/format"
import type { LibrarySkill, SyncedSkill } from "../types"
import { useLibrarySkills, useSkillWrites } from "./api"
import { sourceLabel } from "./skill-meta"
import { LIBRARY_TABS } from "./tabs"

function UsedBy({ skill }: { skill: LibrarySkill }) {
  const agents = skill.used_by.filter((u) => u.kind === "agent")
  const channels = skill.used_by.length - agents.length
  if (!skill.used_by.length) return <span className="text-faint">Unused</span>
  return (
    <span className="flex items-center gap-2">
      <span className="flex -space-x-1.5">
        {agents.slice(0, 3).map((u) => <AgentAvatar key={u.id} name={u.name} className="size-5 text-[9px] ring-2 ring-background" />)}
      </span>
      <span className="text-muted-foreground">{[agents.length && plural(agents.length, "agent"), channels && plural(channels, "channel")].filter(Boolean).join(", ")}</span>
    </span>
  )
}

export function Skills() {
  const query = useLibrarySkills()
  const navigate = useNavigate()
  const [q, setQ] = useParam("q")
  const [syncing, setSyncing] = useState(false)
  const [creating, setCreating] = useState(false)
  const all = query.data ?? []
  const rows = all.filter((s) => !q || `${s.title} ${s.name} ${s.description}`.toLowerCase().includes(q.toLowerCase()))
  return (
    <Page
      crumbs={[{ label: "Agents", to: "/agents" }, { label: "Skills" }]}
      tabs={LIBRARY_TABS}
      flush
      actions={
        <>
          <Button size="sm" variant="outline" onClick={() => setSyncing(true)}><RefreshCw />Sync from GitHub</Button>
          <Button size="sm" onClick={() => setCreating(true)}><Plus />New skill</Button>
        </>
      }
    >
      <FilterBar>
        <SearchInput value={q} onChange={(v) => setQ(v || null)} className="w-56" />
        <span className="ml-auto text-xs text-muted-foreground tabular">{plural(rows.length, "skill")}</span>
      </FilterBar>
      <QueryState query={query} className="px-6" isEmpty={() => !rows.length} empty={all.length ? "No skills match" : "No skills"}>{() => (
        <Table>
          <TableHeader>
            <TableRow className="hover:bg-transparent">
              <TableHead className="pl-4 lg:pl-6">Name</TableHead>
              <TableHead className="max-md:hidden">Source</TableHead>
              <TableHead>Used by</TableHead>
              <TableHead className="pr-4 text-right lg:pr-6">Updated</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {rows.map((s) => (
              <TableRow key={s.id} {...rowProps(() => navigate(`/agents/skills/${encodeURIComponent(s.id)}`))} className="cursor-pointer">
                <TableCell className="pl-4 lg:pl-6">
                  <span className="block min-w-0">
                    <span className="block truncate font-medium">{s.title}</span>
                    <span className="block max-w-[44ch] truncate text-xs text-muted-foreground">{s.description}</span>
                  </span>
                </TableCell>
                <TableCell className="text-muted-foreground max-md:hidden">{sourceLabel(s)}</TableCell>
                <TableCell><UsedBy skill={s} /></TableCell>
                <TableCell className="pr-4 text-right text-muted-foreground lg:pr-6"><Ago iso={s.updated_at} /></TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
      )}</QueryState>
      <Sync open={syncing} onOpenChange={setSyncing} />
      <NewSkill open={creating} onOpenChange={setCreating} />
    </Page>
  )
}

function NewSkill({ open, onOpenChange }: { open: boolean; onOpenChange: (o: boolean) => void }) {
  const { create } = useSkillWrites()
  const navigate = useNavigate()
  const [name, setName] = useState("")
  const [description, setDescription] = useState("")
  const go = () => name.trim() && create.mutate({ name: name.trim(), description: description.trim() }, {
    onSuccess: (made) => { onOpenChange(false); setName(""); setDescription(""); navigate(`/agents/skills/${encodeURIComponent(made.id)}`) },
  })
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="sm:max-w-md">
        <DialogHeader><DialogTitle>New skill</DialogTitle></DialogHeader>
        <form id="new-skill" className="space-y-2" onSubmit={(e) => { e.preventDefault(); go() }}>
          <Input value={name} onChange={(e) => setName(e.target.value)} placeholder="Name" aria-label="Name" className="h-8" autoFocus />
          <Input value={description} onChange={(e) => setDescription(e.target.value)} placeholder="Description" aria-label="Description" className="h-8" />
        </form>
        <DialogFooter>
          <Button variant="ghost" onClick={() => onOpenChange(false)}>Cancel</Button>
          <Button type="submit" form="new-skill" disabled={!name.trim() || create.isPending}>Create</Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}

const MARK: Record<SyncedSkill["action"], string> = { created: "Added", updated: "Updated", skipped: "Skipped", failed: "Failed" }

function Sync({ open, onOpenChange }: { open: boolean; onOpenChange: (o: boolean) => void }) {
  const { sync } = useSkillWrites()
  const [url, setUrl] = useState("")
  const [branch, setBranch] = useState("main")
  const [path, setPath] = useState("")
  const done = sync.data
  const close = (o: boolean) => { if (!o) sync.reset(); onOpenChange(o) }
  return (
    <Dialog open={open} onOpenChange={close}>
      <DialogContent className="sm:max-w-md">
        <DialogHeader><DialogTitle>Sync from GitHub</DialogTitle></DialogHeader>
        {done ? (
          <ul className="max-h-64 space-y-1 overflow-y-auto text-sm">
            {done.map((s) => <li key={s.name} className="flex items-baseline gap-2"><span className="font-medium">{s.name}</span><span className="text-xs text-muted-foreground">{MARK[s.action]}{s.detail ? `, ${s.detail}` : ""}</span></li>)}
            {!done.length && <li className="text-muted-foreground">No skills found</li>}
          </ul>
        ) : (
          <form id="sync" className="space-y-2" onSubmit={(e) => { e.preventDefault(); if (url.trim()) sync.mutate({ url: url.trim(), branch: branch.trim(), path: path.trim() }) }}>
            <Input value={url} onChange={(e) => setUrl(e.target.value)} placeholder="https://github.com/owner/repo" aria-label="Repository" className="h-8" autoFocus />
            <div className="grid grid-cols-2 gap-2">
              <Input value={branch} onChange={(e) => setBranch(e.target.value)} placeholder="Branch" aria-label="Branch" className="h-8" />
              <Input value={path} onChange={(e) => setPath(e.target.value)} placeholder="Folder" aria-label="Folder" className="h-8" />
            </div>
          </form>
        )}
        <DialogFooter>
          <Button variant="ghost" onClick={() => close(false)}>{done ? "Close" : "Cancel"}</Button>
          {!done && <Button type="submit" form="sync" disabled={!url.trim() || sync.isPending}>Sync</Button>}
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
