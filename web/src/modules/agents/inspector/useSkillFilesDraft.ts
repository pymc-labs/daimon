import { useState } from "react"
import type { SkillFiles } from "../types"

export type StoredFile = SkillFiles["files"][number]

const PATH = /^[A-Za-z0-9][A-Za-z0-9._/-]*$/

/**
 * The edits to one version's files: what changed, which file is open, and saving them as the body
 * of a new version (a file left alone is null, kept from the version the editor loaded).
 * Start over with a new `key` when the files change.
 */
export function useSkillFilesDraft(stored: StoredFile[], onSave: (body: Record<string, string | null>) => Promise<void>) {
  const [edits, setEdits] = useState<Record<string, string>>({})
  const [added, setAdded] = useState<string[]>([])
  const [removed, setRemoved] = useState<string[]>([])
  const [path, setPath] = useState("SKILL.md")
  const [busy, setBusy] = useState(false)
  const names = [...stored.map((f) => f.path).filter((n) => !removed.includes(n)), ...added]
  const shown = names.includes(path) ? path : names[0] ?? "SKILL.md"
  const file = stored.find((f) => f.path === shown)
  const edited = (n: string) => added.includes(n) || (n in edits && edits[n] !== stored.find((f) => f.path === n)?.content)
  const dirty = added.length > 0 || removed.length > 0 || Object.keys(edits).some(edited)
  const discard = () => { setEdits({}); setAdded([]); setRemoved([]) }
  return {
    names, shown, file, edited, dirty, busy, discard,
    binary: !!file && file.content === null,
    text: edits[shown] ?? file?.content ?? "",
    open: setPath,
    setText: (v: string) => setEdits((d) => ({ ...d, [shown]: v })),
    add: (n: string) => {
      if (!PATH.test(n) || names.includes(n)) return false
      setRemoved((r) => r.filter((x) => x !== n))
      if (!stored.some((f) => f.path === n)) setAdded((a) => [...a, n])
      setPath(n)
      return true
    },
    drop: () => {
      if (added.includes(shown)) setAdded((a) => a.filter((x) => x !== shown))
      else setRemoved((r) => [...r, shown])
      setEdits((d) => Object.fromEntries(Object.entries(d).filter(([n]) => n !== shown)))
      setPath("SKILL.md")
    },
    save: async () => {
      setBusy(true)
      try { await onSave(Object.fromEntries(names.map((n) => [n, edits[n] ?? (added.includes(n) ? "" : null)]))) }
      finally { setBusy(false) }
    },
  }
}

export type SkillFilesDraft = ReturnType<typeof useSkillFilesDraft>
