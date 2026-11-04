import { useQueryClient } from "@tanstack/react-query"
import { Plus, Trash2, X } from "lucide-react"
import { useState } from "react"
import { toast } from "sonner"
import { Button } from "@/components/ui/button"
import { Command, CommandEmpty, CommandGroup, CommandInput, CommandItem, CommandList } from "@/components/ui/command"
import { Input } from "@/components/ui/input"
import { Switch } from "@/components/ui/switch"
import { cn } from "@/lib/utils"
import { useConfirm } from "@/lib/confirm"
import { pressProps } from "@/lib/filters"
import { addServerSkill, saveSkillFiles, useServerSkills, useSetFollow, useSkillFiles, useSkillFollows } from "../api"
import { Dot, Pill } from "../bits"
import { SkillFilesEditor } from "./SkillFilesEditor"
import type { ServerSkillOption, Skill, SkillFollow } from "../types"
import { Field, Section, type Props } from "./parts"
import { menuEntries, skillBadge, titleOf } from "./server-skills"

const TONE = { muted: "", warn: "border-warning/40 text-warning", bad: "border-destructive/40 text-destructive" }

function MirrorBadge({ follow, pinned }: { follow: SkillFollow; pinned: string }) {
  const badge = skillBadge(follow, pinned)
  return <Pill className={TONE[badge.tone]}>{badge.label}</Pill>
}

export function SkillsEditor(p: Props) {
  const { agent, config, catalog, ro, update, changed, select } = p
  const qc = useQueryClient()
  const have = new Set(config.skills.map((s) => s.skill_id))
  const follows = useSkillFollows(agent.id).data ?? {}
  const menu = useServerSkills(!ro).data
  const [adding, setAdding] = useState<string | null>(null)
  const add = (s: Skill) => { update((c) => { c.skills.push(structuredClone(s)) }, 0); toast(`Added ${s.title}`) }
  const addFromServer = async (o: ServerSkillOption) => {
    setAdding(o.uri)
    try {
      const ref = await addServerSkill(o.uri)
      const title = titleOf(ref.name)
      update((c) => { if (!c.skills.some((x) => x.skill_id === ref.id)) c.skills.push({ type: "custom", skill_id: ref.id, title, description: o.description, version: ref.version }) }, 0)
      toast(`Added ${title}`)
      void qc.invalidateQueries({ queryKey: ["agents", "server-skills"] })
      void qc.invalidateQueries({ queryKey: ["agents", agent.id, "skill-follows"] })
      void qc.invalidateQueries({ queryKey: ["agents", "library", "skills"] })
    } catch (e) {
      toast.error(e instanceof Error ? e.message : "Could not add the skill")
    } finally {
      setAdding(null)
    }
  }
  return (
    <>
      <Section title={`Attached (${config.skills.length})`}>
        <div className="-mx-2">
          {config.skills.map((s) => (
            <div key={s.skill_id} {...pressProps(() => select(`skill:${s.skill_id}`))} className="group flex h-8 cursor-pointer items-center gap-2 rounded-md px-2 text-sm hover:bg-accent">
              <span className="truncate">{s.title}</span>
              {changed.has(`skill:${s.skill_id}`) && <Dot />}
              {follows[s.skill_id] ? <MirrorBadge follow={follows[s.skill_id]!} pinned={s.version} />
                : <span className="truncate text-xs text-faint">{s.type === "anthropic" ? "Anthropic" : s.skill_id}</span>}
              {!ro && (
                <button onClick={(e) => { e.stopPropagation(); update((c) => { c.skills = c.skills.filter((x) => x.skill_id !== s.skill_id) }, 0) }}
                  className="ml-auto rounded p-1 text-faint opacity-0 group-hover:opacity-100 hover:bg-background hover:text-foreground" aria-label={`Remove ${s.title}`}>
                  <X className="size-3.5" />
                </button>
              )}
            </div>
          ))}
        </div>
      </Section>
      {!ro && <NewSkill {...p} />}
      {!ro && (
        <Section title="Add skill">
          <Command className="rounded-lg border">
            <CommandInput placeholder="Search skills" />
            <CommandList className="max-h-80">
              <CommandEmpty>No match</CommandEmpty>
              {menu && (
                <CommandGroup heading={menu.label}>
                  {menuEntries(menu.skills, config.skills).map(({ option: o, disabled }) => (
                    <CommandItem key={o.uri} value={`${menu.label} ${o.name} ${o.uri}`} disabled={!!disabled || adding !== null}
                      onSelect={() => void addFromServer(o)} className="items-start py-2">
                      <Plus className="mt-0.5" />
                      <span className="min-w-0">
                        <span className="block text-sm">{titleOf(o.name)}{disabled && <span className="ml-2 text-xs text-faint">{disabled}</span>}</span>
                        <span className="line-clamp-1 text-xs text-muted-foreground">{o.description}</span>
                      </span>
                    </CommandItem>
                  ))}
                </CommandGroup>
              )}
              <CommandGroup heading={menu ? "Library" : undefined}>
                {catalog?.skills.filter((s) => !have.has(s.skill_id)).map((s) => (
                  <CommandItem key={s.skill_id} value={`${s.title} ${s.skill_id}`} onSelect={() => add(s)} className="items-start py-2">
                    <Plus className="mt-0.5" />
                    <span className="min-w-0">
                      <span className="block text-sm">{s.title}{s.type === "anthropic" && <span className="ml-2 text-xs text-faint">Anthropic</span>}</span>
                      <span className="line-clamp-1 text-xs text-muted-foreground">{s.description}</span>
                    </span>
                  </CommandItem>
                ))}
              </CommandGroup>
            </CommandList>
          </Command>
        </Section>
      )}
    </>
  )
}

const SLUG = /[^a-z0-9]+/g

/** Writes a new custom skill from a name and a description. */
function NewSkill({ agent, config, update, select }: Props) {
  const [name, setName] = useState("")
  const [description, setDescription] = useState("")
  const [busy, setBusy] = useState(false)
  const slug = name.toLowerCase().replace(SLUG, "-").replace(/^-+|-+$/g, "").slice(0, 64)
  const ok = !!slug && !!description.trim() && !config.skills.some((s) => s.skill_id === slug || s.title.toLowerCase() === name.trim().toLowerCase())
  const create = async () => {
    setBusy(true)
    try {
      const doc = `---\nname: ${slug}\ndescription: ${description.trim()}\n---\n\n# ${name.trim()}\n\n`
      const saved = await saveSkillFiles(agent.id, null, { "SKILL.md": doc })
      update((c) => { if (!c.skills.some((x) => x.skill_id === saved.skill_id)) c.skills.push(saved) }, 0)
      setName(""); setDescription("")
      select(`skill:${saved.skill_id}`)
    } catch (e) {
      toast.error(e instanceof Error ? e.message : "Could not create the skill")
    } finally {
      setBusy(false)
    }
  }
  return (
    <Section title="New skill">
      <form className="space-y-3" onSubmit={(e) => { e.preventDefault(); if (ok && !busy) void create() }}>
        <Field label="Name"><Input value={name} onChange={(e) => setName(e.target.value)} className="h-8" placeholder="Weekly summary" /></Field>
        <Field label="Description"><Input value={description} onChange={(e) => setDescription(e.target.value)} className="h-8" placeholder="When to use it" /></Field>
        <div className="flex justify-end"><Button type="submit" size="sm" disabled={!ok || busy}>Create skill</Button></div>
      </form>
    </Section>
  )
}

export function SkillEditor(p: Props & { skill: Skill }) {
  // A new version is a new set of files: start the edits over.
  return <SkillBody key={`${p.skill.skill_id}@${p.skill.version}`} {...p} />
}

function SkillBody({ agent, config, ro, update, select, skill }: Props & { skill: Skill }) {
  const confirm = useConfirm()
  const follow = useSkillFollows(agent.id).data?.[skill.skill_id]
  const setFollow = useSetFollow(agent.id)
  const custom = skill.type === "custom"
  const editable = custom && !ro && !follow
  const files = useSkillFiles(agent.id, skill, editable)
  const badge = follow && skillBadge(follow, skill.version)
  const servers = (skill.requires ?? []).map((k) => ({ k, s: config.mcp_servers.find((x) => x.key === k) }))

  const save = async (body: Record<string, string | null>) => {
    try {
      const saved = await saveSkillFiles(agent.id, skill.skill_id, body, skill.version)
      update((c) => {
        const i = c.skills.findIndex((x) => x.skill_id === skill.skill_id)
        if (saved.skill_id === skill.skill_id && i >= 0) c.skills[i] = { ...c.skills[i]!, ...saved }
        else if (!c.skills.some((x) => x.skill_id === saved.skill_id)) c.skills.push(saved)
      }, 0)
      if (saved.skill_id !== skill.skill_id) select(`skill:${saved.skill_id}`)
    } catch (e) {
      toast.error(e instanceof Error ? e.message : "Could not save the skill")
    }
  }
  const removeSkill = async () => {
    if (!(await confirm({ title: "Remove skill?", body: skill.title, action: "Remove", destructive: true }))) return
    update((c) => { c.skills = c.skills.filter((x) => x.skill_id !== skill.skill_id) }, 0)
    select("skills")
  }
  return (
    <div className="flex min-h-0 flex-1 flex-col">
      <div className="space-y-1 border-b px-5 py-4">
        <div className="flex items-center gap-2">
          <h2 className="text-base font-semibold">{skill.title}</h2>
          <Pill>{follow ? "Server" : custom ? "Custom" : "Anthropic"}</Pill>
          {follow ? <MirrorBadge follow={follow} pinned={skill.version} />
            : <span className="text-xs text-faint">{skill.version && skill.version !== "latest" ? "Pinned" : "Latest"}</span>}
          {!ro && (
            <Button variant="ghost" size="xs" className="ml-auto text-muted-foreground hover:text-destructive" onClick={() => void removeSkill()}>
              <Trash2 />Remove skill
            </Button>
          )}
        </div>
        <p className="text-sm text-muted-foreground">{skill.description}</p>
        {follow && (
          <div className="flex items-center gap-2 pt-2 text-sm">
            <label className="flex items-center gap-2">
              <Switch size="sm" checked={follow.follow} disabled={ro || setFollow.isPending}
                onCheckedChange={(on) => setFollow.mutate({ skill_id: skill.skill_id, follow: on })} />
              Follow updates
            </label>
            {badge?.update && !ro && (
              <Button size="xs" variant="outline" className="ml-auto"
                onClick={() => update((c) => { const s = c.skills.find((x) => x.skill_id === skill.skill_id); if (s) s.version = badge.update! }, 0)}>
                Update to v{badge.update}
              </Button>
            )}
          </div>
        )}
        {(servers.length > 0 || skill.platforms) && (
          <div className="flex flex-wrap items-center gap-1.5 pt-2 text-xs text-muted-foreground">
            {servers.map(({ k, s }) => (
              <button key={k} onClick={() => s && select(`mcp:${k}`)} className={cn("inline-flex h-6 items-center gap-1.5 rounded-md border px-2 hover:bg-accent", !s && "border-destructive/40 text-destructive")}>
                Uses {s?.name ?? k}{!s && " (missing)"}
              </button>
            ))}
            {skill.platforms && <Pill>{skill.platforms.join(", ")} only</Pill>}
          </div>
        )}
      </div>
      {editable ? (
        files.isPending ? <div className="px-5 py-4 text-sm text-muted-foreground">Loading</div>
          : files.isError ? <div className="px-5 py-4 text-sm text-muted-foreground">Files unavailable</div>
          : <SkillFilesEditor files={files.data.files} onSave={save} />
      ) : null}
    </div>
  )
}
