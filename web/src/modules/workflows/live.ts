// Ask Daimon edits the draft while it talks: the canvas follows each edit, flashes what changed, and offers Undo.
import { useQueryClient } from "@tanstack/react-query"
import { useEffect, useMemo, useRef, useState } from "react"
import { toast } from "sonner"

import type { Msg } from "@/modules/chat/types"
import { send, useThread } from "@/modules/chat/turns"
import { getDraft, type DraftEditor } from "./api"
import { diff } from "./spec"
import type { Spec } from "./types"

export const EDIT_TOOL = "edit_workflow_draft"

/** Edits the running reply has finished: when it grows, the draft on the server has a new revision. */
export const editCount = (messages: Msg[]) => {
  const m = messages[messages.length - 1]
  return m?.role === "assistant" ? (m.steps ?? []).filter((s) => s.tool === EDIT_TOOL && s.status === "done").length : 0
}

/** Steps that exist in `next` and differ from `base`: what to flash and offer as chips. */
export const changedNodes = (base: Spec | null, next: Spec) =>
  diff(base, next).filter((c) => c.node && c.effect !== "removed").map((c) => c.node!)

export type TurnEdit = { nodes: string[]; undo?: () => void }

export function useLiveDraft(wfId: string, editor: DraftEditor, spec: Spec, sid: string | undefined, selected: string | null) {
  const qc = useQueryClient()
  const t = useThread(sid)
  const live = useRef({ spec, selected, last: "" })
  useEffect(() => { live.current = { spec, selected, last: t.messages[t.messages.length - 1]?.id ?? "" } })
  const before = useRef<Spec | null>(null)
  const was = useRef(false)
  const [flash, setFlash] = useState<Set<string>>(new Set())
  const [edits, setEdits] = useState<Record<string, TurnEdit>>({})

  useEffect(() => {
    if (!flash.size) return
    const t = setTimeout(() => setFlash(new Set()), 1600)
    return () => clearTimeout(t)
  }, [flash])

  /** Take the server's draft when it is newer than ours and nothing of ours is waiting to save. */
  const refresh = async () => {
    const d = await getDraft(wfId).catch(() => null)
    if (!d || editor.dirty() || d.revision <= (editor.revision() ?? -1)) return live.current.spec
    setFlash(new Set(changedNodes(live.current.spec, d.spec)))
    editor.adopt(d)
    return d.spec
  }
  const count = useMemo(() => editCount(t.messages), [t.messages])
  useEffect(() => { if (count) void refresh() }, [count])   // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => {
    if (!t.busy) return
    const i = setInterval(() => void refresh(), 1500)
    return () => clearInterval(i)
  }, [t.busy])   // eslint-disable-line react-hooks/exhaustive-deps

  // The turn ended: settle on the server's draft, then say what changed.
  useEffect(() => {
    if (t.busy) { was.current = true; return }
    if (!was.current) return
    was.current = false
    const base = before.current
    before.current = null
    void refresh().then((after) => {
      if (!base || !diff(base, after).length) return
      const nodes = changedNodes(base, after)
      const id = live.current.last
      const undo = () => {
        editor.save(base, 0)
        setEdits((e) => ({ ...e, [id]: { nodes } }))
      }
      setEdits((e) => ({ ...e, [id]: { nodes, undo } }))
      toast(nodes.length ? `Daimon changed ${nodes.length} step${nodes.length === 1 ? "" : "s"}` : "Daimon changed the workflow", { action: { label: "Undo", onClick: undo } })
    })
  }, [t.busy])   // eslint-disable-line react-hooks/exhaustive-deps

  /** Saves what the canvas has pending, so Daimon reads the draft as shown; remembers it for Undo. */
  const opts = {
    prepare: async () => { await editor.settle(); before.current = live.current.spec },
    context: () => ({ selected_node: live.current.selected }),
  }
  return { flash, edits, opts, ask: (text: string) => { if (sid) void send(qc, sid, text, [], opts) } }
}
