import { render, screen } from "@testing-library/react"
import { describe, expect, it } from "vitest"

import { ModelPreviewPanel } from "./ModelPreviewPanel"
import type { ModelPreview } from "./types"

const preview: ModelPreview = {
  moves: true, allowed: true, skills: ["pricing"], servers: [], capabilities: [], shared_limits: ["Shared threads keep the first model"],
  drops: ["memory", "sandbox"], cost: "Costs about the same", threads: "Threads switch on their next message.", digest: "d", revision: 2, refusal: null,
}

describe("ModelPreviewPanel", () => {
  it("lists what is lost, the shared limits, the cost and the threads", () => {
    render(<ModelPreviewPanel preview={preview} />)
    for (const text of ["Memory", "Sandbox", "pricing", "Shared threads keep the first model", "Costs about the same", "Threads switch on their next message."]) {
      expect(screen.getByText(text), text).toBeInTheDocument()
    }
  })

  it("shows only the refusal when the pick can't go ahead", () => {
    render(<ModelPreviewPanel preview={{ ...preview, allowed: false, refusal: { line: "Ask an admin.", action: null } }} />)
    expect(screen.getByText("Ask an admin.")).toBeInTheDocument()
    expect(screen.queryByText("Memory"), "drops hidden").not.toBeInTheDocument()
  })

  it("renders nothing for a pick that stays on the provider", () => {
    const { container } = render(<ModelPreviewPanel preview={{ ...preview, moves: false }} />)
    expect(container, "empty").toBeEmptyDOMElement()
  })
})
