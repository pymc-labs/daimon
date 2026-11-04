import type { ServerSkillOption, Skill, SkillFollow } from "../types"

/** The most skills one agent runs with. */
export const SKILL_CAP = 20

export type MenuEntry = { option: ServerSkillOption; disabled: string | null }

/** The server's skills this agent doesn't have yet, each with why it can't be added, if it can't. */
export function menuEntries(options: ServerSkillOption[], attached: Skill[]): MenuEntry[] {
  const have = new Set(attached.map((s) => s.skill_id))
  const names = new Set(attached.map((s) => s.title.toLowerCase()))
  const full = attached.length >= SKILL_CAP
  return options
    .filter((o) => !o.skill_id || !have.has(o.skill_id))
    .map((option) => ({
      option,
      disabled: option.taken || (!option.skill_id && names.has(titleOf(option.name).toLowerCase())) ? "Name taken"
        : full ? `${SKILL_CAP} skills max` : null,
    }))
}

/** `data-validation` as `Data validation`, as the library titles it. */
export const titleOf = (name: string) => {
  const words = name.replace(/[-_]/g, " ").trim()
  return words.slice(0, 1).toUpperCase() + words.slice(1)
}

export type Badge = { label: string; tone: "muted" | "warn" | "bad"; update?: string; apply?: boolean }

/** How a mirrored skill stands on this agent; `update` is the version a pinned agent can move to, `apply` a held one. */
export function skillBadge(follow: SkillFollow, pinned: string): Badge {
  if (follow.status === "unavailable") return { label: "Unavailable", tone: "bad" }
  if (follow.status === "pending") return { label: "Needs approval", tone: "warn" }
  if (follow.status === "needs_reconfirm") return { label: "Needs re-confirm", tone: "warn", apply: true }
  if (follow.follow) return { label: "Following", tone: "muted" }
  if (follow.version && follow.version !== pinned) return { label: "Update available", tone: "warn", update: follow.version }
  return { label: `Pinned v${pinned}`, tone: "muted" }
}
