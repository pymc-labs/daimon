import { Link } from "react-router"

import { AgentAvatar, Avatar } from "@/components/common"
import { ENTITY_FALLBACK, channelLabel } from "@/lib/format"
import { useMe } from "@/lib/hooks"
import type { Capability } from "@/lib/types"
import { cn } from "@/lib/utils"

type Props = { id: string | null; name: string | null; avatar?: string | null; className?: string }

const q = encodeURIComponent

/** A name that opens its object; plain text when there is no id, the viewer may not open it, or `to` is null. */
function EntityLink({ to, cap, label, avatar, className }: {
  to: string | null; cap: Capability; label: string; avatar?: React.ReactNode; className?: string
}) {
  const { data: me } = useMe()
  const body = <>{avatar}<span className="truncate">{label}</span></>
  const base = cn("inline-flex min-w-0 max-w-full items-center gap-1.5", className)
  if (!to || !me?.capabilities.includes(cap)) return <span className={base}>{body}</span>
  return <Link to={to} onClick={(e) => e.stopPropagation()} className={cn(base, "hover:underline")}>{body}</Link>
}

const face = (name: string, avatar?: string | null) => <AgentAvatar src={avatar} name={name} className="size-4 text-[8px]" />

/** `node` opens that node of the agent's canvas, e.g. `skill:<id>`. */
export function AgentLink({ id, name, avatar, node, className }: Props & { node?: string }) {
  const label = name ?? ENTITY_FALLBACK.agent
  return <EntityLink to={id && name ? `/agents/${q(id)}${node ? `?node=${q(node)}` : ""}` : null} cap="agents.view" label={label} avatar={name ? face(name, avatar) : undefined} className={className} />
}

/** `kind` decides the "#": threads, forum posts, categories and teams get none. */
export function ChannelLink({ id, name, kind, className }: Omit<Props, "avatar"> & { kind?: string | null }) {
  const label = name ? channelLabel(name, kind) : ENTITY_FALLBACK.channel
  return <EntityLink to={id && name ? `/channels?channel=${q(id)}` : null} cap="channels.view" label={label} className={className} />
}

export function SkillLink({ id, name, className }: Omit<Props, "avatar">) {
  return <EntityLink to={id && name ? `/agents/skills/${q(id)}` : null} cap="agents.view" label={name ?? "Deleted skill"} className={className} />
}

export function RoutineLink({ id, name, className }: Omit<Props, "avatar">) {
  return <EntityLink to={id && name ? `/routines?routine=${q(id)}` : null} cap="routines.view" label={name ?? "Deleted routine"} className={className} />
}

/** A link for people who manage people, plain text for everyone else. */
export function PersonLink({ id, name, avatar, className }: Props) {
  const label = name ?? ENTITY_FALLBACK.person
  return <EntityLink to={id && name ? `/admin?person=${q(id)}` : null} cap="people.manage" label={label} avatar={name && avatar ? <Avatar name={name} className="size-4 text-[8px]" /> : undefined} className={className} />
}

export function EnvLink({ id, name, className }: Omit<Props, "avatar">) {
  return <EntityLink to={id && name ? `/agents/environments?env=${q(id)}` : null} cap="agents.view" label={name ?? "Deleted sandbox"} className={className} />
}
