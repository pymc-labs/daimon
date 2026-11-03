import { ExternalLink, Trash2 } from "lucide-react"
import { useState } from "react"
import { useNavigate, useParams } from "react-router"

import { QueryState } from "@/components/QueryState"
import { Ago } from "@/components/common"
import { AgentLink, ChannelLink } from "@/components/links"
import { PanelMenu, PanelSection, SettingRow, Tag, type MenuAction } from "@/components/panel"
import { Page } from "@/components/shell/Page"
import { Button } from "@/components/ui/button"
import { Textarea } from "@/components/ui/textarea"
import { ApiError } from "@/lib/api"
import { useConfirm } from "@/lib/confirm"
import { useParam } from "@/lib/filters"
import { cn } from "@/lib/utils"
import { SkillFilesView } from "../inspector/SkillFilesEditor"
import { useSkillFilesDraft } from "../inspector/useSkillFilesDraft"
import type { SkillDetail, SkillFiles, SkillVersion } from "../types"
import { useLibraryFiles, useSkill, useSkillVersions, useSkillWrites } from "./api"
import { descriptionField, sourceLabel, withDescription } from "./skill-meta"

export function SkillPage() {
  const { id = "" } = useParams()
  const skill = useSkill(id)
  const [held] = useParam("version")
  const latest = skill.data?.version ?? null
  const version = held && held !== latest ? held : null
  const custom = skill.data?.source === "custom"
  const files = useLibraryFiles(id, version)
  const versions = useSkillVersions(id)
  return (
    <QueryState
      query={skill}
      skeleton={<Page crumbs={[{ label: "Agents", to: "/agents" }, { label: "Skills", to: "/agents/skills" }, { label: "Skill" }]}><div /></Page>}
    >
      {(s) => (
        <Loaded
          key={`${s.id}@${version ?? ""}`}
          skill={s} custom={custom} files={files.data} filesLoading={custom && files.isPending}
          versions={versions.data ?? []} viewing={version}
        />
      )}
    </QueryState>
  )
}

function Loaded({ skill, custom, files, filesLoading, versions, viewing }: {
  skill: SkillDetail; custom: boolean; files: SkillFiles | undefined; filesLoading: boolean; versions: SkillVersion[]; viewing: string | null
}) {
  const navigate = useNavigate()
  const confirm = useConfirm()
  const [, setVersion] = useParam("version")
  const { save, restore, remove } = useSkillWrites()
  const readOnly = !custom || skill.default || viewing !== null
  // The draft outlives refetches. `base` is the version it sits on; if the skill moves past it, or a
  // save is refused, they pick Reload or Overwrite, so a newer version is never replaced unasked.
  const [base, setBase] = useState(skill.version)
  const [refused, setRefused] = useState(false)
  const draft = useSkillFilesDraft(files?.files ?? [], async (body) => {
    try {
      await save.mutateAsync({ id: skill.id, files: body, expected_version: skill.version })
    } catch (e) {
      if (e instanceof ApiError && e.status === 409) setRefused(true)
      throw e
    }
  })
  if (!draft.dirty && base !== skill.version) setBase(skill.version)
  const changed = draft.dirty && !draft.busy && (refused || base !== skill.version)
  const submit = async () => {
    try { await draft.save() } catch { return }
    draft.discard()
    setRefused(false)
  }
  const drop = async () => {
    if (!(await confirm({ title: `Delete ${skill.title}?`, action: "Delete", destructive: true }))) return
    remove.mutate(skill.id, { onSuccess: () => navigate("/agents/skills") })
  }
  const menu: MenuAction[] = [
    ...(skill.source_url ? [{ label: "Open source", icon: <ExternalLink />, onSelect: () => window.open(skill.source_url!, "_blank", "noopener") }] : []),
    ...(custom && !skill.default ? [{ label: "Delete", icon: <Trash2 />, destructive: true, onSelect: () => void drop() }] : []),
  ]
  const markdown = draft.read("SKILL.md")
  const description = descriptionField(markdown)
  const showDirty = custom && !readOnly && draft.dirty
  const reload = () => { draft.discard(); setRefused(false) }
  return (
    <Page
      crumbs={[{ label: "Agents", to: "/agents" }, { label: "Skills", to: "/agents/skills" }, { label: skill.title }]}
      flush className="flex flex-col"
      actions={
        <>
          {showDirty && <Button size="sm" variant="ghost" disabled={draft.busy} onClick={draft.discard}>Discard</Button>}
          {showDirty && !changed && <Button size="sm" disabled={draft.busy} onClick={() => void submit()}>Save version</Button>}
          <PanelMenu items={menu} />
        </>
      }
    >
      <div className="flex min-h-0 flex-1 flex-col overflow-y-auto lg:flex-row lg:overflow-hidden">
        <div className="flex h-[70vh] min-h-[360px] min-w-0 flex-col lg:h-auto lg:min-h-0 lg:flex-1">
          {showDirty && changed && (
            <div role="alert" className="flex min-h-10 shrink-0 flex-wrap items-center gap-2 border-b bg-muted/40 px-4 py-1.5 text-sm lg:px-6">
              <span>This skill changed since you started editing.</span>
              <Button size="xs" variant="ghost" className="ml-auto" disabled={draft.busy} onClick={reload}>Reload</Button>
              <Button size="xs" disabled={draft.busy} onClick={() => void submit()}>Overwrite</Button>
            </div>
          )}
          {viewing && (
            <div className="flex h-10 shrink-0 items-center gap-2 border-b bg-muted/40 px-4 text-sm lg:px-6">
              <span>Viewing version {viewing}</span>
              <Button size="xs" className="ml-auto" disabled={restore.isPending}
                onClick={() => restore.mutate({ id: skill.id, version: viewing }, { onSuccess: () => setVersion(null) })}>Restore</Button>
              <Button size="xs" variant="ghost" onClick={() => setVersion(null)}>Back to latest</Button>
            </div>
          )}
          {files ? <SkillFilesView draft={draft} readOnly={readOnly} /> : (
            <div className="px-5 py-4 text-sm text-muted-foreground">{filesLoading ? "Loading" : "The files of this skill are not available."}</div>
          )}
        </div>
        <aside className="shrink-0 border-t lg:w-80 lg:overflow-y-auto lg:border-t-0 lg:border-l">
          <PanelSection title="About" className="border-t-0">
            <div className="space-y-2.5">
              {custom && !readOnly && files && description.editable ? (
                <div className="space-y-1 text-sm">
                  <label htmlFor="skill-description" className="text-muted-foreground">Description</label>
                  <Textarea id="skill-description" rows={3} value={description.value} onChange={(e) => draft.setFile("SKILL.md", withDescription(markdown, e.target.value))} />
                </div>
              ) : <SettingRow label="Description" top><span>{skill.description || "–"}</span></SettingRow>}
              <SettingRow label="Source">
                {skill.source_url
                  ? <a href={skill.source_url} target="_blank" rel="noopener noreferrer" className="inline-flex max-w-full items-center gap-1 hover:underline"><span className="truncate">GitHub</span><ExternalLink className="size-3 shrink-0" /></a>
                  : sourceLabel(skill)}
              </SettingRow>
            </div>
          </PanelSection>
          <PanelSection title={`Used by${skill.used_by.length ? ` (${skill.used_by.length})` : ""}`}>
            {!skill.used_by.length ? <div className="text-sm text-muted-foreground">Nothing</div> : (
              <ul className="space-y-1.5 text-sm">
                {skill.used_by.map((u) => (
                  <li key={`${u.kind}:${u.id}`} className="flex items-center gap-2">
                    {u.kind === "agent" ? <AgentLink id={u.id} name={u.name} node={`skill:${skill.id}`} /> : <ChannelLink id={u.id} name={u.name} />}
                    <span className="ml-auto shrink-0 text-xs text-muted-foreground">{u.version ? `v${u.version}` : "Latest"}</span>
                  </li>
                ))}
              </ul>
            )}
          </PanelSection>
          {custom && (
            <PanelSection title="Versions">
              <QueryState query={{ data: versions, error: null, isError: false, refetch: () => undefined }} isEmpty={(v) => !v.length} empty="None">{(v) => (
                <ul className="-mx-2 text-sm">
                  {v.map((x) => {
                    const current = x.version === (viewing ?? skill.version)
                    return (
                      <li key={x.version}>
                        <button type="button" aria-current={current || undefined} onClick={() => setVersion(x.version === skill.version ? null : x.version)}
                          className={cn("flex w-full items-center gap-2 rounded-md px-2 py-1.5 text-left hover:bg-accent", current && "bg-accent")}>
                          <span className="font-mono text-xs">v{x.version}</span>
                          {x.version === skill.version && <Tag>Latest</Tag>}
                          <span className="ml-auto flex min-w-0 items-center gap-1.5 text-xs text-muted-foreground">
                            {x.author && <span className="truncate">{x.author}</span>}
                            <Ago iso={x.created_at} className="shrink-0" />
                          </span>
                        </button>
                      </li>
                    )
                  })}
                </ul>
              )}</QueryState>
            </PanelSection>
          )}
        </aside>
      </div>
    </Page>
  )
}
