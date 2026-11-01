import { ShieldCheck, UserPlus } from "lucide-react"
import { Route } from "react-router"

import { lazyPage } from "../lazy"
import type { ModuleDef } from "../types"

const Admin = lazyPage(() => import("./Admin"), "Admin")

export const admin: ModuleDef = {
  key: "admin",
  nav: [{ to: "/admin", label: "Admin", icon: ShieldCheck, cap: "people.manage", g: "x" }],
  routes: <Route path="/admin/*" element={<Admin />} />,
  actions: [{ label: "Invite", to: "/admin/invites?dialog=invite", icon: UserPlus, cap: "people.manage" }],
}
