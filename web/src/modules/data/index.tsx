import { Database } from "lucide-react"
import { Route } from "react-router"

import { lazyPage } from "../lazy"
import type { ModuleDef } from "../types"

const Data = lazyPage(() => import("./Data"), "Data")

export const data: ModuleDef = {
  key: "data",
  nav: [{ to: "/data", label: "Data", icon: Database, cap: "data.view", g: "d" }],
  routes: <Route path="/data/*" element={<Data />} />,
}
