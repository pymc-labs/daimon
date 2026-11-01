import type { Capability, Me } from "@/lib/types"
import { admin } from "./admin"
import { agents } from "./agents"
import { chat } from "./chat"
import { credits } from "./credits"
import type { ModuleDef, NavItem } from "./types"

export const MODULES: ModuleDef[] = [chat, agents, credits, admin]

const allowed = (me: Me, cap?: Capability) => !cap || me.capabilities.includes(cap)

/** Modules the server enabled for this person, with nav filtered by capability. */
export function enabled(me: Me) {
  return MODULES.filter((m) => me.modules.includes(m.key))
}
export const navFor = (me: Me): NavItem[] => enabled(me).flatMap((m) => m.nav.filter((n) => allowed(me, n.cap)))
export const actionsFor = (me: Me) => enabled(me).flatMap((m) => (m.actions ?? []).filter((a) => allowed(me, a.cap)))
