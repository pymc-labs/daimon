import { History, Plus, Workflow } from "lucide-react"
import { Route } from "react-router"

import { lazyPage } from "../lazy"
import type { ModuleDef } from "../types"

const WorkflowList = lazyPage(() => import("./List"), "WorkflowList")
const NewWorkflow = lazyPage(() => import("./New"), "NewWorkflow")
const WorkflowPage = lazyPage(() => import("./WorkflowPage"), "WorkflowPage")
const RunsPage = lazyPage(() => import("./run/RunsTable"), "RunsPage")
const RunPage = lazyPage(() => import("./run/RunPage"), "RunPage")

export const workflows: ModuleDef = {
  key: "workflows",
  nav: [
    { to: "/workflows", label: "Workflows", icon: Workflow, cap: "routines.view", g: "w" },
    { to: "/runs", label: "Runs", icon: History, cap: "routines.view", g: "u" },
  ],
  routes: (
    <>
      <Route path="/workflows" element={<WorkflowList />} />
      <Route path="/runs" element={<RunsPage />} />
      <Route path="/runs/attention" element={<RunsPage />} />
      <Route path="/workflows/new" element={<NewWorkflow />} />
      <Route path="/workflows/:id/runs/:n" element={<RunPage />} />
      <Route path="/workflows/:id/*" element={<WorkflowPage />} />
    </>
  ),
  gates: [{ to: "/workflows/new", cap: "routines.manage" }],
  actions: [{ label: "New workflow", to: "/workflows/new", icon: Plus, cap: "routines.manage" }],
}
