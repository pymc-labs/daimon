import { useQuery } from "@tanstack/react-query"
import { Plus } from "lucide-react"
import { Navigate, Route, Routes, useLocation } from "react-router"
import { useParam } from "@/lib/filters"
import { Page } from "@/components/shell/Page"
import { Button } from "@/components/ui/button"
import { api } from "@/lib/api"
import { useCan } from "@/lib/hooks"
import type { Overview } from "./types"
import { Allowances, NewAllowance } from "./Allowances"
import { MineTab } from "./Mine"
import { OverviewTab } from "./Overview"
import { SpendTab } from "./Spend"
import { TopUp } from "./TopUp"

export function Credits() {
  const can = useCan()
  if (!can("credits.manage")) return <Page crumbs={[{ label: "Credits" }]}><MineTab /></Page>
  return <AdminCredits />
}

function AdminCredits() {
  const { pathname } = useLocation()
  const [dialog, setDialog] = useParam("dialog")
  const { data: overview } = useQuery({ queryKey: ["credits", "overview"], queryFn: () => api.get<Overview>("/api/credits/overview") })
  const action = pathname === "/credits" ? overview?.topup && <Button size="sm" onClick={() => setDialog("topup")}><Plus />Top up</Button>
    : pathname === "/credits/allowances" ? <Button size="sm" onClick={() => setDialog("allowance")}><Plus />New allowance</Button> : null
  const tab = { "/credits/spend": "Spend", "/credits/allowances": "Allowances", "/credits/usage": "My usage" }[pathname]
  return (
    <Page
      crumbs={[{ label: "Credits", to: "/credits" }, ...(tab ? [{ label: tab }] : [])]}
      actions={action}
      tabs={[
        { label: "Overview", to: "/credits", end: true }, { label: "Spend", to: "/credits/spend" },
        { label: "Allowances", to: "/credits/allowances" }, { label: "My usage", to: "/credits/usage" },
      ]}
    >
      <Routes>
        <Route index element={<OverviewTab />} />
        <Route path="spend" element={<SpendTab />} />
        <Route path="allowances" element={<Allowances />} />
        <Route path="usage" element={<MineTab />} />
        <Route path="*" element={<Navigate to="/credits" replace />} />
      </Routes>
      {overview?.topup && <TopUp mode={overview.topup} open={dialog === "topup"} onOpenChange={(o) => setDialog(o ? "topup" : null)} />}
      <NewAllowance open={dialog === "allowance"} onOpenChange={(o) => setDialog(o ? "allowance" : null)} />
    </Page>
  )
}
