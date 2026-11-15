import { render, screen } from "@testing-library/react"
import { describe, expect, it } from "vitest"

import { ModelPreviewPanel } from "./ModelPreviewPanel"
import type { ModelPreview } from "./types"

const preview: ModelPreview = {
  moves: true, card: true, allowed: true, skills: ["pricing"], servers: [], capabilities: [], title: "Switch Ada to GPT-6 Luna?",
  lines: ["Stops working: memory and the pricing skill", "Shared threads keep the first model"], drops: ["memory", "skill:sk_1"], digest: "d", revision: 2, refusal: null,
}

describe("ModelPreviewPanel", () => {
  it("shows the card's lines as the server words them", () => {
    render(<ModelPreviewPanel preview={preview} />)
    for (const text of preview.lines) expect(screen.getByText(text), text).toBeInTheDocument()
  })

  it("never shows a raw drop or a category label", () => {
    render(<ModelPreviewPanel preview={preview} />)
    expect(screen.queryByText("skill:sk_1"), "a skill shows by name").not.toBeInTheDocument()
    expect(screen.queryByText("Shared threads"), "no category label").not.toBeInTheDocument()
  })

  it("shows only the refusal when the pick can't go ahead", () => {
    render(<ModelPreviewPanel preview={{ ...preview, allowed: false, refusal: { line: "Ask an admin.", action: null } }} />)
    expect(screen.getByText("Ask an admin.")).toBeInTheDocument()
    expect(screen.queryByText(preview.lines[0]), "lines hidden").not.toBeInTheDocument()
  })

  it("renders nothing for a pick that loses nothing, even one that moves provider", () => {
    const { container } = render(<ModelPreviewPanel preview={{ ...preview, card: false, lines: [] }} />)
    expect(container, "empty").toBeEmptyDOMElement()
  })
})
