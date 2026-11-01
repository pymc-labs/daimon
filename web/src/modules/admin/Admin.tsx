import { Plus } from "lucide-react"
import { Navigate, Route, Routes, useLocation } from "react-router"
import { useParam } from "@/lib/filters"
import { useCan } from "@/lib/hooks"
import type { Capability } from "@/lib/types"
import { Page } from "@/components/shell/Page"
import { Button } from "@/components/ui/button"
import { AgentAccessView } from "./AgentAccess"
import { Audit } from "./Audit"
import { Invites, NewInvite } from "./Invites"
import { People } from "./People"
import { Workspace } from "./Workspace"

const TABS: { label: string; to: string; end?: boolean; cap: Capability }[] = [
  { label: "People", to: "/admin", end: true, cap: "people.manage" }, { label: "Invites", to: "/admin/invites", cap: "people.manage" },
  { label: "Agent access", to: "/admin/agents", cap: "workspace.manage" }, { label: "Audit log", to: "/admin/audit", cap: "audit.view" },
  { label: "Workspace", to: "/admin/workspace", cap: "workspace.manage" },
]

export function Admin() {
  const { pathname } = useLocation()
  const can = useCan()
  const [, setDialog] = useParam("dialog")
  const tabs = TABS.filter((t) => can(t.cap))
  const first = tabs[0]?.to
  const tab = tabs.find((t) => t.to === pathname && t.to !== "/admin")
  // A tab this person may not open leads to the first one they may.
  const here = TABS.find((t) => t.to === pathname.replace(/\/$/, ""))
  if (first && here && !can(here.cap)) return <Navigate to={first} replace />
  const page = (to: string, cap: Capability, el: React.ReactElement) =>
    <Route path={to} element={can(cap) ? el : <Navigate to={first ?? "/chat"} replace />} />
  return (
    <Page
      crumbs={[{ label: "Admin", to: "/admin" }, ...(tab ? [{ label: tab.label }] : [])]}
      tabs={tabs}
      actions={can("people.manage") && !pathname.startsWith("/admin/workspace") ? <Button size="sm" onClick={() => setDialog("invite")}><Plus />Invite</Button> : null}
    >
      <Routes>
        {page("", "people.manage", <People />)}
        {page("invites", "people.manage", <Invites />)}
        {page("agents", "workspace.manage", <AgentAccessView />)}
        {page("audit", "audit.view", <Audit />)}
        {page("workspace", "workspace.manage", <Workspace />)}
        <Route path="*" element={<Navigate to={first ?? "/chat"} replace />} />
      </Routes>
      {can("people.manage") && <NewInvite />}
    </Page>
  )
}
