import { ExternalLink, RefreshCw, Trash2 } from "lucide-react"
import { useState } from "react"
import { Link } from "react-router"
import { toast } from "sonner"
import { plural } from "@/lib/format"
import { Mono, StatusDot } from "@/components/common"
import { AppIcon } from "@/components/brand"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select"
import { ToggleGroup, ToggleGroupItem } from "@/components/ui/toggle-group"
import { useMe } from "@/lib/hooks"
import { cn } from "@/lib/utils"
import { Dot, Locked } from "../bits"
import { RULE } from "../lib"
import type { McpServer, ToolRule } from "../types"
import { Field, Section, type Props } from "./parts"
import { useLocal } from "./useLocal"

const AUTH = { oauth: "OAuth", token: "Token", none: "None" }

export function ServerEditor({ agent, ro, update, select, server: s, changed }: Props & { server: McpServer }) {
  const set = (fn: (x: McpServer) => void, d = 0) => update((c) => fn(c.mcp_servers.find((x) => x.key === s.key)!), d)
  const [url, setUrl] = useLocal(s.url, (v) => set((x) => { x.url = v }, 600))
  const hidden = !agent.can_view_details
  const writes = s.tools.filter((t) => t.effect === "write")
  const app = useMe().data?.apps.find((a) => a.key === s.app)
  const managed = !!s.app
  return (
    <>
      <div className="flex items-center gap-3 px-5 py-4">
        <span className="flex size-9 items-center justify-center rounded-lg border bg-muted">
          {app ? <AppIcon icon={app.icon} className="size-5" /> : <span className="text-sm font-semibold text-muted-foreground">{s.name[0]}</span>}
        </span>
        <div className="min-w-0">
          <div className="text-base font-semibold">{s.name}</div>
          <div className="text-xs text-muted-foreground">{plural(s.tools.length, "tool")}, {writes.length} change data</div>
        </div>
      </div>
      <Section title="Connection">
        <div className="space-y-3">
          {hidden ? <Locked who={agent.owner.name} /> : (
            <>
              <Field label="URL" changed={changed.has(`mcp:${s.key}/url`)}>
                <Input value={url} disabled={ro || managed} onChange={(e) => setUrl(e.target.value)} className="h-8 font-mono text-[12.5px]" />
              </Field>
              <Field label="Sign-in" changed={changed.has(`mcp:${s.key}/auth`)}>
                <Select value={s.auth} disabled={ro || managed} onValueChange={(v) => set((x) => { x.auth = v as McpServer["auth"] })}>
                  <SelectTrigger className="h-8 w-36"><SelectValue /></SelectTrigger>
                  <SelectContent>{Object.entries(AUTH).map(([k, l]) => <SelectItem key={k} value={k}>{l}</SelectItem>)}</SelectContent>
                </Select>
              </Field>
              <Field label="Credential">
                <span className="flex items-center gap-3">
                  {s.credential === "connected" ? <StatusDot status="healthy" label="Connected" />
                    : s.credential === "expired" ? <StatusDot status="failing" label="Expired" /> : <StatusDot status="setup" label="Not signed in" />}
                  {!ro && !managed && (
                    <Button variant="outline" size="xs" className="ml-auto" onClick={() => toast(`Signing in to ${s.name}`)}>
                      <RefreshCw />{s.credential === "connected" ? "Reconnect" : "Sign in"}
                    </Button>
                  )}
                </span>
              </Field>
              {app && (
                <Field label="Managed in">
                  <Button variant="outline" size="xs" className="w-fit" asChild>
                    <Link to={`/apps/${app.key}${app.connections_path ?? ""}`}><AppIcon icon={app.icon} className="size-3.5" />{app.name}<ExternalLink /></Link>
                  </Button>
                </Field>
              )}
            </>
          )}
        </div>
      </Section>
      <Section
        title="Tool rules"
        action={!ro && writes.length > 1 && (
          <Select value="" onValueChange={(v) => set((x) => { for (const t of x.tools) if (t.effect === "write") t.rule = v as ToolRule })}>
            <SelectTrigger size="sm" className="h-7 text-xs"><SelectValue placeholder="All writes" /></SelectTrigger>
            <SelectContent>{(["allow", "ask", "deny"] as const).map((r) => <SelectItem key={r} value={r}>{RULE[r].label}</SelectItem>)}</SelectContent>
          </Select>
        )}
        flush
      >
        <div className="divide-y border-t">
          {s.tools.map((t) => (
            <div key={t.name} className="flex items-center gap-3 px-5 py-2">
              <div className="min-w-0 flex-1">
                <div className="flex items-center gap-1.5">
                  <Mono className="font-medium">{t.name}</Mono>
                  <span className={cn("rounded px-1 text-[10.5px] font-medium", t.effect === "write" ? "bg-warning/10 text-warning" : "bg-muted text-muted-foreground")}>
                    {t.effect === "write" ? "Write" : "Read"}
                  </span>
                  {changed.has(`mcp:${s.key}/${t.name}`) && <Dot />}
                </div>
                <div className="truncate text-xs text-muted-foreground">{t.description}</div>
              </div>
              <ToggleGroup type="single" value={t.rule} disabled={ro} variant="segmented" size="sm"
                onValueChange={(v) => v && set((x) => { x.tools.find((y) => y.name === t.name)!.rule = v as ToolRule })}>
                {(["allow", "ask", "deny"] as const).map((r) => (
                  <ToggleGroupItem key={r} value={r} className={cn("h-6 px-2 text-[11px]", t.rule === r && r === "ask" && "!text-warning", t.rule === r && r === "deny" && "!text-destructive")}>
                    {RULE[r].label}
                  </ToggleGroupItem>
                ))}
              </ToggleGroup>
            </div>
          ))}
        </div>
      </Section>
      {!ro && (
        <div className="mt-auto border-t px-5 py-3">
          <Button variant="ghost" size="sm" className="text-muted-foreground hover:text-destructive"
            onClick={() => { update((c) => { c.mcp_servers = c.mcp_servers.filter((x) => x.key !== s.key) }, 0); select("") }}>
            <Trash2 />Remove server
          </Button>
        </div>
      )}
    </>
  )
}

export function NewServer({ config, update, select }: Props) {
  const [name, setName] = useState("")
  const [url, setUrl] = useState("")
  const [auth, setAuth] = useState<McpServer["auth"]>("oauth")
  const key = name.toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "")
  const ok = key && /^https:\/\/\S+$/.test(url) && !config.mcp_servers.some((s) => s.key === key)
  const add = () => {
    update((c) => {
      c.mcp_servers.push({
        key, name: name.trim(), url, auth, credential: auth === "none" ? "connected" : "missing",
        tools: [
          { name: "search", description: "Search records", effect: "read", rule: "allow" },
          { name: "get", description: "Read one record", effect: "read", rule: "allow" },
          { name: "create", description: "Create a record", effect: "write", rule: "ask" },
          { name: "update", description: "Change a record", effect: "write", rule: "ask" },
        ],
      })
    }, 0)
    select(`mcp:${key}`)
  }
  return (
    <Section>
      <form className="space-y-3" onSubmit={(e) => { e.preventDefault(); if (ok) add() }}>
        <Field label="Name"><Input value={name} onChange={(e) => setName(e.target.value)} autoFocus className="h-8" placeholder="Notion" /></Field>
        <Field label="URL"><Input value={url} onChange={(e) => setUrl(e.target.value)} className="h-8 font-mono text-[12.5px]" placeholder="https://" /></Field>
        <Field label="Sign-in">
          <Select value={auth} onValueChange={(v) => setAuth(v as McpServer["auth"])}>
            <SelectTrigger className="h-8 w-36"><SelectValue /></SelectTrigger>
            <SelectContent>{Object.entries(AUTH).map(([k, l]) => <SelectItem key={k} value={k}>{l}</SelectItem>)}</SelectContent>
          </Select>
        </Field>
        <div className="flex justify-end gap-2 pt-2">
          <Button type="button" variant="ghost" size="sm" onClick={() => select("")}>Cancel</Button>
          <Button type="submit" size="sm" disabled={!ok}>Add server</Button>
        </div>
      </form>
    </Section>
  )
}
