import { describe, expect, it } from "vitest"

import { SPECS } from "@/mock/workflows-specs"
import { CATALOG, newGrants } from "@/mock/workflows"
import { lineDiff, reviewDiff, reviewCount, trimContext } from "./review"
import { clone } from "./spec"

describe("lineDiff", () => {
  it("marks added, removed and kept lines", () => {
    expect(lineDiff("a\nb\nc", "a\nx\nc\nd")).toEqual([
      { op: "keep", text: "a" }, { op: "del", text: "b" }, { op: "add", text: "x" }, { op: "keep", text: "c" }, { op: "add", text: "d" },
    ])
  })
  it("trims to the changes and one line around them", () => {
    const t = trimContext(lineDiff("1\n2\n3\n4\n5\n6", "1\n2\n3\n4\n5\n6!"))
    expect(t.map((l) => l.text)).toEqual(["5", "6", "6!"])
  })
})

describe("reviewDiff", () => {
  const live = SPECS.lead
  it("is empty for the same spec", () => expect(reviewCount(reviewDiff(live, clone(live)))).toBe(0))

  it("calls out instruction, route and added node changes", () => {
    const s = clone(live)
    s.nodes.research!.instructions = "Find the company.\nContact nobody.\n"
    s.nodes.research!.routes!.unqualified = { to: "tag_spam", when: "Spam" }
    s.nodes.tag_spam = { kind: "action", title: "Tag as spam", tool: "hubspot/tag_contact", args: { tag: "spam" } }
    const r = reviewDiff(live, s)
    const research = r.nodes.find((n) => n.id === "research")!
    expect(research.effect).toBe("changed")
    expect(research.instructions?.some((l) => l.op === "del")).toBe(true)
    expect(research.changes.find((c) => c.label === "Route unqualified")?.to).toBe("Tag as spam")
    const added = r.nodes.find((n) => n.id === "tag_spam")!
    expect(added.effect).toBe("added")
    expect(added.changes.map((c) => c.label)).toContain("Tool")
  })

  it("words agent and post fields as the inspector does", () => {
    const s = clone(live)
    s.nodes.research = { ...s.nodes.research!, agent: "mmm", at: "C0SALES", writes: "policy", memory: "personal", session: "fresh", builtins: "read", timeout: "15m", retry: { attempts: 3 } }
    s.nodes.send = { kind: "post", to: "owner", as_agent: "sales", message: "Hi" }
    const r = reviewDiff(live, s, { catalog: CATALOG })
    const at = (id: string) => Object.fromEntries(r.nodes.find((n) => n.id === id)!.changes.map((c) => [c.label, c.to]))
    expect(at("research")).toMatchObject({ Agent: "MMM Analyst", "Works in": "#sales", Writes: "As the server allows", "Personal memory": "On", Session: "Fresh", "Built-ins": "Read", Timeout: "15m", Tries: "3" })
    expect(at("send")).toMatchObject({ "Post to": "Owner", "Post as": "Sales Desk" })
  })

  it("reports removed nodes and trigger and access changes", () => {
    const s = clone(live)
    delete s.nodes.send
    s.triggers = [{ ...s.triggers![0]!, verify: "github" }]
    s.visibility = "private"
    const r = reviewDiff(live, s, { catalog: CATALOG })
    expect(r.nodes.find((n) => n.id === "send")?.effect).toBe("removed")
    expect(r.triggers[0]).toMatchObject({ effect: "changed", changes: [{ label: "Verification", to: "github" }] })
    expect(r.access).toEqual([{ label: "Visibility", from: "shared", to: "private" }])
  })
})

describe("newGrants", () => {
  it("is empty when nothing is added", () => expect(newGrants(SPECS.triage, clone(SPECS.triage))).toEqual([]))
  it("holds every grant of a first version", () => expect(newGrants(null, SPECS.triage).length).toBeGreaterThan(0))
  it("lists only grants the live version lacked", () => {
    const s = clone(SPECS.lead)
    s.nodes.tag_spam = { kind: "action", title: "Tag as spam", tool: "hubspot/tag_contact", args: { tag: "spam" } }
    s.nodes.research!.routes!.unqualified = "tag_spam"
    expect(newGrants(SPECS.lead, s)).toEqual([{ kind: "unattended_write", node: "tag_spam", detail: "hubspot/tag_contact" }])
  })
})
