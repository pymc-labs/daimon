import { Suspense, useState } from "react"
import { Outlet, useLocation, useNavigate } from "react-router"

import { Rows } from "@/components/QueryState"
import { Sheet, SheetContent, SheetTitle } from "@/components/ui/sheet"
import { useKey, useMe } from "@/lib/hooks"
import { AppFrames } from "@/modules/apps/AppFrame"
import { navFor } from "@/modules/registry"
import { CommandPalette } from "./CommandPalette"
import { ShellCtx } from "./shell-context"
import { ShortcutsDialog } from "./ShortcutsDialog"
import { Sidebar } from "./Sidebar"

export function AppShell() {
  const { data: me } = useMe()
  const [nav, setNav] = useState(false)
  const [palette, setPalette] = useState(false)
  const [shortcuts, setShortcuts] = useState(false)
  const navigate = useNavigate()
  const loc = useLocation()
  const [g, setG] = useState(0)

  useKey((e) => {
    const mod = e.metaKey || e.ctrlKey
    if (mod && e.key.toLowerCase() === "k") { e.preventDefault(); setPalette((v) => !v) }
    else if (mod && e.key.toLowerCase() === "o") { e.preventDefault(); navigate("/chat"); setTimeout(() => document.getElementById("composer")?.focus(), 50) }
  }, [navigate], true)
  useKey((e) => {
    if (e.metaKey || e.ctrlKey || e.altKey) return
    if (e.key === "?") setShortcuts(true)
    else if (e.key === "/") {
      e.preventDefault()
      const el = document.querySelector<HTMLInputElement>("[data-search]")
      if (el) el.focus()
      else setPalette(true)
    }
    else if (e.key === "g") setG(Date.now())
    else if (me && Date.now() - g < 1200) {
      const to = navFor(me).find((n) => n.g === e.key)?.to
      if (to) { navigate(to); setG(0) }
    }
  }, [g, navigate, me])

  return (
    <ShellCtx.Provider value={{ openNav: () => setNav(true), openPalette: () => setPalette(true), openShortcuts: () => setShortcuts(true) }}>
      <div className="flex h-dvh overflow-hidden">
        <aside className="hidden w-60 shrink-0 border-r bg-muted lg:flex">
          <Sidebar />
        </aside>
        <Sheet open={nav} onOpenChange={setNav}>
          <SheetContent side="left" className="w-64 bg-muted p-0" showCloseButton={false} onClick={(e) => (e.target as HTMLElement).closest("a") && setNav(false)}>
            <SheetTitle className="sr-only">Navigation</SheetTitle>
            <Sidebar />
          </SheetContent>
        </Sheet>
        {!loc.pathname.startsWith("/apps/") && (
          <div key={loc.pathname.split("/").slice(1, 3).join("/")} className="flex min-w-0 flex-1">
            <Suspense fallback={<Rows className="w-full p-6" />}><Outlet /></Suspense>
          </div>
        )}
        <AppFrames />
      </div>
      <CommandPalette open={palette} onOpenChange={setPalette} />
      <ShortcutsDialog open={shortcuts} onOpenChange={setShortcuts} />
    </ShellCtx.Provider>
  )
}
