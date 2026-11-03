import { describe, expect, it } from "vitest"

import { descriptionOf, withDescription } from "./skill-meta"

const md = "---\nname: pr-review\ndescription: Review a pull request.\n---\n\n# Review\n"

describe("skill description", () => {
  it("reads the front matter line, quoted or not", () => {
    expect(descriptionOf(md)).toBe("Review a pull request.")
    expect(descriptionOf('---\ndescription: "Use: always"\n---\n')).toBe("Use: always")
  })

  it("rewrites it and quotes what would break the front matter", () => {
    expect(withDescription(md, "Check tests")).toContain("description: Check tests\n")
    expect(withDescription(md, "Use: always")).toContain('description: "Use: always"\n')
    expect(descriptionOf(withDescription(md, "Use: always"))).toBe("Use: always")
  })

  it("adds the line when it is missing", () => {
    expect(descriptionOf(withDescription("---\nname: x\n---\n", "Hi"))).toBe("Hi")
  })
})
