import { Plus, Trash2 } from "lucide-react"
import { useState, type ReactNode } from "react"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { cn } from "@/lib/utils"
import { Dot } from "../bits"
import { useSkillFilesDraft, type SkillFilesDraft, type StoredFile } from "./useSkillFilesDraft"

/** File tabs over one textarea. `toolbar` sits at the right of the tab row. */
export function SkillFilesView({ draft, readOnly, toolbar }: { draft: SkillFilesDraft; readOnly?: boolean; toolbar?: ReactNode }) {
  const [naming, setNaming] = useState<string | null>(null)
  const { names, shown, binary, file } = draft
  const addFile = () => { if (draft.add((naming ?? "").trim())) setNaming(null) }
  return (
    <div className="flex min-h-0 flex-1 flex-col">
      <div className="flex h-9 shrink-0 items-center gap-1 border-b px-3 text-xs">
        <div className="flex min-w-0 items-center gap-0.5 overflow-x-auto [scrollbar-width:none]">
          {names.map((n) => (
            <button key={n} onClick={() => draft.open(n)}
              className={cn("flex h-6 shrink-0 items-center rounded-md px-2 font-mono", n === shown ? "bg-accent text-foreground" : "text-muted-foreground hover:bg-accent")}>
              {n}{draft.edited(n) && <Dot />}
            </button>
          ))}
          {!readOnly && (naming === null ? (
            <Button variant="ghost" size="icon-xs" aria-label="Add file" onClick={() => setNaming("")}><Plus /></Button>
          ) : (
            <Input value={naming} autoFocus onChange={(e) => setNaming(e.target.value)} onBlur={() => !naming && setNaming(null)}
              onKeyDown={(e) => { if (e.key === "Enter") addFile(); if (e.key === "Escape") setNaming(null) }}
              className="h-6 w-40 font-mono text-xs" placeholder="references/notes.md" />
          ))}
        </div>
        <div className="ml-auto flex shrink-0 items-center gap-1">
          {!readOnly && shown !== "SKILL.md" && <Button variant="ghost" size="icon-xs" aria-label={`Delete ${shown}`} onClick={draft.drop}><Trash2 /></Button>}
          {toolbar}
        </div>
      </div>
      {binary ? <div className="px-5 py-4 text-sm text-muted-foreground">Binary file, {file?.size} bytes</div> : (
        <textarea
          value={draft.text}
          onChange={(e) => draft.setText(e.target.value)}
          readOnly={readOnly}
          aria-label={shown}
          spellCheck={false}
          className="min-h-0 flex-1 resize-none bg-transparent px-5 py-4 font-mono text-[12.5px] leading-[1.7] outline-none"
        />
      )}
    </div>
  )
}

/** A skill version's files, editable, saved as a new version through `onSave`. */
export function SkillFilesEditor({ files, onSave, readOnly }: { files: StoredFile[]; onSave: (body: Record<string, string | null>) => Promise<void>; readOnly?: boolean }) {
  const draft = useSkillFilesDraft(files, onSave)
  return (
    <SkillFilesView draft={draft} readOnly={readOnly} toolbar={!readOnly && (
      <>
        {draft.dirty && <Button variant="ghost" size="xs" disabled={draft.busy} onClick={draft.discard}>Discard</Button>}
        <Button size="xs" disabled={!draft.dirty || draft.busy} onClick={() => void draft.save()}>Save version</Button>
      </>
    )} />
  )
}
