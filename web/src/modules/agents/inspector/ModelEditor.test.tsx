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
    { id: "gpt-6-luna", label: "GPT-6 Luna", backend: "openai", in_usd: 0.1, out_usd: 0.5 },
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

  it("disables effort on a provider without it and fast on a model without it", () => {
    show({ id: "gpt-6-luna", backend: "openai", speed: "standard", effort: "medium" })
    expect(screen.getByRole("switch"), "fast off").toBeDisabled()
    expect(screen.getByRole("radio", { name: "High" }), "effort off").toBeDisabled()
  })
})
