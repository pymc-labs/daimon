import { Navigate, Outlet, Route, Routes, useLocation } from "react-router"

import { AppShell } from "@/components/shell/AppShell"
import { ErrorState, Rows } from "@/components/QueryState"
import { ApiError } from "@/lib/api"
import { useMe } from "@/lib/hooks"
import type { ModuleKey } from "@/lib/types"
import { MODULES } from "@/modules/registry"
import { InviteLanding } from "@/pages/Invite"
import { SignIn } from "@/pages/SignIn"
import { NotFound, SignedOut } from "@/pages/Status"
import { Workspaces } from "@/pages/Workspaces"

function RequireAuth() {
  const me = useMe()
  const loc = useLocation()
  if (me.error instanceof ApiError && me.error.status === 401) return <Navigate to={`/signin?next=${encodeURIComponent(loc.pathname + loc.search)}`} replace />
  if (!me.data) return <div className="h-dvh">{me.isError ? <ErrorState error={me.error} onRetry={() => void me.refetch()} className="h-full justify-center" /> : <Rows className="mx-auto max-w-sm p-6 pt-[12vh]" />}</div>
  return <Outlet />
}

/** Routes of a module the server has not enabled for this person lead to chat. */
function ModuleGate({ k }: { k: ModuleKey }) {
  const { data } = useMe()
  return data?.modules.includes(k) ? <Outlet /> : <Navigate to="/chat" replace />
}

export default function App() {
  return (
    <Routes>
      <Route path="/signin" element={<SignIn />} />
      <Route path="/signed-out" element={<SignedOut />} />
      <Route path="/invite/:token" element={<InviteLanding />} />
      <Route element={<RequireAuth />}>
        <Route path="/workspaces" element={<Workspaces />} />
        <Route element={<AppShell />}>
          <Route index element={<Navigate to="/chat" replace />} />
          {MODULES.map((m) => <Route key={m.key} element={<ModuleGate k={m.key} />}>{m.routes}</Route>)}
          <Route path="/apps/:app/*" element={null} />
          <Route path="*" element={<NotFound />} />
        </Route>
      </Route>
    </Routes>
  )
}
