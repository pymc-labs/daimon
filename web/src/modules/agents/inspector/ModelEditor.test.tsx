import { QueryClient, QueryClientProvider } from "@tanstack/react-query"
import { render, screen } from "@testing-library/react"
import userEvent from "@testing-library/user-event"
import { beforeEach, describe, expect, it, vi } from "vitest"

import { TooltipProvider } from "@/components/ui/tooltip"
import type { Agent, AgentConfig, Catalog } from "../types"
import { ModelEditor } from "./ModelEditor"

vi.mock("@/lib/api", () => ({ api: { get: vi.fn() } }))

const catalog = {
  models: [
    { id: "claude-opus-5-5", label: "Opus 5.5", backend: "anthropic", in_usd: 4, out_usd: 20, fast: true },
    { id: "gpt-6-luna", label: "GPT-6 Luna", backend: "openai", in_usd: 0.1, out_usd: 0.5, efforts: ["low", "medium", "high"] },
    { id: "gpt-6-plain", label: "GPT-6 Plain", backend: "selfhosted", in_usd: 0.1, out_usd: 0.5 },
  ],
} as Catalog
const agent = { id: "ag_1", model: { id: "claude-opus-5-5", backend: "anthropic" } } as Agent
const show = (model: AgentConfig["model"], update = vi.fn()) => {
  const config = { model } as AgentConfig
  render(
    <QueryClientProvider client={new QueryClient()}><TooltipProvider>
      <ModelEditor node="model" agent={agent} config={config} catalog={catalog} ro={false} changed={new Set()} update={update} select={() => {}} />
    </TooltipProvider></QueryClientProvider>,
  )
  return update
}

beforeEach(() => vi.clearAllMocks())

describe("ModelEditor", () => {
  it("lists every model in one list with its provider as a tag and the picked one selected", () => {
    show({ id: "gpt-6-luna", backend: "openai", speed: "standard", effort: "medium" })
    expect(screen.getByRole("button", { name: /Opus 5\.5/ })).toHaveTextContent("Anthropic")
    expect(screen.getByRole("button", { name: /GPT-6 Luna/ })).toHaveClass("border-primary")
    expect(screen.getByRole("button", { name: /Opus 5\.5/ }), "other provider not selected").not.toHaveClass("border-primary")
  })

  it("picks the model with its provider, and shows no fast mode where there is none", async () => {
    const update = show({ id: "claude-opus-5-5", backend: "anthropic", speed: "standard", effort: "high" })
    await userEvent.click(screen.getByRole("button", { name: /GPT-6 Luna/ }))
    expect(update).toHaveBeenCalledOnce()
    const c = { model: { id: "claude-opus-5-5", backend: "anthropic", speed: "standard", effort: "high" } } as AgentConfig
    vi.mocked(update).mock.calls[0]![0](c)
    expect(c.model, "picked model").toMatchObject({ id: "gpt-6-luna", backend: "openai", speed: "standard" })
  })

  it("offers the profile's efforts where it declares them and keeps fast off", async () => {
    const update = show({ id: "gpt-6-luna", backend: "openai", speed: "standard", effort: "medium" })
    expect(screen.getByRole("switch"), "fast off").toBeDisabled()
    expect(screen.queryByRole("radio", { name: "Max" }), "claude-only level").toBeNull()
    expect(screen.getByRole("radio", { name: "High" }), "effort on").toBeEnabled()
    await userEvent.click(screen.getByRole("radio", { name: "High" }))
    const c = { model: { effort: "medium" } } as AgentConfig
    vi.mocked(update).mock.calls[0]![0](c)
    expect(c.model.effort, "picked effort").toBe("high")
  })

  it("disables effort on a model whose profile declares none", () => {
    show({ id: "gpt-6-plain", backend: "selfhosted", speed: "standard", effort: "medium" })
    expect(screen.getByRole("radio", { name: "High" }), "effort off").toBeDisabled()
  })

  it("moves Claude's top effort to the new model's highest when the model is picked", async () => {
    const update = show({ id: "claude-opus-5-5", backend: "anthropic", speed: "standard", effort: "max" })
    await userEvent.click(screen.getByRole("button", { name: /GPT-6 Luna/ }))
    const c = { model: { id: "claude-opus-5-5", backend: "anthropic", speed: "standard", effort: "max" } } as AgentConfig
    vi.mocked(update).mock.calls[0]![0](c)
    expect(c.model.effort, "mapped down").toBe("high")
  })

  it("shows no level for a model default and keeps it unset on another provider", async () => {
    const update = show({ id: "gpt-6-luna", backend: "openai", speed: "standard", effort: null })
    expect(screen.getByRole("radio", { name: "Medium" }), "nothing picked").not.toBeChecked()
    await userEvent.click(screen.getByRole("button", { name: /GPT-6 Plain/ }))
    const c = { model: { id: "gpt-6-luna", backend: "openai", speed: "standard", effort: null } } as AgentConfig
    vi.mocked(update).mock.calls[0]![0](c)
    expect(c.model.effort, "still the model default").toBeNull()
  })
})
