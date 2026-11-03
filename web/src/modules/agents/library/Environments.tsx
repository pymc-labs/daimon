import { Archive, Boxes, Plus } from "lucide-react"
import { useState } from "react"

import { Drawer } from "@/components/Drawer"
import { QueryState } from "@/components/QueryState"
import { Ago } from "@/components/common"
import { FilterBar, SearchInput } from "@/components/filters"
import { ChannelLink } from "@/components/links"
import { PanelHead, PanelSection, SettingRow, Tag } from "@/components/panel"
import { Page } from "@/components/shell/Page"
import { Button } from "@/components/ui/button"
import { Checkbox } from "@/components/ui/checkbox"
import { DropdownMenu, DropdownMenuContent, DropdownMenuItem, DropdownMenuTrigger } from "@/components/ui/dropdown-menu"
import { Input } from "@/components/ui/input"
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table"
import { Textarea } from "@/components/ui/textarea"
import { ToggleGroup, ToggleGroupItem } from "@/components/ui/toggle-group"
import { rowProps, useParam } from "@/lib/filters"
import { useConfirm } from "@/lib/confirm"
import { plural } from "@/lib/format"
import { ECOSYSTEMS, type EnvironmentDoc, type EnvironmentInput } from "../types"
import { useEnvironments, useEnvironmentWrites } from "./api"
import { LIBRARY_TABS } from "./tabs"

const count = (e: EnvironmentDoc) => Object.values(e.packages).reduce((n, p) => n + (p?.length ?? 0), 0)
const entries = (text: string, separator: RegExp) => [...new Set(text.split(separator).map((x) => x.trim()).filter(Boolean))]
const lines = (text: string) => entries(text, /\n/)
const hostList = (text: string) => entries(text, /[\s,]+/)
const NETWORK = { limited: "Limited", unrestricted: "Open" }

export function Environments() {
  const query = useEnvironments()
  const [, setEnv] = useParam("env")
  const [q, setQ] = useParam("q")
  const all = query.data ?? []
  const rows = all.filter((e) => !q || `${e.name} ${e.description}`.toLowerCase().includes(q.toLowerCase()))
  return (
    <Page
      crumbs={[{ label: "Agents", to: "/agents" }, { label: "Environments" }]}
      tabs={LIBRARY_TABS}
      flush
      actions={<Button size="sm" onClick={() => setEnv("new")}><Plus />New environment</Button>}
    >
      <FilterBar>
        <SearchInput value={q} onChange={(v) => setQ(v || null)} className="w-56" />
        <span className="ml-auto text-xs text-muted-foreground tabular">{plural(rows.length, "environment")}</span>
      </FilterBar>
      <QueryState query={query} className="px-6" isEmpty={() => !rows.length} empty={all.length ? "No environments match" : "No environments"}>{() => (
        <Table>
          <TableHeader>
            <TableRow className="hover:bg-transparent">
              <TableHead className="pl-4 lg:pl-6">Name</TableHead>
              <TableHead>Network</TableHead>
              <TableHead className="text-right max-sm:hidden">Packages</TableHead>
              <TableHead className="text-right max-md:hidden">Used by</TableHead>
              <TableHead className="pr-4 text-right lg:pr-6">Updated</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {rows.map((e) => (
              <TableRow key={e.id} {...rowProps(() => setEnv(e.id))} className="cursor-pointer">
                <TableCell className="pl-4 lg:pl-6">
                  <span className="flex items-center gap-2"><span className="font-medium">{e.name}</span>{e.managed && <Tag>Built in</Tag>}</span>
                  {e.description && <span className="block max-w-[44ch] truncate text-xs text-muted-foreground">{e.description}</span>}
                </TableCell>
                <TableCell className="text-muted-foreground">{e.cloud ? NETWORK[e.network] : "Self-hosted"}</TableCell>
                <TableCell className="text-right text-muted-foreground tabular max-sm:hidden">{count(e) || "–"}</TableCell>
                <TableCell className="text-right text-muted-foreground tabular max-md:hidden">{e.used_by?.length || "–"}</TableCell>
                <TableCell className="pr-4 text-right text-muted-foreground lg:pr-6"><Ago iso={e.updated_at} /></TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
      )}</QueryState>
      <Drawer param="env" title="Environment" ids={rows.map((e) => e.id)}>
        {(id) => <EnvironmentForm key={`${id}|${rows.find((e) => e.id === id)?.updated_at ?? ""}`} env={id === "new" ? null : rows.find((e) => e.id === id) ?? null} missing={id !== "new" && !rows.some((e) => e.id === id)} />}
      </Drawer>
    </Page>
  )
}

const blank: EnvironmentInput = { name: "", description: "", network: "limited", allowed_hosts: [], allow_package_managers: true, allow_mcp_servers: true, packages: {} }

export function EnvironmentForm({ env, missing }: { env: EnvironmentDoc | null; missing: boolean }) {
  const confirm = useConfirm()
  const [, setEnv] = useParam("env")
  const { create, save, archive } = useEnvironmentWrites((message) => confirm({ title: "Allow internet access?", body: message, action: "Continue", destructive: true }))
  const first = {
    name: env?.name ?? "", description: env?.description ?? "", network: env?.network ?? "limited", hosts: (env?.allowed_hosts ?? []).join("\n"),
    managers: env?.allow_package_managers ?? true, mcp: env?.allow_mcp_servers ?? true,
    packages: Object.fromEntries(ECOSYSTEMS.map((k) => [k, (env?.packages[k] ?? []).join("\n")])) as Record<string, string>,
  }
  const [name, setName] = useState(first.name)
  const [description, setDescription] = useState(first.description)
  const [network, setNetwork] = useState<EnvironmentInput["network"]>(first.network)
  const [hosts, setHosts] = useState(first.hosts)
  const [managers, setManagers] = useState(first.managers)
  const [mcp, setMcp] = useState(first.mcp)
  const [packages, setPackages] = useState(first.packages)
  const [extra, setExtra] = useState<string[]>([])
  if (missing) return <div className="p-5 text-sm text-muted-foreground">Not found</div>
  const locked = env?.managed === true || (env !== null && !env.cloud)
  const shown = ECOSYSTEMS.filter((k) => first.packages[k] || extra.includes(k))
  const build = (n: typeof first): EnvironmentInput => ({
    ...blank, name: n.name.trim(), description: n.description.trim(), network: n.network, allowed_hosts: hostList(n.hosts),
    allow_package_managers: n.managers, allow_mcp_servers: n.mcp,
    packages: Object.fromEntries(ECOSYSTEMS.map((k) => [k, lines(n.packages[k] ?? "")]).filter(([, v]) => (v as string[]).length)),
  })
  const spec = build({ name, description, network, hosts, managers, mcp, packages })
  const dirty = env ? JSON.stringify(spec) !== JSON.stringify(build(first)) : name.trim() !== ""
  const discard = () => {
    setName(first.name); setDescription(first.description); setNetwork(first.network); setHosts(first.hosts)
    setManagers(first.managers); setMcp(first.mcp); setPackages(first.packages)
  }
  const busy = create.isPending || save.isPending || archive.isPending
  const go = () => env
    ? save.mutate({ id: env.id, spec, expected_updated_at: env.updated_at })
    : create.mutate(spec, { onSuccess: (made) => setEnv(made.id) })
  const retire = async () => {
    if (!env || !(await confirm({ title: `Archive ${env.name}?`, action: "Archive", destructive: true }))) return
    archive.mutate(env.id, { onSuccess: () => setEnv(null) })
  }
  const free = ECOSYSTEMS.filter((k) => !shown.includes(k))
  return (
    <form className="flex flex-1 flex-col" onSubmit={(e) => { e.preventDefault(); if (!locked && dirty && name.trim()) go() }}>
      <PanelHead
        icon={<Boxes className="size-5" />}
        title={env ? env.name : "New environment"}
        subtitle={env ? (env.cloud ? NETWORK[env.network] + " network" : "Self-hosted") : undefined}
        tags={env?.managed ? <Tag>Built in</Tag> : undefined}
        menu={env && !locked ? [{ label: "Archive", icon: <Archive />, destructive: true, disabled: busy, onSelect: () => void retire() }] : []}
      />
      <PanelSection>
        <div className="space-y-2.5">
          {!env && <SettingRow label="Name"><Input aria-label="Name" value={name} onChange={(e) => setName(e.target.value)} className="h-8" autoFocus /></SettingRow>}
          <SettingRow label="Description"><Input aria-label="Description" value={description} onChange={(e) => setDescription(e.target.value)} className="h-8" disabled={locked} /></SettingRow>
          <SettingRow label="Network">
            <ToggleGroup type="single" value={network} onValueChange={(v) => v && setNetwork(v as typeof network)} variant="segmented" size="sm" disabled={locked}>
              <ToggleGroupItem value="limited" className="px-3 text-xs">Limited</ToggleGroupItem>
              <ToggleGroupItem value="unrestricted" className="px-3 text-xs">Open</ToggleGroupItem>
            </ToggleGroup>
          </SettingRow>
          {network === "limited" && (
            <>
              <SettingRow label="Allowed hosts" top><Textarea aria-label="Allowed hosts" value={hosts} onChange={(e) => setHosts(e.target.value)} rows={3} className="font-mono text-xs" disabled={locked} /></SettingRow>
              <SettingRow label="Allow">
                <div className="space-y-1.5">
                  <label className="flex items-center gap-2"><Checkbox checked={managers} onCheckedChange={(c) => setManagers(c === true)} disabled={locked} />Package managers</label>
                  <label className="flex items-center gap-2"><Checkbox checked={mcp} onCheckedChange={(c) => setMcp(c === true)} disabled={locked} />The agent's own servers</label>
                </div>
              </SettingRow>
            </>
          )}
        </div>
      </PanelSection>
      <PanelSection title="Packages">
        <div className="space-y-2.5">
          {shown.map((k) => (
            <SettingRow key={k} label={k} top>
              <Textarea aria-label={k} value={packages[k] ?? ""} onChange={(e) => setPackages({ ...packages, [k]: e.target.value })} rows={2} className="font-mono text-xs" disabled={locked} />
            </SettingRow>
          ))}
          {!shown.length && <div className="text-sm text-muted-foreground">None</div>}
          {!locked && free.length > 0 && (
            <DropdownMenu>
              <DropdownMenuTrigger asChild><Button type="button" variant="ghost" size="sm" className="-ml-2"><Plus />Add ecosystem</Button></DropdownMenuTrigger>
              <DropdownMenuContent align="start">
                {free.map((k) => <DropdownMenuItem key={k} onSelect={() => setExtra([...extra, k])}>{k}</DropdownMenuItem>)}
              </DropdownMenuContent>
            </DropdownMenu>
          )}
        </div>
      </PanelSection>
      {env?.used_by && (
        <PanelSection title={`Used by${env.used_by.length ? ` (${env.used_by.length})` : ""}`}>
          {!env.used_by.length ? <div className="text-sm text-muted-foreground">Nothing</div> : (
            <ul className="space-y-1.5 text-sm">
              {env.used_by.map((u) => <li key={`${u.kind}:${u.id}`}>{u.kind === "channel" ? <ChannelLink id={u.id} name={u.name} kind={u.platform_kind} /> : u.name}</li>)}
            </ul>
          )}
        </PanelSection>
      )}
      {!locked && dirty && (
        <div className="sticky bottom-0 mt-auto flex justify-end gap-2 border-t bg-popover px-5 py-3">
          {env && <Button type="button" variant="ghost" disabled={busy} onClick={discard}>Discard</Button>}
          <Button type="submit" disabled={busy || !name.trim()}>{env ? "Save" : "Create"}</Button>
        </div>
      )}
    </form>
  )
}
