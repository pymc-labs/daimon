import { Bot, Plus } from "lucide-react"
import { Route } from "react-router"

import { lazyPage } from "../lazy"
import type { ModuleDef } from "../types"

const AgentPage = lazyPage(() => import("./AgentPage"), "AgentPage")
const AgentList = lazyPage(() => import("./List"), "AgentList")
const NewAgent = lazyPage(() => import("./New"), "NewAgent")

export const agents: ModuleDef = {
  key: "agents",
  nav: [{ to: "/agents", label: "Agents", icon: Bot, cap: "agents.view", g: "a" }],
  routes: (
    <>
      <Route path="/agents" element={<AgentList />} />
      <Route path="/agents/new" element={<NewAgent />} />
      <Route path="/agents/:id" element={<AgentPage />} />
    </>
  ),
  actions: [{ label: "New agent", to: "/agents/new", icon: Plus, cap: "agents.create" }],
}
