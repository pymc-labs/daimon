import { Wallet } from "lucide-react"
import { Route } from "react-router"

import { lazyPage } from "../lazy"
import type { ModuleDef } from "../types"

const Credits = lazyPage(() => import("./Credits"), "Credits")

export const credits: ModuleDef = {
  key: "credits",
  nav: [{ to: "/credits", label: "Credits", icon: Wallet, cap: "credits.view_own", g: "r" }],
  routes: <Route path="/credits/*" element={<Credits />} />,
  actions: [
    { label: "Top up", to: "/credits?dialog=topup", icon: Wallet, cap: "credits.manage" },
    { label: "New allowance", to: "/credits/allowances?dialog=allowance", icon: Wallet, cap: "credits.manage" },
  ],
}
