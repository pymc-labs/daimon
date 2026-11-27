import { FlaskConical } from "lucide-react"
import { Route } from "react-router"

import { lazyPage } from "../lazy"
import type { ModuleDef } from "../types"

const Evals = lazyPage(() => import("./Evals"), "Evals")
const Suite = lazyPage(() => import("./Suite"), "Suite")
const Case = lazyPage(() => import("./Case"), "CasePage")
const NewRun = lazyPage(() => import("./NewRun"), "NewRun")
const Run = lazyPage(() => import("./Run"), "Run")
const Compare = lazyPage(() => import("./Compare"), "Compare")
const Review = lazyPage(() => import("./Review"), "Review")

export const evals: ModuleDef = {
  key: "evals",
  nav: [{ to: "/evals", label: "Evals", icon: FlaskConical, cap: "evals.use", g: "e" }],
  routes: (
    <>
      <Route path="/evals" element={<Evals />} />
      <Route path="/evals/suites/:suite" element={<Suite />} />
      <Route path="/evals/suites/:suite/cases/:case" element={<Case />} />
      <Route path="/evals/suites/:suite/run" element={<NewRun />} />
      <Route path="/evals/runs/:run" element={<Run />} />
      <Route path="/evals/runs/:run/compare" element={<Compare />} />
      <Route path="/evals/queues/:queue" element={<Review />} />
    </>
  ),
}
