import { useEffect, useRef } from "react"
import { Navigate, useLocation, useNavigate, useParams } from "react-router"

import { Rows } from "@/components/QueryState"
import { useMe, useSwitchWorkspace } from "@/lib/hooks"
import { safeNext } from "@/lib/safe-next"

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i

/** `/w/<workspace>/<page>`: a link from chat. Opens the page in that workspace, switching first when
 * the person is in it; otherwise the workspace picker. The page goes through `safeNext`, so it stays on this site. */
export function WorkspaceLink() {
  const { workspace = "", "*": rest = "" } = useParams()
  const { search, hash } = useLocation()
  const navigate = useNavigate()
  const me = useMe()
  const to = safeNext(`/${rest}${search}${hash}`)
  const picker = `/workspaces?next=${encodeURIComponent(to)}`
  const here = me.data?.workspace.id === workspace
  const member = UUID.test(workspace) && !!me.data?.workspaces.some((w) => w.id === workspace)
  const sent = useRef(false)
  const { mutate } = useSwitchWorkspace(to, { replace: true, onError: () => navigate(picker, { replace: true }) })
  useEffect(() => {
    if (!me.data || here || !member || sent.current) return
    sent.current = true
    mutate(workspace)
  }, [me.data, here, member, workspace, mutate])
  if (!me.data) return null
  if (here) return <Navigate to={to} replace />
  if (!member) return <Navigate to={picker} replace />
  return <Rows className="mx-auto max-w-sm p-6 pt-[12vh]" />
}
