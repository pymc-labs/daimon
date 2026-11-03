import { describe, expect, it } from "vitest"

import { descriptionField, withDescription } from "./skill-meta"

const md = "---\nname: pr-review\ndescription: Review a pull request.\n---\n\n# Review\n"
const read = (m: string) => descriptionField(m).value

describe("skill description", () => {
  it("reads a plain, quoted or empty one-line value", () => {
    expect(read(md)).toBe("Review a pull request.")
    expect(read('---\ndescription: "Use: always"\n---\n')).toBe("Use: always")
    expect(read("---\ndescription: 'It''s here'\n---\n")).toBe("It's here")
    expect(read("---\ndescription: Plain # a comment\n---\n")).toBe("Plain")
  })

  it("always writes a quoted string and leaves the rest alone", () => {
    const out = withDescription(md, "Check tests: now")
    expect(out).toBe('---\nname: pr-review\ndescription: "Check tests: now"\n---\n\n# Review\n')
    expect(read(out)).toBe("Check tests: now")
    expect(read(withDescription(md, 'Say "hi"\nthen go'))).toBe('Say "hi"\nthen go')
  })

  it("adds the line to the front matter when it is missing", () => {
    expect(read(withDescription("---\nname: x\n---\nBody\n", "Hi"))).toBe("Hi")
    expect(withDescription("---\nname: x\n---\nBody\n", "Hi")).toBe('---\ndescription: "Hi"\nname: x\n---\nBody\n')
  })

  it("does not edit a folded, literal or multi-line description", () => {
    for (const front of ["description: >\n  Folded\n  text", "description: |-\n  Literal", "description: Starts here\n  and goes on", 'description: "Open\n  quote"']) {
      const doc = `---\n${front}\n---\nBody\n`
      expect(descriptionField(doc).editable).toBe(false)
      expect(withDescription(doc, "New")).toBe(doc)
    }
  })

  it("ignores a description key outside the front matter or nested in it", () => {
    expect(descriptionField("---\nname: x\nmetadata:\n  description: nested\n---\ndescription: body\n")).toEqual({ value: "", editable: true })
    expect(descriptionField("No front matter\ndescription: x\n").editable).toBe(false)
  })

  it("keeps CRLF line endings", () => {
    expect(withDescription("---\r\nname: x\r\ndescription: a\r\n---\r\nBody", "b")).toBe('---\r\nname: x\r\ndescription: "b"\r\n---\r\nBody')
  })
})
