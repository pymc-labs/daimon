import { Plus, Trash2, X } from "lucide-react"
import { useState } from "react"
import { toast } from "sonner"
import { Button } from "@/components/ui/button"
import { Command, CommandEmpty, CommandGroup, CommandInput, CommandItem, CommandList } from "@/components/ui/command"
import { Input } from "@/components/ui/input"
import { cn } from "@/lib/utils"
import { useConfirm } from "@/components/confirm"
import { pressProps } from "@/components/filters"
import { saveSkillFiles, useSkillFiles } from "../api"
import { Dot, Pill } from "../bits"
import type { Skill } from "../types"
import { Field, Section, type Props } from "./parts"

export function SkillsEditor(p: Props) {
  const { config, catalog, ro, update, changed, select } = p
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
      {!ro && <NewSkill {...p} />}
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

const PATH = /^[A-Za-z0-9][A-Za-z0-9._/-]*$/

export function SkillEditor(p: Props & { skill: Skill }) {
  // A new version is a new set of files: start the edits over.
  return <SkillBody key={`${p.skill.skill_id}@${p.skill.version}`} {...p} />
}

function SkillBody({ agent, config, ro, update, select, skill }: Props & { skill: Skill }) {
  const confirm = useConfirm()
  const custom = skill.type === "custom"
  const files = useSkillFiles(agent.id, skill, custom && !ro)
  const [edits, setEdits] = useState<Record<string, string>>({})
  const [added, setAdded] = useState<string[]>([])
  const [removed, setRemoved] = useState<string[]>([])
  const [path, setPath] = useState("SKILL.md")
  const [naming, setNaming] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const stored = files.data?.files ?? []
  const names = [...stored.map((f) => f.path).filter((n) => !removed.includes(n)), ...added]
  const shown = names.includes(path) ? path : names[0] ?? "SKILL.md"
  const file = stored.find((f) => f.path === shown)
  const binary = !!file && file.content === null
  const text = edits[shown] ?? file?.content ?? ""
  const dirty = added.length > 0 || removed.length > 0 || Object.entries(edits).some(([n, v]) => v !== stored.find((f) => f.path === n)?.content)
  const servers = (skill.requires ?? []).map((k) => ({ k, s: config.mcp_servers.find((x) => x.key === k) }))

  const save = async () => {
    setBusy(true)
    try {
      const body = Object.fromEntries(names.map((n) => [n, edits[n] ?? (added.includes(n) ? "" : null)]))
      const saved = await saveSkillFiles(agent.id, skill.skill_id, body, skill.version)
      update((c) => {
        const i = c.skills.findIndex((x) => x.skill_id === skill.skill_id)
        if (saved.skill_id === skill.skill_id && i >= 0) c.skills[i] = { ...c.skills[i]!, ...saved }
        else if (!c.skills.some((x) => x.skill_id === saved.skill_id)) c.skills.push(saved)
      }, 0)
      if (saved.skill_id !== skill.skill_id) select(`skill:${saved.skill_id}`)
    } catch (e) {
      toast.error(e instanceof Error ? e.message : "Could not save the skill")
    } finally {
      setBusy(false)
    }
  }
  const removeSkill = async () => {
    if (!(await confirm({ title: "Remove skill?", body: skill.title, action: "Remove", destructive: true }))) return
    update((c) => { c.skills = c.skills.filter((x) => x.skill_id !== skill.skill_id) }, 0)
    select("skills")
  }
  const addFile = () => {
    const n = (naming ?? "").trim()
    if (!PATH.test(n) || names.includes(n)) return
    setRemoved((r) => r.filter((x) => x !== n))
    if (!stored.some((f) => f.path === n)) setAdded((a) => [...a, n])
    setPath(n)
    setNaming(null)
  }
  const dropFile = () => {
    if (added.includes(shown)) setAdded((a) => a.filter((x) => x !== shown))
    else setRemoved((r) => [...r, shown])
    setEdits((d) => Object.fromEntries(Object.entries(d).filter(([n]) => n !== shown)))
    setPath("SKILL.md")
  }
  return (
    <div className="flex min-h-0 flex-1 flex-col">
      <div className="space-y-1 border-b px-5 py-4">
        <div className="flex items-center gap-2">
          <h2 className="text-base font-semibold">{skill.title}</h2>
          <Pill>{custom ? "Custom" : "Anthropic"}</Pill>
          <span className="text-xs text-faint">v{skill.version}</span>
          {!ro && (
            <Button variant="ghost" size="xs" className="ml-auto text-muted-foreground hover:text-destructive" onClick={() => void removeSkill()}>
              <Trash2 />Remove skill
            </Button>
          )}
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
      {custom && !ro && (
        <div className="flex h-9 shrink-0 items-center gap-1 border-b px-3 text-xs">
          <div className="flex min-w-0 items-center gap-0.5 overflow-x-auto [scrollbar-width:none]">
            {names.map((n) => (
              <button key={n} onClick={() => setPath(n)}
                className={cn("flex h-6 shrink-0 items-center rounded-md px-2 font-mono", n === shown ? "bg-accent text-foreground" : "text-muted-foreground hover:bg-accent")}>
                {n}{(added.includes(n) || (n in edits && edits[n] !== stored.find((f) => f.path === n)?.content)) && <Dot />}
              </button>
            ))}
            {naming === null ? (
              <Button variant="ghost" size="icon-xs" aria-label="Add file" onClick={() => setNaming("")}><Plus /></Button>
            ) : (
              <Input value={naming} autoFocus onChange={(e) => setNaming(e.target.value)} onBlur={() => !naming && setNaming(null)}
                onKeyDown={(e) => { if (e.key === "Enter") addFile(); if (e.key === "Escape") setNaming(null) }}
                className="h-6 w-40 font-mono text-xs" placeholder="references/notes.md" />
            )}
          </div>
          <div className="ml-auto flex shrink-0 items-center gap-1">
            {shown !== "SKILL.md" && <Button variant="ghost" size="icon-xs" aria-label={`Delete ${shown}`} onClick={dropFile}><Trash2 /></Button>}
            {dirty && <Button variant="ghost" size="xs" disabled={busy} onClick={() => { setEdits({}); setAdded([]); setRemoved([]) }}>Discard</Button>}
            <Button size="xs" disabled={!dirty || busy} onClick={() => void save()}>Save version</Button>
          </div>
        </div>
      )}
      {custom && !ro ? (
        files.isPending ? <div className="px-5 py-4 text-sm text-muted-foreground">Loading</div>
          : files.isError ? <div className="px-5 py-4 text-sm text-muted-foreground">Files unavailable</div>
          : binary ? <div className="px-5 py-4 text-sm text-muted-foreground">Binary file, {file?.size} bytes</div>
          : (
            <textarea
              value={text}
              onChange={(e) => setEdits((d) => ({ ...d, [shown]: e.target.value }))}
              spellCheck={false}
              className="min-h-0 flex-1 resize-none bg-transparent px-5 py-4 font-mono text-[12.5px] leading-[1.7] outline-none"
            />
          )
      ) : null}
    </div>
  )
}
