import { Navigate, Route, Routes } from "react-router"
import { Page } from "@/components/shell/Page"
import { useCan } from "@/lib/hooks"
import { DataSources } from "./DataSources"
import { Github } from "./Github"
import { Mcp } from "./Mcp"
import { Tokens } from "./Tokens"
import { WorkspacesTab } from "./Workspaces"

const TABS = [
  { label: "Chat apps", to: "/integrations", end: true }, { label: "GitHub", to: "/integrations/github" },
  { label: "Tool servers", to: "/integrations/mcp" }, { label: "API tokens", to: "/integrations/tokens" },
]
const DATA = { label: "Data sources", to: "/integrations/data" }

export function Integrations() {
  const data = useCan()("data.manage")
  return (
    <Page crumbs={[{ label: "Integrations", to: "/integrations" }]} tabs={data ? [...TABS, DATA] : TABS}>
      <div className="mx-auto max-w-[1040px]">
        <Routes>
          <Route index element={<WorkspacesTab />} />
          <Route path="github" element={<Github />} />
          <Route path="mcp" element={<Mcp />} />
          <Route path="tokens" element={<Tokens />} />
          {data && <Route path="data" element={<DataSources />} />}
          <Route path="*" element={<Navigate to="/integrations" replace />} />
        </Routes>
      </div>
    </Page>
  )
}
