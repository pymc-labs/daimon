import { useMutation, useQueryClient } from "@tanstack/react-query"
import { useState } from "react"
import { toast } from "sonner"

import { CopyButton } from "@/components/common"
import { MultiPicker, type PickItem } from "@/components/picker"
import { Button } from "@/components/ui/button"
import { Dialog, DialogContent, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog"
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select"
import { Switch } from "@/components/ui/switch"
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip"
import { api } from "@/lib/api"
import { useConfirm } from "@/lib/confirm"
import { useMe } from "@/lib/hooks"
import { cn } from "@/lib/utils"
import { ACCESS, expiry, KEY, useChange } from "./bits"
import { publicRefusal } from "./rules"
import type { ArtifactAccess, ArtifactDetail, PersonRef } from "./types"

const DAYS = [["never", "Never"], ["7", "7 days"], ["30", "30 days"], ["90", "90 days"], ["365", "1 year"]] as const
const inDays = (d: number) => new Date(Date.now() + d * 86_400_000).toISOString()

function AccessCard({ access, fact, selected, reason, onSelect }: { access: ArtifactAccess; fact: string; selected: boolean; reason: string | null; onSelect: () => void }) {
  const { icon: Icon, label } = ACCESS[access]
  const card = (
    <button
      type="button" role="radio" aria-checked={selected} aria-disabled={!!reason} onClick={() => !reason && onSelect()}
      className={cn(
        "flex w-full items-center gap-3 rounded-lg border p-3 text-left transition-colors",
        selected ? "border-primary bg-accent/50 ring-1 ring-primary" : "hover:bg-accent/50",
        reason && "cursor-not-allowed opacity-50 hover:bg-transparent",
      )}
    >
      <span className="flex size-9 shrink-0 items-center justify-center rounded-full bg-accent"><Icon className="size-4" /></span>
      <span className="min-w-0 flex-1">
        <span className="block text-sm font-medium">{label}</span>
        <span className="block truncate text-xs text-muted-foreground">{fact}</span>
      </span>
      <span className={cn("flex size-4 shrink-0 items-center justify-center rounded-full border", selected && "border-primary bg-primary")}>
        {selected && <span className="size-1.5 rounded-full bg-primary-foreground" />}
      </span>
    </button>
  )
  if (!reason) return card
  return (
    <Tooltip>
      <TooltipTrigger asChild><span className="block">{card}</span></TooltipTrigger>
      <TooltipContent>{reason}</TooltipContent>
    </Tooltip>
  )
}

const Field = ({ label, children }: { label: string; children: React.ReactNode }) => (
  <div className="space-y-1.5"><div className="text-xs font-medium text-muted-foreground">{label}</div>{children}</div>
)

/** The people picker: searches the workspace; chips keep the names of everyone picked. */
function People({ value, onChange, label, locked }: { value: PersonRef[]; onChange: (p: PersonRef[]) => void; label: string; locked?: string[] }) {
  const [found, setFound] = useState<PersonRef[]>([])
  const search = (q: string) => api.get<PersonRef[]>(`/api/artifacts/people?q=${encodeURIComponent(q)}`).then(setFound)
  const known = new Map([...found, ...value].map((p) => [p.id, p]))
  const items: PickItem[] = [...known.values()].map((p) => ({ id: p.id, label: p.kind === "visitor" ? `${p.name} (guest)` : p.name }))
  return (
    <MultiPicker
      items={items} value={value.map((p) => p.id)} locked={locked} placeholder={label}
      onSearch={(q) => void search(q)}
      onAdd={(id) => { const p = known.get(id); if (p) onChange([...value, p]) }}
      onRemove={(id) => onChange(value.filter((p) => p.id !== id))}
    />
  )
}

function Form({ a, close }: { a: ArtifactDetail; close: () => void }) {
  const { data: me } = useMe()
  const qc = useQueryClient()
  const confirm = useConfirm()
  const left = a.expires_at ? Math.ceil((new Date(a.expires_at).getTime() - Date.now()) / 86_400_000) : null
  const [access, setAccess] = useState(a.access)
  const [people, setPeople] = useState(a.people)
  const [editors, setEditors] = useState(a.editors)
  const [share, setShare] = useState(a.editors_can_share)
  const [exp, setExp] = useState(left === null ? "never" : "keep")
  const save = useChange(a.id, () => { toast("Sharing saved"); close() })
  const replace = useMutation({
    mutationFn: () => api.post(`/api/artifacts/${a.id}/link`),
    onSuccess: () => { void qc.invalidateQueries({ queryKey: KEY }); toast("Link replaced") },
  })
  const why = publicRefusal(a)
  const pick = (v: ArtifactAccess) => { setAccess(v); if (v === "public" && exp === "never") setExp("30") }
  const submit = () => save.mutate({
    access, people: people.map((p) => p.id), editors: editors.map((p) => p.id), editors_can_share: share,
    ...(exp === "keep" ? {} : { expires_at: exp === "never" ? null : inDays(Number(exp)) }),
  })
  return (
    <>
      <div role="radiogroup" aria-label="Who can open" className="space-y-2">
        <AccessCard access="workspace" selected={access === "workspace"} fact={`Everyone in ${me?.workspace.name ?? "the workspace"}`} reason={null} onSelect={() => pick("workspace")} />
        <AccessCard access="people" selected={access === "people"} fact="Only the people you add" reason={null} onSelect={() => pick("people")} />
        <AccessCard access="public" selected={access === "public"} fact="Anyone with the link, no sign in" reason={why} onSelect={() => pick("public")} />
      </div>
      <div className="space-y-4">
        {access !== "public" && <Field label={access === "people" ? "People" : "Also share with"}><People value={people} onChange={setPeople} label="Search people" /></Field>}
        {access === "public" && a.link && (
          <Field label="Link">
            <div className="flex items-center gap-1.5">
              <div className="h-8 min-w-0 flex-1 truncate rounded-md border bg-muted px-2.5 font-mono text-xs leading-8" title={a.link}>{a.link}</div>
              <CopyButton text={a.link} label="Copy link" className="size-8 border" />
              <Button
                variant="outline" size="sm" className="h-8" disabled={replace.isPending}
                onClick={async () => { if (await confirm({ title: "Replace link?", body: "The old link stops working.", action: "Replace", destructive: true })) replace.mutate() }}
              >Replace link</Button>
            </div>
          </Field>
        )}
        <Field label="Editors">
          <People value={editors} onChange={setEditors} label="Search people" />
          <label className="mt-2.5 flex items-center justify-between gap-3 text-sm">
            Editors can share
            <Switch checked={share} onCheckedChange={setShare} aria-label="Editors can share" />
          </label>
        </Field>
        <Field label="Expires">
          <Select value={exp} onValueChange={setExp}>
            <SelectTrigger className="w-44"><SelectValue /></SelectTrigger>
            <SelectContent>
              {exp === "keep" && <SelectItem value="keep">{expiry(a.expires_at)}</SelectItem>}
              {DAYS.map(([v, l]) => <SelectItem key={v} value={v}>{l}</SelectItem>)}
            </SelectContent>
          </Select>
        </Field>
      </div>
      <DialogFooter className="-mx-5 -mb-5 flex-row sm:justify-end">
        <Button variant="outline" className="max-sm:flex-1" onClick={close}>Cancel</Button>
        <Button className="max-sm:flex-1" disabled={save.isPending} onClick={submit}>Save</Button>
      </DialogFooter>
    </>
  )
}

export function ShareDialog({ artifact, open, onOpenChange }: { artifact: ArtifactDetail; open: boolean; onOpenChange: (o: boolean) => void }) {
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-h-[calc(100dvh-2rem)] gap-5 overflow-y-auto p-5 sm:max-w-lg [&>*]:min-w-0">
        <DialogHeader><DialogTitle className="truncate pr-8 text-base">Share {artifact.title}</DialogTitle></DialogHeader>
        <Form key={String(open)} a={artifact} close={() => onOpenChange(false)} />
      </DialogContent>
    </Dialog>
  )
}
