import type { Capability, EmbeddedApp, Me } from "@/lib/types"
import { admin } from "./admin"
import { agents } from "./agents"
import { chat } from "./chat"
import { credits } from "./credits"
import type { ModuleDef, NavItem } from "./types"

export const MODULES: ModuleDef[] = [chat, agents, credits, admin]

const allowed = (me: Me, cap?: Capability) => !cap || me.capabilities.includes(cap)
const usable = (me: Me, n: NavItem) => allowed(me, n.cap) && (n.show?.(me) ?? true)
const within = (path: string, to: string) => path === to || path.startsWith(`${to}/`)

/** Modules the server enabled for this person, with nav filtered by capability. */
export function enabled(me: Me) {
  return MODULES.filter((m) => me.modules.includes(m.key))
}
/** Pages in the module nav this person can use. */
export const navFor = (me: Me): NavItem[] => enabled(me).flatMap((m) => m.nav.filter((n) => usable(me, n)))
export const actionsFor = (me: Me) => enabled(me).flatMap((m) => (m.actions ?? []).filter((a) => allowed(me, a.cap)))

/** One sidebar entry: a module page, or a page of an embedded app (`app` set, icon named by the app). */
export type SideItem = NavItem | { to: string; label: string; app: EmbeddedApp; icon: string; g?: undefined; end?: undefined }
export const isAppItem = (i: SideItem): i is Extract<SideItem, { app: EmbeddedApp }> => "app" in i

/** The sidebar: chat and agents, then embedded apps' pages, then credits and admin. */
export function sidebarFor(me: Me): SideItem[] {
  const own = (keys: string[], invert = false) =>
    navFor(me).filter((n) => keys.includes(moduleOf(n)) !== invert)
  const apps: SideItem[] = me.apps.flatMap((app) => app.nav.map((n) => ({ to: `/apps/${app.key}${n.path}`, label: n.label, app, icon: n.icon })))
  return [...own(["chat", "agents"]), ...apps, ...own(["chat", "agents"], true)]
}
const moduleOf = (n: NavItem) => MODULES.find((m) => m.nav.includes(n))!.key

/** Where a path this person may not open leads: the first page they can use. */
export const homeFor = (me: Me) => navFor(me)[0]?.to ?? "/chat"

/** The page to send the person to instead of `path`, or null when they may open it. */
export function redirectFor(me: Me, path: string): string | null {
  if (path.startsWith("/apps/")) return me.apps.some((a) => path === `/apps/${a.key}` || path.startsWith(`/apps/${a.key}/`)) ? null : homeFor(me)
  for (const m of MODULES) {
    if (m.gates?.some((g) => within(path, g.to) && !allowed(me, g.cap))) return homeFor(me)
    const item = m.nav.find((n) => within(path, n.to))
    if (item && !usable(me, item)) return homeFor(me)
  }
  return null
}
