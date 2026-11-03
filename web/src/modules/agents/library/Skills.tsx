import { RefreshCw, Trash2 } from "lucide-react"
import { useState } from "react"

import { Drawer } from "@/components/Drawer"
import { QueryState } from "@/components/QueryState"
import { Ago } from "@/components/common"
import { Page } from "@/components/shell/Page"
import { Button } from "@/components/ui/button"
import { Dialog, DialogContent, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog"
import { Input } from "@/components/ui/input"
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table"
import { rowProps, useParam } from "@/lib/filters"
import { useConfirm } from "@/lib/confirm"
import { plural } from "@/lib/format"
import { PanelSection, Tag } from "@/modules/admin/shared"
import type { LibrarySkill, SyncedSkill } from "../types"
import { useLibrarySkills, useSkillVersions, useSkillWrites } from "./api"
import { LIBRARY_TABS } from "./tabs"

const usedBy = (s: LibrarySkill) => {
  const agents = s.used_by.filter((u) => u.kind === "agent").length
  const channels = s.used_by.length - agents
  return [agents && plural(agents, "agent"), channels && plural(channels, "channel")].filter(Boolean).join(", ") || "Unused"
}

export function Skills() {
  const query = useLibrarySkills()
  const [, setSkill] = useParam("skill")
  const [syncing, setSyncing] = useState(false)
  const rows = query.data ?? []
  return (
    <Page
      crumbs={[{ label: "Agents", to: "/agents" }, { label: "Skills" }]}
      tabs={LIBRARY_TABS}
      flush
      actions={<Button size="sm" onClick={() => setSyncing(true)}><RefreshCw />Sync from GitHub</Button>}
    >
      <QueryState query={query} className="px-6" isEmpty={() => !rows.length} empty="No skills">{() => (
        <Table>
          <TableHeader>
            <TableRow className="hover:bg-transparent">
              <TableHead className="pl-4 lg:pl-6">Name</TableHead>
              <TableHead>Version</TableHead>
              <TableHead>Used by</TableHead>
              <TableHead className="pr-4 text-right lg:pr-6">Added</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {rows.map((s) => (
              <TableRow key={s.id} {...rowProps(() => setSkill(s.id))} className="cursor-pointer">
                <TableCell className="pl-4 lg:pl-6">
                  <span className="flex items-center gap-2"><span className="font-medium">{s.name}</span>{s.source === "anthropic" && <Tag>Built in</Tag>}{s.default && <Tag>Default</Tag>}</span>
                </TableCell>
                <TableCell className="text-muted-foreground">{s.version || "–"}</TableCell>
                <TableCell className="text-muted-foreground">{usedBy(s)}</TableCell>
                <TableCell className="pr-4 text-right text-muted-foreground lg:pr-6"><Ago iso={s.created_at} /></TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
      )}</QueryState>
      <Drawer param="skill" title="Skill" ids={rows.map((s) => s.id)}>{(id) => <SkillView skill={rows.find((s) => s.id === id)} />}</Drawer>
      <Sync open={syncing} onOpenChange={setSyncing} />
    </Page>
  )
}

function SkillView({ skill }: { skill?: LibrarySkill }) {
  const confirm = useConfirm()
  const { remove } = useSkillWrites()
  const [, setSkill] = useParam("skill")
  const versions = useSkillVersions(skill?.id ?? "")
  if (!skill) return <div className="p-5 text-sm text-muted-foreground">Not found</div>
  const locked = skill.source !== "custom" || skill.default
  const drop = async () => {
    if (!(await confirm({ title: `Delete ${skill.name}?`, action: "Delete", destructive: true }))) return
    remove.mutate(skill.id, { onSuccess: () => setSkill(null) })
  }
  return (
    <>
      <div className="flex items-center gap-2 px-5 py-4">
        <h2 className="text-lg font-semibold">{skill.name}</h2>
        {skill.source === "anthropic" && <Tag>Built in</Tag>}{skill.default && <Tag>Default</Tag>}
        {!locked && <Button variant="outline" size="sm" className="ml-auto" disabled={remove.isPending} onClick={() => void drop()}><Trash2 />Delete</Button>}
      </div>
      <PanelSection title="Used by">
        {!skill.used_by.length ? <div className="text-sm text-muted-foreground">Nothing</div> : (
          <ul className="space-y-1 text-sm">
            {skill.used_by.map((u) => <li key={`${u.kind}:${u.name}`} className="flex items-center gap-2">{u.name}<span className="text-xs text-muted-foreground">{u.kind === "agent" ? "Agent" : "Channel"}</span></li>)}
          </ul>
        )}
      </PanelSection>
      {skill.source === "custom" && (
        <PanelSection title="Versions">
          <QueryState query={versions} isEmpty={(v) => !v.length} empty="None">{(v) => (
            <ul className="space-y-1 text-sm">
              {v.map((x, i) => <li key={x.version} className="flex items-center gap-2"><span className="font-mono text-xs">{x.version}</span>{i === 0 && <Tag>Latest</Tag>}<span className="ml-auto text-xs text-muted-foreground"><Ago iso={x.created_at} /></span></li>)}
            </ul>
          )}</QueryState>
        </PanelSection>
      )}
    </>
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
