import { Hash } from "lucide-react"
import { Route } from "react-router"

import { lazyPage } from "../lazy"
import type { ModuleDef } from "../types"

const Channels = lazyPage(() => import("./List"), "Channels")

export const channels: ModuleDef = {
  key: "channels",
  nav: [{ to: "/channels", label: "Channels", icon: Hash, cap: "channels.view", g: "h" }],
  routes: <Route path="/channels" element={<Channels />} />,
}
