import type { Capability, Me } from "@/lib/types"
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

/** The sidebar: the module pages this person can use. */
export const sidebarFor = (me: Me): NavItem[] => navFor(me)

/** Where a path this person may not open leads: the first page they can use. */
export const homeFor = (me: Me) => navFor(me)[0]?.to ?? "/chat"

/** The page to send the person to instead of `path`, or null when they may open it. */
export function redirectFor(me: Me, path: string): string | null {
  for (const m of MODULES) {
    if (m.gates?.some((g) => within(path, g.to) && !allowed(me, g.cap))) return homeFor(me)
    const item = m.nav.find((n) => within(path, n.to))
    if (item && !usable(me, item)) return homeFor(me)
  }
  return null
}
