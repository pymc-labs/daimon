import { fireEvent, render, screen } from "@testing-library/react"
import userEvent from "@testing-library/user-event"
import { describe, expect, it, vi } from "vitest"

import { Composer } from "./Composer"

const doc = new File(["a"], "notes.txt", { type: "text/plain" })
const view = (canAttach: boolean, onSend = vi.fn()) => {
  render(<Composer onSend={onSend} onStop={() => {}} busy={false} placeholder="Message" picker={null} canAttach={canAttach} />)
  return onSend
}

describe("Composer files", () => {
  it("offers the paperclip and takes dropped and pasted files when allowed", async () => {
    const send = view(true)
    expect(screen.getByRole("button", { name: "Attach files" })).toBeInTheDocument()
    fireEvent.drop(screen.getByRole("textbox", { name: "Message" }), { dataTransfer: { files: [doc], types: ["Files"] } })
    expect(await screen.findByText("notes.txt")).toBeInTheDocument()
    await userEvent.click(screen.getByRole("button", { name: "Send" }))
    expect(send).toHaveBeenCalledWith("", [doc])
  })

  it("shows no paperclip and ignores dropped and pasted files when not allowed", () => {
    view(false)
    expect(screen.queryByRole("button", { name: "Attach files" })).not.toBeInTheDocument()
    const box = screen.getByRole("textbox", { name: "Message" })
    fireEvent.drop(box, { dataTransfer: { files: [doc], types: ["Files"] } })
    fireEvent.paste(box, { clipboardData: { files: [doc], types: ["Files"] } })
    expect(screen.queryByText("notes.txt")).not.toBeInTheDocument()
    expect(screen.getByRole("button", { name: "Send" })).toBeDisabled()
  })
})
