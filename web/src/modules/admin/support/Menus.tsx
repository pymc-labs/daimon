import { ChevronRight, ListChecks, Plus } from "lucide-react"
import { useState } from "react"
import { toast } from "sonner"

import { Drawer } from "@/components/Drawer"
import { InheritedSelect } from "@/components/inherited"
import { PanelHead, PanelSection, SettingRow } from "@/components/panel"
import { SettingsList } from "@/components/settings"
import { Button } from "@/components/ui/button"
import { useConfirm } from "@/lib/confirm"
import { plural } from "@/lib/format"
import { rowProps, useParam } from "@/lib/filters"
import { usePlaces, useSupportSave, type Outcome } from "./api"
import { Actions, Failure } from "./Limits"
import { menuKey } from "./lib"
import { emptyMenu, menuErrors, toCatalog, toDraft, type MenuDraft } from "./menuDraft"
import { MenuEditor } from "./MenuEditor"
import { PlacePicker, Section } from "./parts"
import type { Menu, MenuUse, Place, SupportPage } from "./types"

/** Step 5: the menus, and which one each place uses. */
export function Menus({ d }: { d: SupportPage }) {
  const [, setOpen] = useParam("menu")
  return (
    <>
      <Section title="Menus" add={<Button size="xs" variant="outline" onClick={() => setOpen("new")}><Plus />Add menu</Button>}>
        <div className="divide-y rounded-lg border text-sm">
          {d.menus.map((m) => (
            <div key={m.key} {...rowProps(() => setOpen(m.key))} className="flex min-h-11 cursor-pointer items-center gap-3 px-4 py-1.5 hover:bg-accent/50 focus-visible:bg-accent/50 focus-visible:outline-none">
              <span className="min-w-0 truncate font-medium">{m.name}</span>
              <span className="shrink-0 text-xs text-muted-foreground tabular">{plural(m.options, "option")}</span>
              <span className="ml-auto min-w-0 truncate text-muted-foreground max-sm:hidden">{m.used_in.map((u) => u.name).join(", ")}</span>
              <ChevronRight className="size-4 shrink-0 text-faint max-sm:ml-auto" />
            </div>
          ))}
          {!d.menus.length && <div className="px-4 py-3 text-muted-foreground">No menus</div>}
        </div>
      </Section>
      <Used d={d} />
      <Drawer param="menu" title="Menu" ids={d.menus.map((m) => m.key)}>
        {(id) => (id === "new" ? <MenuDetail key="new" d={d} /> : d.menus.find((m) => m.key === id) ? <MenuDetail key={id} d={d} menu={d.menus.find((m) => m.key === id)} /> : null)}
      </Drawer>
    </>
  )
}

/** The menu each place uses: the workspace, then the agents and channels that choose their own. */
export function Used({ d }: { d: SupportPage }) {
  const save = useSupportSave()
  const places = usePlaces(d)
  const [added, setAdded] = useState<Place[]>([])
  const [error, setError] = useState<string | null>(null)
  const workspace = d.menu_use.find((u) => u.scope === "workspace")
  const rows = d.menu_use.filter((u) => u.scope !== "workspace" && (u.menu !== null || added.some((a) => a.scope === u.scope && a.id === u.scope_id)))
  const name = (key: string | null) => d.menus.find((m) => m.key === key)?.name
  const options = d.menus.map((m) => ({ value: m.key, label: m.name }))
  const row = (u: MenuUse, from: string, label: string) => ({
    key: `${u.scope}|${u.scope_id}`,
    label: u.name,
    control: (
      <InheritedSelect
        bare label={u.name} value={u.menu} disabled={save.pending} inherited={{ value: "", label, from }} options={options}
        onChange={async (menu) => setError((await save.run("menus/use", { scope: u.scope, scope_id: u.scope_id, menu, expected: u.menu })).error)}
      />
    ),
  })
  return (
    <Section
      title="Menu used"
      add={<PlacePicker places={places} taken={rows.map((u) => ({ scope: u.scope as Place["scope"], id: u.scope_id, name: u.name }))} onPick={(p) => setAdded((a) => [...a, p])} />}
    >
      <SettingsList className="max-w-none" rows={[
        ...(workspace ? [row(workspace, "Built-in", "")] : []),
        ...rows.map((u) => row(u, "Workspace", name(workspace?.menu ?? null) ?? "")),
      ]} />
      {error && <p role="alert" className="mt-2 text-xs text-destructive">{error}</p>}
    </Section>
  )
}

/** The ids of every row in order: it changes when a row is added, removed or moved. */
const shape = (m: MenuDraft) => JSON.stringify(m.options.map((o) => [o.id, o.fields.map((f) => f.id), o.price.steps.map((x) => x.id)]))

/** A menu in a drawer; `menu` is missing for a new one. */
function MenuDetail({ d, menu }: { d: SupportPage; menu?: Menu }) {
  const save = useSupportSave()
  const confirm = useConfirm()
  const [, setOpen] = useParam("menu")
  const [draft, setDraft] = useState(() => (menu ? toDraft(menu.body) : emptyMenu()))
  // The revision as it was read: what a save expects, whatever has been read since.
  const [revision, setRevision] = useState(menu?.revision ?? null)
  const [out, setOut] = useState<Outcome | null>(null)
  const [inUse, setInUse] = useState<string | null>(null)
  const errors = menuErrors(out?.error ?? null, out?.problems ?? [], draft)
  // An error names rows by place, so it goes when the rows move.
  const edit = (next: MenuDraft) => { if (shape(next) !== shape(draft)) setOut(null); setDraft(next) }
  const key = menu?.key ?? menuKey(draft.name, d.menus.map((m) => m.key))
  const put = async (body: Record<string, unknown> | null) => {
    const r = await save.run(`menus/${encodeURIComponent(key)}`, { menu: body, expected_revision: revision })
    setOut(r)
    if (!r.error) { setOpen(null); toast(body ? "Menu saved" : "Menu removed"); return }
    if (!r.fresh) return
    const now = r.fresh.menus.find((m) => m.key === key)
    if (now) { setDraft(toDraft(now.body)); setRevision(now.revision) } else if (menu) setOpen(null)
  }
  const remove = async () => {
    if (menu?.used_in.length) { setInUse(`Used in ${menu.used_in.map((u) => u.name).join(", ")}. Pick another menu there first.`); return }
    if (await confirm({ title: "Remove menu?", body: menu?.name, action: "Remove", destructive: true })) await put(null)
  }
  return (
    <div className="flex min-h-full flex-col">
      <PanelHead icon={<ListChecks className="size-4 text-muted-foreground" />} title={menu?.name || "New menu"} subtitle={menu ? plural(menu.options, "option") : undefined} />
      {menu && (
        <PanelSection>
          <SettingRow label="Used in">{menu.used_in.length ? menu.used_in.map((u) => u.name).join(", ") : <span className="text-muted-foreground">No place</span>}</SettingRow>
        </PanelSection>
      )}
      <PanelSection><MenuEditor draft={draft} errors={errors} onChange={edit} /></PanelSection>
      <div className="sticky bottom-0 mt-auto border-t bg-popover px-5 py-3">
        <Actions>
          {menu && <Button variant="ghost" className="mr-auto text-destructive max-sm:mr-0" disabled={save.pending} onClick={() => void remove()}>Remove</Button>}
          <Button variant="outline" onClick={() => setOpen(null)}>Cancel</Button>
          <Button disabled={save.pending || !draft.name.trim()} onClick={() => void put(toCatalog(draft))}>Save</Button>
        </Actions>
        <Failure>{errors.rest ?? inUse}</Failure>
      </div>
    </div>
  )
}
