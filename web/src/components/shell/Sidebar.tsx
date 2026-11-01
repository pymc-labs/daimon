import { useMutation, useQueryClient } from "@tanstack/react-query"
import {
  Check, ChevronsUpDown, Database, Keyboard, Link2, LogOut, Monitor, Moon, MoreHorizontal, Plus, Search, Sun, UserRound,
} from "lucide-react"
import { useMemo, useState } from "react"
import { Link, NavLink, useMatch, useNavigate } from "react-router"
import { toast } from "sonner"

import { AppIcon, BrandIcon, Logo } from "@/components/brand"
import { APP_ICONS } from "@/lib/app-icons"
import { AgentAvatar, Avatar, Kbd } from "@/components/common"
import { useConfirm } from "@/lib/confirm"
import { CreditMeter } from "@/components/CreditMeter"
import { LoadMore, QueryState, Rows } from "@/components/QueryState"
import {
  DropdownMenu, DropdownMenuContent, DropdownMenuItem, DropdownMenuLabel, DropdownMenuSeparator, DropdownMenuSub,
  DropdownMenuSubContent, DropdownMenuSubTrigger, DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu"
import { api } from "@/lib/api"
import { useMe, useProviders, useSessions } from "@/lib/hooks"
import { useTheme } from "@/lib/theme-context"
import type { ChatSession, Me, Person } from "@/lib/types"
import { cn, MOD } from "@/lib/utils"
import { evict } from "@/modules/chat/turns"
import { navFor } from "@/modules/registry"
import { signOut } from "./auth"
import { ShellCtx } from "./shell-context"
import { RenameDialog } from "./RenameDialog"

const itemCls = (active: boolean) => cn(
  "flex h-8 items-center gap-2 rounded-md px-2.5 text-sm",
  active ? "bg-accent font-medium text-foreground" : "text-muted-foreground hover:bg-accent hover:text-foreground",
)

export function Sidebar() {
  const { data: me } = useMe()
  const chatOpen = !!useMatch("/chat/*")
  if (!me) return <div className="h-full w-full" />
  return (
    <div className="flex h-full w-full flex-col">
      <div className="flex h-12 items-center gap-1 px-3">
        <WorkspaceSwitcher me={me} />
        <PaletteButton />
      </div>
      <div className="px-3 pb-2">
        <Link
          to="/chat"
          className="flex h-8 items-center gap-2 rounded-md border bg-background px-2.5 text-sm font-medium shadow-[0_1px_2px_rgb(0_0_0/0.04)] hover:bg-accent"
        >
          <Plus className="size-4" />
          New chat
          <span className="ml-auto flex gap-0.5"><Kbd>{MOD}</Kbd><Kbd>O</Kbd></span>
        </Link>
      </div>
      <nav className="space-y-0.5 px-3">
        {navFor(me).map(({ to, label, icon: Icon, end }) => (
          <NavLink key={to} to={to} end={end} className={({ isActive }) => itemCls(isActive || (to === "/chat" && chatOpen))}>
            <Icon className="size-4" strokeWidth={1.75} />
            {label}
          </NavLink>
        ))}
      </nav>
      {me.apps.map((app) => (
        <nav key={app.key} className="mt-4 space-y-0.5 px-3" aria-label={app.name}>
          <div className="flex items-center gap-1.5 px-2.5 pb-1 text-xs font-medium text-muted-foreground">
            <AppIcon icon={app.icon} className="size-3.5" />
            {app.name}
          </div>
          {app.nav.map((n) => {
            const Icon = APP_ICONS[n.icon] ?? Database
            return (
              <NavLink key={n.path} to={`/apps/${app.key}${n.path}`} className={({ isActive }) => itemCls(isActive)}>
                <Icon className="size-4" strokeWidth={1.75} />
                {n.label}
              </NavLink>
            )
          })}
        </nav>
      ))}
      <RecentChats />
      {me.credit && (
        <CreditLink me={me}>
          <CreditMeter meter={me.credit} compact />
        </CreditLink>
      )}
      <UserMenu />
    </div>
  )
}

function CreditLink({ me, children }: { me: Me; children: React.ReactNode }) {
  const cls = "mx-3 block rounded-md px-2.5 py-2"
  if (!me.modules.includes("credits")) return <div className={cls}>{children}</div>
  return <Link to="/credits" className={cn(cls, "hover:bg-accent")} aria-label="Credits">{children}</Link>
}

function WorkspaceSwitcher({ me }: { me: Me }) {
  const qc = useQueryClient()
  const navigate = useNavigate()
  const switchTo = useMutation({
    mutationFn: (id: string) => api.post("/api/workspaces/switch", { id }),
    onSuccess: () => { qc.clear(); navigate("/chat") },
  })
  const pick = (id: string) => { if (id !== me.workspace.id) switchTo.mutate(id) }
  const label = (
    <>
      <Logo className="size-6" />
      <span className="truncate text-sm font-semibold">{me.workspace.name}</span>
    </>
  )
  if (me.workspaces.length < 2) return <div className="flex min-w-0 items-center gap-2 px-1">{label}</div>
  return (
    <DropdownMenu>
      <DropdownMenuTrigger className="flex h-8 min-w-0 items-center gap-2 rounded-md px-1 hover:bg-accent data-[state=open]:bg-accent">
        {label}
        <ChevronsUpDown className="size-3.5 shrink-0 text-muted-foreground" />
      </DropdownMenuTrigger>
      <DropdownMenuContent align="start" className="w-60">
        <DropdownMenuLabel className="text-xs text-muted-foreground">Workspaces</DropdownMenuLabel>
        {me.workspaces.map((w) => (
          <DropdownMenuItem key={w.id} onSelect={() => pick(w.id)}>
            <BrandIcon brand={w.platform} />
            <span className="truncate">{w.name}</span>
            {w.id === me.workspace.id && <Check className="ml-auto" />}
          </DropdownMenuItem>
        ))}
      </DropdownMenuContent>
    </DropdownMenu>
  )
}

function PaletteButton() {
  return (
    <ShellCtx.Consumer>
      {({ openPalette }) => (
        <button onClick={openPalette} className="ml-auto shrink-0 rounded-md p-1.5 text-muted-foreground hover:bg-accent hover:text-foreground" aria-label={`Search (${MOD}K)`}>
          <Search className="size-4" />
        </button>
      )}
    </ShellCtx.Consumer>
  )
}

function group(s: ChatSession) {
  const d = new Date(s.updated_at)
  const today = new Date()
  today.setHours(0, 0, 0, 0)
  const days = (today.getTime() - d.getTime()) / 86400000
  if (days <= 0) return "Today"
  if (days <= 1) return "Yesterday"
  if (days <= 7) return "Previous 7 days"
  return "Older"
}

function RecentChats() {
  const sessions = useSessions()
  const { data } = sessions
  const id = useMatch("/chat/:id")?.params.id
  const navigate = useNavigate()
  const qc = useQueryClient()
  const [renaming, setRenaming] = useState<ChatSession | null>(null)
  const groups = useMemo(() => {
    const out: [string, ChatSession[]][] = []
    for (const s of data ?? []) {
      const g = group(s)
      const last = out[out.length - 1]
      if (last && last[0] === g) last[1].push(s)
      else out.push([g, [s]])
    }
    return out
  }, [data])
  const confirm = useConfirm()
  const del = useMutation({
    mutationFn: (s: ChatSession) => api.del(`/api/chat/sessions/${s.id}`),
    onSuccess: (_, s) => {
      evict(s.id)
      qc.invalidateQueries({ queryKey: ["chat", "sessions"] })
      if (s.id === id) navigate("/chat")
      toast("Chat deleted")
    },
  })
  return (
    <div className="mt-4 min-h-0 flex-1 overflow-y-auto px-3 pb-3">
      {!data && <QueryState query={sessions} skeleton={<Rows n={4} />}>{() => null}</QueryState>}
      {groups.map(([label, list]) => (
        <div key={label} className="mb-3">
          <div className="px-2.5 pb-1 text-xs font-medium text-muted-foreground">{label}</div>
          {list.map((s) => (
            <div key={s.id} className="group relative">
              <NavLink
                to={`/chat/${s.id}`}
                className={({ isActive }) => cn(
                  "flex h-8 items-center gap-2 rounded-md pr-8 pl-2.5 text-sm",
                  isActive ? "bg-accent text-foreground" : "text-muted-foreground hover:bg-accent hover:text-foreground",
                )}
              >
                <AgentAvatar src={s.agent.avatar} name={s.agent.name} className="size-4 rounded" />
                <span className="truncate">{s.title}</span>
                {s.running && <span className="ml-auto size-1.5 shrink-0 animate-pulse rounded-full bg-link" role="img" aria-label="Running" />}
              </NavLink>
              <DropdownMenu>
                <DropdownMenuTrigger
                  className="absolute top-1 right-1 rounded p-1 text-muted-foreground opacity-0 group-hover:opacity-100 hover:bg-background focus-visible:opacity-100 data-[state=open]:opacity-100"
                  aria-label="Chat actions"
                >
                  <MoreHorizontal className="size-4" />
                </DropdownMenuTrigger>
                <DropdownMenuContent align="start">
                  <DropdownMenuItem onSelect={() => setRenaming(s)}>Rename</DropdownMenuItem>
                  <DropdownMenuItem variant="destructive" onSelect={async () => {
                    if (await confirm({ title: "Delete chat?", body: s.title, action: "Delete", destructive: true })) del.mutate(s)
                  }}>Delete</DropdownMenuItem>
                </DropdownMenuContent>
              </DropdownMenu>
            </div>
          ))}
        </div>
      ))}
      <LoadMore list={sessions} className="py-1" />
      <RenameDialog session={renaming} onClose={() => setRenaming(null)} />
    </div>
  )
}

const ROLE: Record<string, string> = { owner: "Owner", admin: "Admin", member: "", visitor: "Visitor" }
/** Local switching between seeded people exists only in the mock build. */
const MOCK = import.meta.env.MODE === "mock"

function UserMenu() {
  const { data: me } = useMe()
  const { theme, setTheme } = useTheme()
  const qc = useQueryClient()
  const [users, setUsers] = useState<Person[] | null>(null)
  const providers = useProviders()
  const link = useMutation({
    mutationFn: (provider: string) => api.post<{ url: string }>(`/auth/${provider}/link`, { next: "/chat" }),
    onSuccess: ({ url }) => location.assign(url),
  })
  if (!me) return <div className="h-14 border-t" />
  const switchTo = async (email: string) => {
    await api.post("/api/dev/switch", { email })
    qc.clear()
    location.assign("/chat")
  }
  const linked = new Set(me.identities.map((i) => i.provider))
  const linkable = me.role === "visitor" ? [] : (providers.data ?? []).filter((p) => p.id !== "oidc")
  return (
    <ShellCtx.Consumer>
      {({ openShortcuts }) => (
        <DropdownMenu onOpenChange={(o) => o && MOCK && !users && api.get<Person[]>("/api/dev/users").then(setUsers)}>
          <DropdownMenuTrigger className="m-2 mt-0 flex items-center gap-2 rounded-md p-2 text-left hover:bg-accent data-[state=open]:bg-accent">
            <Avatar name={me.name} />
            <span className="min-w-0 flex-1">
              <span className="block truncate text-sm font-medium">{me.name}</span>
              <span className="block truncate text-xs text-muted-foreground">{me.email}</span>
            </span>
            <ChevronsUpDown className="size-4 text-muted-foreground" />
          </DropdownMenuTrigger>
          <DropdownMenuContent side="top" align="start" className="w-56">
            <DropdownMenuSub>
              <DropdownMenuSubTrigger>
                {theme === "dark" ? <Moon /> : theme === "light" ? <Sun /> : <Monitor />}
                Theme
              </DropdownMenuSubTrigger>
              <DropdownMenuSubContent>
                {(["system", "light", "dark"] as const).map((t) => (
                  <DropdownMenuItem key={t} onSelect={() => setTheme(t)}>
                    {t[0]!.toUpperCase() + t.slice(1)}
                    {theme === t && <Check className="ml-auto" />}
                  </DropdownMenuItem>
                ))}
              </DropdownMenuSubContent>
            </DropdownMenuSub>
            <DropdownMenuItem onSelect={openShortcuts}><Keyboard />Keyboard shortcuts<span className="ml-auto"><Kbd>?</Kbd></span></DropdownMenuItem>
            {MOCK && (
              <DropdownMenuSub>
                <DropdownMenuSubTrigger><UserRound />Switch user</DropdownMenuSubTrigger>
                <DropdownMenuSubContent className="max-h-80 overflow-y-auto">
                  <DropdownMenuLabel className="text-xs text-muted-foreground">Local only</DropdownMenuLabel>
                  {(users ?? []).map((u) => (
                    <DropdownMenuItem key={u.id} onSelect={() => switchTo(u.email)}>
                      <span className="truncate">{u.name}</span>
                      <span className="ml-auto pl-3 text-xs text-muted-foreground">{ROLE[u.role]}</span>
                      {u.email === me.email && <Check />}
                    </DropdownMenuItem>
                  ))}
                </DropdownMenuSubContent>
              </DropdownMenuSub>
            )}
            {linkable.length > 0 && (
              <DropdownMenuSub>
                <DropdownMenuSubTrigger><Link2 />Link chat account</DropdownMenuSubTrigger>
                <DropdownMenuSubContent>
                  {linkable.map((p) => (
                    <DropdownMenuItem key={p.id} disabled={linked.has(p.id) || link.isPending} onSelect={() => link.mutate(p.id)}>
                      <BrandIcon brand={p.id} icon={p.icon} />{p.label}
                      {linked.has(p.id) && <Check className="ml-auto" />}
                    </DropdownMenuItem>
                  ))}
                </DropdownMenuSubContent>
              </DropdownMenuSub>
            )}
            <DropdownMenuSeparator />
            <DropdownMenuItem onSelect={signOut}><LogOut />Sign out</DropdownMenuItem>
          </DropdownMenuContent>
        </DropdownMenu>
      )}
    </ShellCtx.Consumer>
  )
}
