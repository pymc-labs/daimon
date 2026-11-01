import { Database, MessageSquare, Monitor, Moon, Plus, Sun } from "lucide-react"
import { useDeferredValue, useState } from "react"
import { useNavigate } from "react-router"

import { APP_ICONS } from "@/components/brand"
import { AgentAvatar, Kbd } from "@/components/common"
import {
  Command, CommandDialog, CommandEmpty, CommandGroup, CommandInput, CommandItem, CommandList,
} from "@/components/ui/command"
import { useChatAgents, useMe, useSessions } from "@/lib/hooks"
import { useTheme } from "@/lib/theme"
import { actionsFor, navFor } from "@/modules/registry"

type Row = { key: string; label: string; icon: React.ReactNode; run: () => void; hint?: string }

export function CommandPalette({ open, onOpenChange }: { open: boolean; onOpenChange: (o: boolean) => void }) {
  const [q, setQ] = useState("")
  const dq = useDeferredValue(q.trim())
  const navigate = useNavigate()
  const { data: me } = useMe()
  const { data: sessions } = useSessions()
  const { data: agents } = useChatAgents()
  const { setTheme } = useTheme()
  if (!me) return null
  const go = (fn: () => void) => () => { onOpenChange(false); setQ(""); fn() }
  const to = (path: string) => go(() => navigate(path))
  const match = (s: string) => !dq || s.toLowerCase().includes(dq.toLowerCase())
  const pick = (rows: Row[], max = 6) => rows.filter((r) => match(r.label)).slice(0, dq ? max : 4)

  const groups: [string, Row[]][] = [
    ["Actions", pick([
      { key: "new", label: "New chat", icon: <Plus />, run: to("/chat") },
      ...actionsFor(me).map((a) => ({ key: `a-${a.to}`, label: a.label, icon: <a.icon />, run: to(a.to) })),
      { key: "light", label: "Theme: Light", icon: <Sun />, run: go(() => setTheme("light")) },
      { key: "dark", label: "Theme: Dark", icon: <Moon />, run: go(() => setTheme("dark")) },
      { key: "system", label: "Theme: System", icon: <Monitor />, run: go(() => setTheme("system")) },
    ], 8)],
    ["Pages", pick([
      ...navFor(me).map((n) => ({ key: `p-${n.to}`, label: n.label, icon: <n.icon />, run: to(n.to), hint: n.g && `G ${n.g.toUpperCase()}` })),
      ...me.apps.flatMap((a) => a.nav.map((n) => {
        const Icon = APP_ICONS[n.icon] ?? Database
        return { key: `app-${a.key}${n.path}`, label: `${a.name}: ${n.label}`, icon: <Icon />, run: to(`/apps/${a.key}${n.path}`) }
      })),
    ], 8)],
    ["Chat with", pick((agents ?? []).map((a) => (
      { key: `ag-${a.id}`, label: a.name, icon: <AgentAvatar src={a.avatar} name={a.name} className="size-4 rounded" />, run: to(`/chat?agent=${a.id}`) })))],
    ["Chats", pick((sessions ?? []).map((s) => (
      { key: `c-${s.id}`, label: s.title, icon: <MessageSquare />, run: to(`/chat/${s.id}`) })))],
  ]
  const empty = groups.every(([, r]) => !r.length)

  return (
    <CommandDialog open={open} onOpenChange={(o) => { onOpenChange(o); if (!o) setQ("") }} className="sm:max-w-xl" title="Search">
      <Command shouldFilter={false} loop>
        <CommandInput value={q} onValueChange={setQ} placeholder="Search or jump to…" />
        <CommandList className="max-h-[420px]">
          {empty && <CommandEmpty>No results</CommandEmpty>}
          {groups.map(([label, rows]) => rows.length > 0 && (
            <CommandGroup key={label} heading={label}>
              {rows.map((r) => (
                <CommandItem key={r.key} value={r.key} onSelect={r.run}>
                  {r.icon}
                  <span className="truncate">{r.label}</span>
                  {r.hint && <span className="ml-auto flex gap-1">{r.hint.split(" ").map((k) => <Kbd key={k}>{k}</Kbd>)}</span>}
                </CommandItem>
              ))}
            </CommandGroup>
          ))}
        </CommandList>
      </Command>
    </CommandDialog>
  )
}
