import { Plus } from "lucide-react"
import { Navigate, Route, Routes, useLocation } from "react-router"
import { useParam } from "@/lib/filters"
import { Page } from "@/components/shell/Page"
import { Button } from "@/components/ui/button"
import { AgentAccessView } from "./AgentAccess"
import { Audit } from "./Audit"
import { Invites, NewInvite } from "./Invites"
import { People } from "./People"
import { Workspace } from "./Workspace"

const TABS = [
  { label: "People", to: "/admin", end: true }, { label: "Invites", to: "/admin/invites" },
  { label: "Agent access", to: "/admin/agents" }, { label: "Audit log", to: "/admin/audit" }, { label: "Workspace", to: "/admin/workspace" },
]


export function Admin() {
  const { pathname } = useLocation()
  const [, setDialog] = useParam("dialog")
  const tab = TABS.find((t) => t.to === pathname && t.to !== "/admin")
  return (
    <Page
      crumbs={[{ label: "Admin", to: "/admin" }, ...(tab ? [{ label: tab.label }] : [])]}
      tabs={TABS}
      actions={pathname.startsWith("/admin/workspace") ? null : <Button size="sm" onClick={() => setDialog("invite")}><Plus />Invite</Button>}
    >
      <Routes>
        <Route index element={<People />} />
        <Route path="invites" element={<Invites />} />
        <Route path="agents" element={<AgentAccessView />} />
        <Route path="audit" element={<Audit />} />
        <Route path="workspace" element={<Workspace />} />
        <Route path="*" element={<Navigate to="/admin" replace />} />
      </Routes>
      <NewInvite />
    </Page>
  )
}
