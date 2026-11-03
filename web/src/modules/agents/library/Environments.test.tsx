import { QueryClient, QueryClientProvider } from "@tanstack/react-query"
import { render, screen, waitFor, within } from "@testing-library/react"
import userEvent from "@testing-library/user-event"
import { MemoryRouter } from "react-router"
import { toast } from "sonner"
import { beforeEach, describe, expect, it, vi } from "vitest"

import { ConfirmProvider } from "@/components/confirm"
import { ApiError, api } from "@/lib/api"
import type { EnvironmentDoc } from "../types"
import { EnvironmentForm } from "./Environments"

vi.mock("sonner", () => ({ toast: { error: vi.fn() } }))
vi.mock("@/lib/api", async (orig) => ({ ...(await orig<typeof import("@/lib/api")>()), api: { get: vi.fn(), put: vi.fn(), post: vi.fn(), del: vi.fn() } }))

const env: EnvironmentDoc = {
  id: "env_1", name: "daimon", description: "", created_at: "2030-01-01T00:00:00Z", updated_at: "2030-01-01T00:00:00Z", managed: false, cloud: true,
  network: "limited", open_network: false, allowed_hosts: [], allow_package_managers: true, allow_mcp_servers: true, packages: { pip: ["numpy"] },
}

const show = (e: EnvironmentDoc) => render(
  <QueryClientProvider client={new QueryClient()}><MemoryRouter><ConfirmProvider><EnvironmentForm env={e} missing={false} /></ConfirmProvider></MemoryRouter></QueryClientProvider>,
)

beforeEach(() => {
  vi.clearAllMocks()
  vi.stubGlobal("ResizeObserver", class { observe() {} unobserve() {} disconnect() {} })
})

describe("EnvironmentForm", () => {
  it("sends the whole package set and asks again when the network would open", async () => {
    vi.mocked(api.put)
      .mockRejectedValueOnce(new ApiError(428, "A sealed channel runs here."))
      .mockResolvedValueOnce(env as never)
    show(env)
    await userEvent.clear(screen.getByLabelText("pip"))
    await userEvent.type(screen.getByLabelText("pip"), "numpy{Enter}pandas")
    await userEvent.click(screen.getByRole("button", { name: "Save" }))
    await waitFor(() => expect(api.put).toHaveBeenCalledTimes(1))
    expect(vi.mocked(api.put).mock.calls[0]![1]).toMatchObject({ spec: { packages: { pip: ["numpy", "pandas"] } }, confirm_open_network: false })
    await userEvent.click(within(await screen.findByRole("alertdialog")).getByRole("button", { name: "Continue" }))
    await waitFor(() => expect(api.put).toHaveBeenCalledTimes(2))
    expect(vi.mocked(api.put).mock.calls[1]![1]).toMatchObject({ confirm_open_network: true })
  })

  it("keeps a package spec with a comma as one entry", async () => {
    vi.mocked(api.put).mockResolvedValue(env as never)
    show(env)
    await userEvent.clear(screen.getByLabelText("pip"))
    await userEvent.type(screen.getByLabelText("pip"), "numpy>=1.26,<2{Enter}{Enter}pandas")
    await userEvent.click(screen.getByRole("button", { name: "Save" }))
    await waitFor(() => expect(api.put).toHaveBeenCalledTimes(1))
    expect(vi.mocked(api.put).mock.calls[0]![1]).toMatchObject({ spec: { packages: { pip: ["numpy>=1.26,<2", "pandas"] } } })
  })

  it("sends the loaded version and says so when it changed meanwhile", async () => {
    vi.mocked(api.put).mockRejectedValue(new ApiError(409, "This environment changed since you opened it. Reload and try again."))
    show(env)
    await userEvent.click(screen.getByRole("button", { name: "Save" }))
    await waitFor(() => expect(api.put).toHaveBeenCalledTimes(1))
    expect(vi.mocked(api.put).mock.calls[0]![1]).toMatchObject({ expected_updated_at: "2030-01-01T00:00:00Z" })
    await waitFor(() => expect(toast.error).toHaveBeenCalledWith("Changed meanwhile. Reloaded."))
  })

  it("leaves a managed environment read-only", () => {
    show({ ...env, managed: true })
    expect(screen.queryByRole("button", { name: "Save" })).not.toBeInTheDocument()
    expect(screen.queryByRole("button", { name: "Archive" })).not.toBeInTheDocument()
  })
})
