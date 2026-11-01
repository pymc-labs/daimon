import type { LucideIcon } from "lucide-react"

import type { Capability, Me, ModuleKey } from "@/lib/types"

/** A sidebar entry. `g` is the go-to key (G then key). */
export type NavItem = {
  to: string; label: string; icon: LucideIcon; cap?: Capability; g?: string; end?: boolean
  /** Beyond the capability: whether the page would hold anything for this person. */
  show?: (me: Me) => boolean
}

/** A module: its nav, its routes (mounted behind a gate on `key`) and its palette actions. */
export type ModuleDef = {
  key: ModuleKey
  nav: NavItem[]
  routes: React.ReactNode
  actions?: { label: string; to: string; icon: LucideIcon; cap?: Capability }[]
  /** Pages under the module that need a capability the module's own nav item does not. */
  gates?: { to: string; cap: Capability }[]
}
