import { render, screen } from "@testing-library/react"
import userEvent from "@testing-library/user-event"
import { describe, expect, it, vi } from "vitest"

import { CardView } from "./Cards"
import type { Card } from "./types"

const card = (action: "switch_back_to_claude" | null): Card => ({ id: "c1", kind: "refusal", title: "GPT can't answer.", body: "It can't run right now.", help: "Switch back to Claude.", action })
const view = (c: Card, onSwitchBack = vi.fn(async () => "Claude answers here again.")) => {
  render(<CardView card={c} busy={false} onAnswer={() => {}} onSwitchBack={onSwitchBack} />)
  return onSwitchBack
}

describe("refusal card", () => {
  it("offers the way back to Claude when the card carries it, and says what changed", async () => {
    const back = view(card("switch_back_to_claude"))
    await userEvent.click(screen.getByRole("button", { name: "Switch back to Claude" }))
    expect(back).toHaveBeenCalledOnce()
    expect(await screen.findByText("Claude answers here again.")).toBeInTheDocument()
    expect(screen.queryByRole("button", { name: "Switch back to Claude" }), "button gone").not.toBeInTheDocument()
  })

  it("has no button for someone who can't switch", () => {
    view(card(null))
    expect(screen.queryByRole("button")).not.toBeInTheDocument()
  })
})
