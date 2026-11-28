import { describe, expect, it } from "vitest"

import type { Msg } from "@/modules/chat/types"
import { changedNodes, EDIT_TOOL, editCount } from "./live"
import type { Spec } from "./types"

const spec = (nodes: Spec["nodes"]): Spec => ({ schema: "daimon.workflow/v1", name: "W", visibility: "private", start: "a", nodes })
const reply = (...steps: [string, "running" | "done"][]): Msg => ({
  id: "m", role: "assistant", content: "", created_at: "", steps: steps.map(([tool, status], i) => ({ id: String(i), label: tool, tool, status })),
})

describe("live draft", () => {
  it("counts only finished edits in the running reply", () => {
    expect(editCount([])).toBe(0)
    expect(editCount([reply(["read_workflow_draft", "done"], [EDIT_TOOL, "running"])])).toBe(0)
    expect(editCount([reply([EDIT_TOOL, "done"], [EDIT_TOOL, "done"], [EDIT_TOOL, "running"])])).toBe(2)
  })

  it("lists steps that were added or changed, not removed ones", () => {
    const base = spec({ a: { kind: "agent", next: "b" }, b: { kind: "finish" }, c: { kind: "finish" } })
    const next = spec({ a: { kind: "agent", next: "d", title: "A" }, b: { kind: "finish" }, d: { kind: "finish" } })
    expect(changedNodes(base, next)).toEqual(["a", "d"])
  })
})
