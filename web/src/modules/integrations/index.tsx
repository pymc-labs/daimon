import { Plug } from "lucide-react"
import { Route } from "react-router"

import { lazyPage } from "../lazy"
import type { ModuleDef } from "../types"

const Integrations = lazyPage(() => import("./Integrations"), "Integrations")

export const integrations: ModuleDef = {
  key: "integrations",
  nav: [{ to: "/integrations", label: "Integrations", icon: Plug, cap: "integrations.view", g: "i" }],
  routes: <Route path="/integrations/*" element={<Integrations />} />,
}
