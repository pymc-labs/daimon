import type { AgentRef, Identity, Role } from "@/lib/types"

export type AdminPerson = {
  id: string
  name: string
  email: string
  role: Role
  status: "active" | "suspended"
  groups: string[]
  identities: Identity[]
  credit: { left_usd: number; total_usd: number } | null
  last_seen: string | null
  /** A visitor with a live invite: the invite and whether it lets them attach files. */
  invite_id?: string | null
  allow_files?: boolean | null
}

export type Invite = {
  id: string
  email: string
  role: "visitor" | "member"
  agents: AgentRef[]
  credit_usd: number
  allow_files: boolean
  expires_at: string
  status: "pending" | "accepted" | "expired"
  by: string
  link?: string
}

export type AgentAccess = {
  agent: AgentRef
  web: boolean
  who: "everyone" | "groups" | "invites"
  groups: string[]
  fixed_to: string | null
  fixed_name: string | null
  /** Null where the workspace has no direct messages. */
  dm: DmGrant | null
}

export type AuditSource = "web" | "security"
export type AuditTarget = { kind: "person" | "invite" | "other"; id: string | null; name: string }
export type AuditEntry = {
  id: string; source: AuditSource; at: string; who: string; actor_id: string | null; action: string; target: AuditTarget; detail?: string
  /** Security events only: every readable field the event stores. */
  facts?: [string, string][]
  /** Security events only: where it happened; `name` is null while the channel is unnamed. */
  channel?: { id: string; name: string | null } | null
  /** Security events only: the message and turn ids, to copy; never shown. */
  reference?: string | null
}
export type AuditKind = { source: AuditSource; value: string; label: string }

export type Access = {
  /** The people who may start turns; null when everyone may. */
  people: { id: string; name: string }[] | null
  /** Allowed ids of people the web app doesn't list. */
  others: number
}

export type Providers = {
  offered: { id: string; label: string }[]
  allowed: string[]
  /** Whether the list follows the deployment's keyed providers. */
  followed: boolean
}

export type WorkspaceSettings = {
  default_agent: string
  access: Access
  /** Null where choosing providers is switched off. */
  providers: Providers | null
}

export type SettingsPatch = {
  default_agent?: string
  invokers?: { everyone: boolean; people: string[] }
  providers?: string[] | "default"
}

export type FeedbackReason = { code: string; label: string; count: number | null }
export type AgentFeedback = { id: string | null; name: string; up: number | null; down: number | null; reasons: FeedbackReason[] }
export type Feedback = { days: number; agents: AgentFeedback[] }

/** Who may DM an agent: everyone, only the listed people and groups, or everyone but them. */
export type DmWho = "members" | "listed" | "excluded"
/** A person by platform id; `name` is null when the platform never told us. */
export type DmPerson = { id: string; name: string | null }
export type DmGrant = { on: boolean; who: DmWho; users: DmPerson[]; groups: string[] }
export type DmGrantChange = { who: DmWho; user_ids: string[]; group_ids: string[] }

export type DmSubject = "user" | "group" | "everyone"
/** `agent` null is the default agent; `name` is a listed person's stored name. */
export type DmRule = { position: number; subject: DmSubject; subject_id: string | null; name: string | null; agent: string | null; locked: boolean }
/** `platform` is null where the workspace has no direct messages. */
export type DmSettings = { platform: string | null; default_agent: string | null; agents: { id: string; name: string }[]; rules: DmRule[] }
export type DmRuleAdd = { subject: DmSubject; subject_id: string | null; agent: string | null; locked: boolean }
export type DmRuleChange = { agent?: string | null; locked?: boolean; to?: number }

/** Where a DM from a person goes; `rule` is the 0-based position of the rule that matched. */
export type DmExplain = {
  route: "start" | "choose" | "refused" | "unchecked"
  agent: string | null
  reason: "locked" | "pick" | "rule" | "default" | "only" | "no_agent" | "locked_unavailable" | null
  agents: string[]
  allowed: string[]
  rule: number | null
}
/** The platform's groups, live, then stored ids it no longer lists (no name). */
export type PlatformGroups = { platform: string; groups: { id: string; name: string | null }[]; problem: "reinstall" | "unavailable" | null }
