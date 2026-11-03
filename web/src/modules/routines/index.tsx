import { CalendarClock } from "lucide-react"
import { Route } from "react-router"

import { lazyPage } from "../lazy"
import type { ModuleDef } from "../types"

const Routines = lazyPage(() => import("./Routines"), "Routines")

export const routines: ModuleDef = {
  key: "routines",
  nav: [{ to: "/routines", label: "Routines", icon: CalendarClock, cap: "routines.view", g: "o" }],
  routes: <Route path="/routines/*" element={<Routines />} />,
  actions: [{ label: "New routine", to: "/routines?routine=new", icon: CalendarClock, cap: "routines.manage" }],
}
