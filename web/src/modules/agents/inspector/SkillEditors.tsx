import { Plus, Trash2, X } from "lucide-react"
import { toast } from "sonner"
import { Mono } from "@/components/common"
import { Button } from "@/components/ui/button"
import { Command, CommandEmpty, CommandGroup, CommandInput, CommandItem, CommandList } from "@/components/ui/command"
import { cn } from "@/lib/utils"
import { pressProps } from "@/lib/filters"
import { Dot, Pill } from "../bits"
import type { Skill } from "../types"
import { Section, type Props } from "./parts"
import { useLocal } from "./useLocal"

export function SkillsEditor({ config, catalog, ro, update, select, changed }: Props) {
  const have = new Set(config.skills.map((s) => s.skill_id))
  const add = (s: Skill) => { update((c) => { c.skills.push(structuredClone(s)) }, 0); toast(`Added ${s.title}`) }
  return (
    <>
      <Section title={`Attached (${config.skills.length})`}>
        <div className="-mx-2">
          {config.skills.map((s) => (
            <div key={s.skill_id} {...pressProps(() => select(`skill:${s.skill_id}`))} className="group flex h-8 cursor-pointer items-center gap-2 rounded-md px-2 text-sm hover:bg-accent">
              <span className="truncate">{s.title}</span>
              {changed.has(`skill:${s.skill_id}`) && <Dot />}
              <span className="truncate text-xs text-faint">{s.type === "anthropic" ? "Anthropic" : s.skill_id}</span>
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
      {!ro && (
        <Section title="Add skill">
          <Command className="rounded-lg border">
            <CommandInput placeholder="Search skills" />
            <CommandList className="max-h-80">
              <CommandEmpty>No match</CommandEmpty>
              <CommandGroup>
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

export function SkillEditor({ config, ro, update, select, skill }: Props & { skill: Skill }) {
  const custom = skill.type === "custom"
  const [text, setText] = useLocal(skill.content, (v) => update((c) => { c.skills.find((s) => s.skill_id === skill.skill_id)!.content = v }, 500))
  const servers = (skill.requires ?? []).map((k) => ({ k, s: config.mcp_servers.find((x) => x.key === k) }))
  return (
    <div className="flex min-h-0 flex-1 flex-col">
      <div className="space-y-1 border-b px-5 py-4">
        <div className="flex items-center gap-2">
          <h2 className="text-base font-semibold">{skill.title}</h2>
          <Pill>{custom ? "Custom" : "Anthropic"}</Pill>
          <span className="text-xs text-faint">v{skill.version}</span>
        </div>
        <p className="text-sm text-muted-foreground">{skill.description}</p>
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
      <div className="flex h-9 shrink-0 items-center border-b px-5 text-xs text-muted-foreground">
        <Mono>SKILL.md</Mono>
        {!ro && (
          <Button variant="ghost" size="xs" className="ml-auto text-muted-foreground hover:text-destructive"
            onClick={() => { update((c) => { c.skills = c.skills.filter((x) => x.skill_id !== skill.skill_id) }, 0); select("skills") }}>
            <Trash2 />Remove
          </Button>
        )}
      </div>
      <textarea
        value={text}
        readOnly={ro || !custom}
        onChange={(e) => setText(e.target.value)}
        spellCheck={false}
        className="min-h-0 flex-1 resize-none bg-transparent px-5 py-4 font-mono text-[12.5px] leading-[1.7] outline-none"
      />
    </div>
  )
}
