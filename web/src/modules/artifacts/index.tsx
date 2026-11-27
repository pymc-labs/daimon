import { AppWindow } from "lucide-react"
import { Route } from "react-router"

import { lazyPage } from "../lazy"
import type { ModuleDef } from "../types"

const Artifacts = lazyPage(() => import("./Artifacts"), "Artifacts")
const ArtifactPage = lazyPage(() => import("./ArtifactPage"), "ArtifactPage")

export const artifacts: ModuleDef = {
  key: "artifacts",
  nav: [{ to: "/artifacts", label: "Artifacts", icon: AppWindow, cap: "artifacts.view", g: "t" }],
  routes: (
    <>
      <Route path="/artifacts" element={<Artifacts />} />
      <Route path="/artifacts/:id" element={<ArtifactPage />} />
    </>
  ),
}
