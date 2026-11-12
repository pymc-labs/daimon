import { useEffect, useMemo, useState } from "react"
import { useLocation, useSearchParams } from "react-router"

import { ToggleGroup, ToggleGroupItem } from "@/components/ui/toggle-group"
import { askDaimon, useCatalog, useDraft, useDraftEditor } from "./api"
import { AskDaimon, type AskTurn } from "./AskDaimon"
import { Canvas, type InsertAt } from "./Canvas"
import { DraftBar, DraftTools } from "./DraftBar"
import { Inspector } from "./inspector/Inspector"
import { clone, duplicateNode, insertNode, removeNode, validate } from "./spec"
import { SpecView } from "./SpecView"
import type { AskMessage, Kind, Node, Spec, Workflow } from "./types"

export function Design({ wf }: { wf: Workflow }) {
  const ro = !wf.can_edit
  const { data: draft, isPending } = useDraft(wf.id, !ro)
  const { data: catalog } = useCatalog()
  const editor = useDraftEditor(wf, draft)
  const [params, setParams] = useSearchParams()
  const node = params.get("node") ?? "", view = params.get("view"), ask = params.get("ask")
  // One navigation per change: separate setters in one tick would drop all but the last.
  const put = (next: Record<string, string | null>) => setParams((p) => {
    for (const [k, v] of Object.entries(next)) if (v) p.set(k, v); else p.delete(k)
    return p
  }, { replace: true })
  const state = useLocation().state as { ask?: AskMessage[] } | null
  const [turns, setTurns] = useState<AskTurn[]>(() => state?.ask ?? [{ role: "assistant", text: "What should change?" }])
  const [pending, setPending] = useState(false)
  const [flash, setFlash] = useState<Set<string>>(new Set())
  useEffect(() => { if (!flash.size) return; const t = setTimeout(() => setFlash(new Set()), 1600); return () => clearTimeout(t) }, [flash])

  const spec = draft?.spec ?? wf.spec
  const validation = useMemo(() => draft?.validation ?? validate(spec), [draft, spec])
  const changed = useMemo(() => new Set((draft?.changes ?? []).map((c) => c.node).filter((n): n is string => !!n)), [draft])
  const n = node ? spec.nodes[node] : undefined

  const select = (id: string | null) => put(id ? { node: id, ask: null } : { node: null })
  const add = (at: InsertAt, kind: Kind) => { const r = insertNode(spec, at, kind); editor.save(r.spec, 0); select(r.id) }
  const remove = (id: string) => { editor.save(removeNode(spec, id), 0); if (node === id) select(null) }
  const duplicate = (id: string) => { const r = duplicateNode(spec, id); editor.save(r.spec, 0); select(r.id) }
  const set = (fn: (n: Node, s: Spec) => void, delay?: number) => {
    if (!node) return
    const s = clone(spec)
    fn(s.nodes[node]!, s)
    editor.save(s, delay)
  }
  const send = async (text: string) => {
    const before = spec
    setTurns((t) => [...t, { role: "user", text }])
    setPending(true)
    try {
      const r = await askDaimon(wf.id, text)
      editor.adopt(r.draft)
      const turn: AskTurn = { role: "assistant", text: r.reply, changed: r.changed }
      if (r.changed.length) turn.undo = () => { editor.save(before, 0); setTurns((t) => t.map((x) => (x === turn ? { ...x, undo: undefined } : x))) }
      setTurns((t) => [...t, turn])
      if (r.changed.length) setFlash(new Set(r.changed))
    } catch (e) {
      setTurns((t) => [...t, { role: "assistant", text: (e as Error).message }])
    } finally { setPending(false) }
  }
  const panel = !!n || !!ask
  const tools = draft && !ro && !draft.changes.length ? <DraftTools wf={wf} draft={draft} ask={() => put({ node: null, ask: "1" })} /> : null

  return (
    <>
      <ToggleGroup type="single" variant="segmented" size="sm" value={view || "canvas"} onValueChange={(v) => v && put({ view: v === "canvas" ? null : v })}
        className="absolute top-4 left-4 z-10 bg-popover shadow-xs">
        <ToggleGroupItem value="canvas" className="px-3 text-xs">Canvas</ToggleGroupItem>
        <ToggleGroupItem value="spec" className="px-3 text-xs">Spec</ToggleGroupItem>
      </ToggleGroup>
      {view === "spec" ? (
        <>
          <SpecView spec={spec} validation={validation} ro={ro} onChange={(s) => editor.save(s, 0)} />
          {tools && <div className="absolute top-4 right-4 z-10 flex items-center gap-2">{tools}</div>}
        </>
      ) : (ro || !isPending) && (
        // Wait for the draft, so the canvas frames the graph it will show.
        <Canvas spec={spec} validation={validation} catalog={catalog} changed={changed} flash={flash} selected={node || null} ro={ro}
          select={select} add={add} remove={remove} duplicate={duplicate} panel={panel} tools={tools} />
      )}
      {n && node && <Inspector key={node} id={node} n={n} spec={spec} catalog={catalog} ro={ro} set={set} validation={validation} close={() => select(null)} />}
      {!n && ask && !ro && (
        <AskDaimon spec={spec} messages={turns} pending={pending} send={(t) => void send(t)} select={select} close={() => put({ ask: null })} />
      )}
      {draft && !ro && <DraftBar wf={wf} draft={draft} shift={panel} select={(id) => put({ view: null, node: id, ask: null })} ask={() => put({ node: null, ask: "1" })} adopt={editor.adopt} />}
    </>
  )
}
