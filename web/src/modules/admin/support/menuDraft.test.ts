import { describe, expect, it } from "vitest"

import { cleanKey, menuErrors, move, newField, newOption, toCatalog, toDraft, used } from "./menuDraft"

const menu = {
  name: "Trial",
  options: [
    { key: "review_thread", label: "Review the thread", description: "Quick look", price: { kind: "fixed", amount: "0.5" } },
    { key: "coaching", label: "Live coaching", price: { kind: "steps", label: "Length", steps: [{ label: "30 minutes", amount: "0.5" }, { label: "1 hour", amount: "1" }] },
      fields: [{ key: "slots", label: "Time slots", kind: "paragraph", max_chars: 500, placeholder: "Mon 9:00", required: false }] },
    { key: "hands_on", label: "Hands-on", price: { kind: "range", min: "1", max: "3" }, fields: [{ key: "level", label: "Level", kind: "choice", choices: ["New", "Expert"] }] },
    { key: "build", label: "Build it", price: { kind: "quoted", max: "5" } },
  ],
}

describe("menu draft", () => {
  it("round-trips a menu, keeping what the editor has no input for", () => {
    expect(toCatalog(toDraft(menu))).toEqual(menu)
  })

  it("makes keys from labels and clears them as typed", () => {
    const m = toDraft({ name: " Menu ", options: [] })
    m.options[0] = { ...newOption(), label: "Ask many questions!", fields: [{ ...newField(), label: "Your questions" }, { ...newField(), label: "Your questions" }] }
    const [o] = toCatalog(m).options as { key: string; fields: { key: string }[] }[]
    expect(o.key).toBe("ask_many_questions")
    expect(o.fields.map((f) => f.key)).toEqual(["your_questions", "your_questions_2"])
    expect(cleanKey("Review Thread!")).toBe("review_thread")
    expect(cleanKey("Trial A", "-")).toBe("trial-a")
  })

  it("sends max characters as a number and leaves a blank one to the server default", () => {
    const m = toDraft(menu)
    m.options[1].fields[0].max = ""
    m.options[2].fields[0] = { ...newField(), key: "n", label: "N", max: "300" }
    const options = toCatalog(m).options as { fields: Record<string, unknown>[] }[]
    expect(options[1].fields[0]).not.toHaveProperty("max_chars")
    expect(options[2].fields[0]).toMatchObject({ kind: "text", max_chars: 300 })
  })

  it("counts a steps price as one of the four fields", () => {
    expect(used(toDraft(menu).options[1])).toBe(2)
  })

  it("moves a row and keeps the list when it cannot", () => {
    expect(move([1, 2, 3], 2, -1)).toEqual([1, 3, 2])
    const same = [1, 2]
    expect(move(same, 0, -1)).toBe(same)
  })

  it("makes option keys different when labels repeat", () => {
    const m = toDraft({ name: "M", options: [] })
    m.options = [{ ...newOption(), label: "Review" }, { ...newOption(), label: "Review" }, { ...newOption(), label: "" }]
    expect((toCatalog(m).options as { key: string }[]).map((o) => o.key)).toEqual(["review", "review_2", "option"])
  })

  it("places server errors on their inputs, dropping the kind tags", () => {
    const m = toDraft(menu)
    const problems = [
      { loc: ["menu", "options", 0, "price", "fixed", "amount"], msg: "amount must be more than 0 and at most 1000" },
      { loc: ["menu", "options", 1, "price", "steps", "steps", 1, "amount"], msg: "amount must be more than 0 and at most 1000" },
      { loc: ["menu", "options", 2, "fields", 0, "choice", "choices"], msg: "Tuple should have at least 2 items after validation, not 1" },
    ]
    expect(menuErrors("x", problems, m)).toEqual({
      at: {
        "options.0.price.amount": "amount must be more than 0 and at most 1000",
        "options.1.price.steps.1.amount": "amount must be more than 0 and at most 1000",
        "options.2.fields.0.choices": "Tuple should have at least 2 items after validation, not 1",
      },
      rest: null,
    })
  })

  it("puts an error on its option, or on the page when it names no input", () => {
    const m = toDraft(menu)
    const out = menuErrors(null, [
      { loc: ["menu", "options", 2, "price", "range"], msg: "a range needs min below max" },
      { loc: ["menu"], msg: "option keys must be different" },
      { loc: ["menu", "options", 0, "key"], msg: "String should match pattern" },
    ], m)
    expect(out).toEqual({ at: { "options.2.price": "a range needs min below max", "options.0.label": "String should match pattern" }, rest: "option keys must be different" })
    expect(menuErrors("Couldn't save", [], m)).toEqual({ at: {}, rest: "Couldn't save" })
    expect(menuErrors(null, [{ loc: ["menu", "options", 3], msg: "field keys must be different" }], m).at).toEqual({ "options.3": "field keys must be different" })
  })
})
