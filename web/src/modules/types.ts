import type { LucideIcon } from "lucide-react"

import type { Capability, ModuleKey } from "@/lib/types"

/** A sidebar entry. `g` is the go-to key (G then key). */
export type NavItem = { to: string; label: string; icon: LucideIcon; cap?: Capability; g?: string; end?: boolean }

/** A module: its nav, its routes (mounted behind a gate on `key`) and its palette actions. */
export type ModuleDef = {
  key: ModuleKey
  nav: NavItem[]
  routes: React.ReactNode
  actions?: { label: string; to: string; icon: LucideIcon; cap?: Capability }[]
}
