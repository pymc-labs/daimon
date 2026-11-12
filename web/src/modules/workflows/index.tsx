import { Plus, Workflow } from "lucide-react"
import { Route } from "react-router"

import { lazyPage } from "../lazy"
import type { ModuleDef } from "../types"

const WorkflowList = lazyPage(() => import("./List"), "WorkflowList")
const NewWorkflow = lazyPage(() => import("./New"), "NewWorkflow")
const WorkflowPage = lazyPage(() => import("./WorkflowPage"), "WorkflowPage")

export const workflows: ModuleDef = {
  key: "workflows",
  nav: [{ to: "/workflows", label: "Workflows", icon: Workflow, cap: "routines.view", g: "w" }],
  routes: (
    <>
      <Route path="/workflows" element={<WorkflowList />} />
      <Route path="/workflows/new" element={<NewWorkflow />} />
      <Route path="/workflows/:id/*" element={<WorkflowPage />} />
    </>
  ),
  gates: [{ to: "/workflows/new", cap: "routines.manage" }],
  actions: [{ label: "New workflow", to: "/workflows/new", icon: Plus, cap: "routines.manage" }],
}
