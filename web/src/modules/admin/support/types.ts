// `/api/support*` shapes: GET answers the whole page and every PUT answers with it too. See the API contract.
export type Scope = "workspace" | "agent" | "channel"
export type Window = "total" | "monthly"

/** What a limit says, and what a write takes back as the new value. Credits are decimal strings. */
export type LimitValue = {
  /** Credits everyone at the place shares; null for none. */
  shared: string | null
  /** Credits each person may use; for a person's own row, that person's limit. */
  each_person: string | null
  window: Window
  /** Instants; null is open. The end is exclusive. */
  starts_at: string | null
  ends_at: string | null
}

export type Charge = { at: string; person: string; credits: string; what: string }

/** One row of the limits list. A row is its place and, for a person, who; `person_id` null is everyone there. */
export type Limit = LimitValue & {
  scope: Scope
  /** Agent or channel id; empty for the workspace. */
  scope_id: string
  /** The place's name; null for the workspace. */
  name: string | null
  person_id: string | null
  person_name: string | null
  used: string
  state: "active" | "not_started" | "ended"
  revision: number
  /** The latest charges, newest first, at most five. */
  recent: Charge[]
}

export type Place = { scope: "agent" | "channel"; id: string; name: string }
export type MenuUse = { scope: Scope; scope_id: string; name: string; menu: string | null }

export type Menu = {
  key: string
  name: string
  revision: number
  summary: string
  options: number
  used_in: { scope: Scope; scope_id: string; name: string }[]
  catalog: Record<string, unknown>
}

export type RequestChannel = {
  /** Whether this deployment lets the workspace choose its own channel. */
  own_allowed: boolean
  /** The workspace's own pick. */
  value: { id: string; name: string } | null
  /** What applies when there is no pick of its own. */
  default: { name: string } | null
  state: "ready" | "blocked" | "none"
  /** Why `state` is not ready; null when it is. */
  detail: string | null
  open: { count: number; where: string | null }
  revision: number | null
}

export type SupportPage = {
  platform: string
  /** What each person may use where nothing else says; read only. */
  everyone: { each_person: string }
  limits: Limit[]
  menus: Menu[]
  menu_use: MenuUse[]
  /** Agents and channels that have their own "Ask a person" setting, for the Where it shows list. */
  places: Place[]
  agents: { id: string; name: string }[]
  channel: RequestChannel
}

/** PUT /api/support/limits; `limit` null removes the row. `expected_revision` null means the row is new. */
export type LimitChange = {
  scope: Scope; scope_id: string; person_id: string | null
  limit: LimitValue | null
  expected_revision: number | null
}
/** PUT /api/support/menus/{key}; `menu` null removes it. */
export type MenuChange = { menu: Record<string, unknown> | null; expected_revision: number | null }
/** PUT /api/support/menus/use; `menu` null falls back to the level above. */
export type MenuUseChange = { scope: Scope; scope_id: string; menu: string | null; expected: string | null }
/** PUT /api/support/channel; `channel_id` null falls back to the default. */
export type ChannelChange = { channel_id: string | null; expected_revision: number | null }
