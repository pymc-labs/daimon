import { useEffect, useRef, useState } from "react"
import { useLocation, useNavigate } from "react-router"

import { useShell } from "@/components/shell/shell-context"
import { api } from "@/lib/api"
import { useMe } from "@/lib/hooks"
import { useTheme } from "@/lib/theme"
import type { EmbeddedApp } from "@/lib/types"
import { cn } from "@/lib/utils"
import { navFor } from "@/modules/registry"

type Msg = { embed?: number; type: string; path?: string; title?: string; key?: string; mod?: boolean }

const embedSrc = (app: EmbeddedApp, path: string) => `${app.origin}${path}${path.includes("?") ? "&" : "?"}embed=1`

/** The app's access token for the signed-in person; null when the server has none for this app. */
const tokenFor = (key: string) =>
  api.post<{ access_token: string }>(`/api/apps/${key}/token`).then((r) => r.access_token, () => null)

/** Bridge to one embedded app frame: exact-origin postMessage both ways; answers the frame's token requests. */
function useBridge(app: EmbeddedApp, frame: React.RefObject<HTMLIFrameElement | null>, on: (m: Msg) => void) {
  const { resolved } = useTheme()
  const send = (m: Record<string, unknown>) => frame.current?.contentWindow?.postMessage({ embed: 1, ...m }, app.origin)
  useEffect(() => {
    const h = (e: MessageEvent) => {
      if (e.origin !== app.origin || e.source !== frame.current?.contentWindow || e.data?.embed !== 1) return
      if (e.data.type === "token") tokenFor(app.key).then((token) => send({ type: "token", token }))
      else on(e.data)
    }
    window.addEventListener("message", h)
    return () => window.removeEventListener("message", h)
  })
  useEffect(() => { send({ type: "theme", theme: resolved }) })
  return send
}

/** Embedded app pages under /apps/:app/*. Frames stay mounted once opened, so their state survives switching modules. */
export function AppFrames() {
  const { data: me } = useMe()
  const { pathname, search } = useLocation()
  const m = pathname.match(/^\/apps\/([^/]+)(\/.*)?$/)
  const sub = `${m?.[2] ?? "/"}${search}`
  const [opened, setOpened] = useState<Record<string, string>>({})
  const active = m?.[1]
  useEffect(() => {
    if (active && !opened[active]) setOpened((o) => ({ ...o, [active]: sub }))
  }, [active, sub, opened])
  if (!me) return null
  return (
    <>
      {me.apps.filter((a) => opened[a.key]).map((a) => (
        <Frame key={a.key} app={a} start={opened[a.key]!} path={a.key === active ? sub : null} />
      ))}
    </>
  )
}

function Frame({ app, start, path }: { app: EmbeddedApp; start: string; path: string | null }) {
  const ref = useRef<HTMLIFrameElement>(null)
  const at = useRef(start)
  const navigate = useNavigate()
  const { openNav, openPalette, openShortcuts } = useShell()
  const { data: me } = useMe()
  const send = useBridge(app, ref, (msg) => {
    if (msg.type === "navigate" && msg.path) {
      at.current = msg.path
      if (path !== null && msg.path !== path) navigate(`/apps/${app.key}${msg.path}`, { replace: true })
    } else if (msg.type === "title" && path !== null) document.title = msg.title ? `${msg.title} - Daimon` : "Daimon"
    else if (msg.type === "open-nav") openNav()
    else if (msg.type === "keys") {
      if (msg.key === "k" && msg.mod) openPalette()
      else if (msg.key === "?") openShortcuts()
      else if (msg.key?.startsWith("g ") && me) {
        const to = navFor(me).find((n) => n.g === msg.key!.slice(2))?.to
        if (to) navigate(to)
      }
    }
  })
  // Sidebar clicks inside Daimon move the frame without reloading it.
  useEffect(() => {
    if (path !== null && path !== at.current) { at.current = path; send({ type: "navigate", path }) }
  })
  return (
    <iframe
      ref={ref}
      title={app.name}
      src={embedSrc(app, start)}
      className={cn("min-w-0 flex-1 border-0 bg-background", path === null && "hidden")}
    />
  )
}

/** An app's embed `path` (already filled in by the server) in a frame for a right drawer. */
export function AppPanel({ app, path, className }: { app: EmbeddedApp; path: string; className?: string }) {
  const ref = useRef<HTMLIFrameElement>(null)
  useBridge(app, ref, () => {})
  return <iframe ref={ref} title={app.name} src={embedSrc(app, path)} className={cn("size-full border-0 bg-background", className)} />
}
