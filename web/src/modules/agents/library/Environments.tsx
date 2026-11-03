import { Archive, Plus } from "lucide-react"
import { useState } from "react"

import { Drawer } from "@/components/Drawer"
import { QueryState } from "@/components/QueryState"
import { Page } from "@/components/shell/Page"
import { Button } from "@/components/ui/button"
import { Checkbox } from "@/components/ui/checkbox"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table"
import { Textarea } from "@/components/ui/textarea"
import { ToggleGroup, ToggleGroupItem } from "@/components/ui/toggle-group"
import { rowProps, useParam } from "@/lib/filters"
import { useConfirm } from "@/lib/confirm"
import { plural } from "@/lib/format"
import { Field, PanelSection, Tag } from "@/modules/admin/shared"
import { ECOSYSTEMS, type EnvironmentDoc, type EnvironmentInput } from "../types"
import { useEnvironments, useEnvironmentWrites } from "./api"
import { LIBRARY_TABS } from "./tabs"

const count = (e: EnvironmentDoc) => Object.values(e.packages).reduce((n, p) => n + (p?.length ?? 0), 0)
const lines = (text: string) => [...new Set(text.split("\n").map((x) => x.trim()).filter(Boolean))]
const NETWORK = { limited: "Limited", unrestricted: "Open" }

export function Environments() {
  const query = useEnvironments()
  const [, setEnv] = useParam("env")
  const rows = query.data ?? []
  return (
    <Page
      crumbs={[{ label: "Agents", to: "/agents" }, { label: "Environments" }]}
      tabs={LIBRARY_TABS}
      flush
      actions={<Button size="sm" onClick={() => setEnv("new")}><Plus />New environment</Button>}
    >
      <QueryState query={query} className="px-6" isEmpty={() => !rows.length} empty="No environments">{() => (
        <Table>
          <TableHeader>
            <TableRow className="hover:bg-transparent">
              <TableHead className="pl-4 lg:pl-6">Name</TableHead>
              <TableHead>Network</TableHead>
              <TableHead className="pr-4 text-right lg:pr-6">Packages</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {rows.map((e) => (
              <TableRow key={e.id} {...rowProps(() => setEnv(e.id))} className="cursor-pointer">
                <TableCell className="pl-4 lg:pl-6"><span className="flex items-center gap-2"><span className="font-medium">{e.name}</span>{e.managed && <Tag>Built in</Tag>}</span></TableCell>
                <TableCell className="text-muted-foreground">{e.cloud ? NETWORK[e.network] : "Self-hosted"}</TableCell>
                <TableCell className="pr-4 text-right text-muted-foreground lg:pr-6">{count(e) || "–"}</TableCell>
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
  const [name, setName] = useState(env?.name ?? "")
  const [description, setDescription] = useState(env?.description ?? "")
  const [network, setNetwork] = useState<EnvironmentInput["network"]>(env?.network ?? "limited")
  const [hosts, setHosts] = useState((env?.allowed_hosts ?? []).join("\n"))
  const [managers, setManagers] = useState(env?.allow_package_managers ?? true)
  const [mcp, setMcp] = useState(env?.allow_mcp_servers ?? true)
  const [packages, setPackages] = useState<Record<string, string>>(Object.fromEntries(ECOSYSTEMS.map((k) => [k, (env?.packages[k] ?? []).join("\n")])))
  if (missing) return <div className="p-5 text-sm text-muted-foreground">Not found</div>
  const locked = env?.managed === true || (env !== null && !env.cloud)
  const spec: EnvironmentInput = {
    ...blank, name: name.trim(), description: description.trim(), network, allowed_hosts: lines(hosts),
    allow_package_managers: managers, allow_mcp_servers: mcp,
    packages: Object.fromEntries(ECOSYSTEMS.map((k) => [k, lines(packages[k] ?? "")]).filter(([, v]) => (v as string[]).length)),
  }
  const busy = create.isPending || save.isPending || archive.isPending
  const go = () => env
    ? save.mutate({ id: env.id, spec, expected_updated_at: env.updated_at })
    : create.mutate(spec, { onSuccess: (made) => setEnv(made.id) })
  const retire = async () => {
    if (!env || !(await confirm({ title: `Archive ${env.name}?`, action: "Archive", destructive: true }))) return
    archive.mutate(env.id, { onSuccess: () => setEnv(null) })
  }
  return (
    <form onSubmit={(e) => { e.preventDefault(); if (!locked && name.trim()) go() }}>
      <div className="flex items-center gap-2 px-5 py-4">
        <h2 className="text-lg font-semibold">{env ? env.name : "New environment"}</h2>
        {env?.managed && <Tag>Built in</Tag>}
        {env && !locked && <Button type="button" variant="outline" size="sm" className="ml-auto" disabled={busy} onClick={() => void retire()}><Archive />Archive</Button>}
      </div>
      <PanelSection title="Details">
        <div className="space-y-3">
          {!env && <Field label="Name"><Input value={name} onChange={(e) => setName(e.target.value)} className="h-8" autoFocus /></Field>}
          <Field label="Description"><Input value={description} onChange={(e) => setDescription(e.target.value)} className="h-8" disabled={locked} /></Field>
        </div>
      </PanelSection>
      <PanelSection title="Network">
        <div className="space-y-3">
          <ToggleGroup type="single" value={network} onValueChange={(v) => v && setNetwork(v as typeof network)} variant="segmented" size="sm" disabled={locked}>
            <ToggleGroupItem value="limited" className="px-3 text-xs">Limited</ToggleGroupItem>
            <ToggleGroupItem value="unrestricted" className="px-3 text-xs">Open</ToggleGroupItem>
          </ToggleGroup>
          {network === "limited" && (
            <>
              <Field label="Allowed hosts"><Textarea value={hosts} onChange={(e) => setHosts(e.target.value)} rows={3} className="font-mono text-xs" disabled={locked} /></Field>
              <label className="flex items-center gap-2 text-sm"><Checkbox checked={managers} onCheckedChange={(c) => setManagers(c === true)} disabled={locked} />Package managers</label>
              <label className="flex items-center gap-2 text-sm"><Checkbox checked={mcp} onCheckedChange={(c) => setMcp(c === true)} disabled={locked} />The agent's own servers</label>
            </>
          )}
        </div>
      </PanelSection>
      <PanelSection title="Packages">
        <div className="space-y-3">
          {ECOSYSTEMS.map((k) => (
            <div key={k} className="space-y-1.5">
              <Label htmlFor={`pk-${k}`}>{k}</Label>
              <Textarea id={`pk-${k}`} value={packages[k] ?? ""} onChange={(e) => setPackages({ ...packages, [k]: e.target.value })} rows={2} className="font-mono text-xs" disabled={locked} />
            </div>
          ))}
          {env && <div className="text-xs text-muted-foreground">{plural(count(env), "package")} now</div>}
        </div>
      </PanelSection>
      {!locked && (
        <div className="border-t px-5 py-3">
          <Button type="submit" disabled={busy || !name.trim()}>{env ? "Save" : "Create"}</Button>
        </div>
      )}
    </form>
  )
}
