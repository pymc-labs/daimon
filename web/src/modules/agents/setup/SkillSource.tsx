import { BrandIcon } from "@/components/brand"
import { Ago } from "@/components/common"
import { Section } from "../inspector/parts"
import type { Agent } from "../types"
import { Out } from "./Out"
import { useAgentGithub, type GithubSkill } from "./github"

const WHERE: Partial<Record<GithubSkill["origin"], string>> = { library: "Library", external: "Published outside Daimon" }

const place = (s: GithubSkill) => s.repo ? `${s.repo}${s.branch ? `@${s.branch}` : ""}${s.path ? `/${s.path.replace(/^\//, "")}` : ""}` : null

/** Where one skill came from, under its title; nothing for a built-in skill or a reader without setup access. */
export function SkillSource({ agent, skillId }: { agent: Agent; skillId: string }) {
  const skill = useAgentGithub(agent.id, agent.can_view_details).data?.skills.find((s) => s.skill_id === skillId)
  if (!skill || skill.origin === "built_in") return null
  const where = skill.origin === "repo" ? place(skill) : null
  return (
    <div className="flex min-w-0 flex-wrap items-center gap-x-3 gap-y-0.5 text-xs text-muted-foreground">
      {where ? (
        <span className="flex min-w-0 max-w-full items-center gap-1.5">
          <BrandIcon brand="github" className="size-3 shrink-0" />
          <Out href={skill.url} className="truncate font-mono text-[12.5px]">{where}</Out>
        </span>
      ) : WHERE[skill.origin] && <span>{WHERE[skill.origin]}</span>}
      {skill.updated_at && <span className="shrink-0 text-faint">Updated <Ago iso={skill.updated_at} /></span>}
    </div>
  )
}

/** The release an outside pipeline stamped on the agent, at the top of the skills list. */
export function SkillsRelease({ agent }: { agent: Agent }) {
  const release = useAgentGithub(agent.id, agent.can_view_details).data?.release
  if (!release) return null
  return (
    <Section className="py-3">
      <div className="flex min-h-6 items-center gap-2 text-sm">
        <span className="text-xs text-muted-foreground">Release</span>
        <Out href={release.url} className="truncate font-medium">{release.name}</Out>
        {release.at && <Ago iso={release.at} className="ml-auto text-xs text-muted-foreground" />}
      </div>
    </Section>
  )
}
