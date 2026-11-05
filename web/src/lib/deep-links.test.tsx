import { createRoutesFromElements, matchRoutes, Route } from "react-router"
import { describe, expect, it } from "vitest"

import { MODULES } from "@/modules/registry"
import { DEEP_LINKS, deepLink, type LinkKey } from "./deep-links"

const routes = createRoutesFromElements(<Route>{MODULES.map((m) => m.routes)}</Route>)

describe("deep links", () => {
  it("fills a path segment and encodes the opened item", () => {
    expect(deepLink("agent", "ag 1", "skill:s1")).toBe("/agents/ag%201?node=skill%3As1")
    expect(deepLink("channel", null, "19:a@thread.tacv2")).toBe("/channels?channel=19%3Aa%40thread.tacv2")
    expect(deepLink("skill", "s1")).toBe("/agents/skills/s1")
    expect(deepLink("spend")).toBe("/credits/spend")
  })

  it("names only pages a module serves, under that module", () => {
    for (const [key, link] of Object.entries(DEEP_LINKS) as [LinkKey, (typeof DEEP_LINKS)[LinkKey]][]) {
      const matched = matchRoutes(routes, link.path.replace("{id}", "x"))
      const last = matched?.at(-1)?.route.path
      expect(last, `${key} (${link.path}) matches no route`).toBeDefined()
      expect(last, `${key} (${link.path}) falls to a catch-all`).not.toBe("*")
      const owner = MODULES.find((m) => m.nav.some((n) => link.path === n.to || link.path.startsWith(`${n.to}/`)))
      expect(owner?.key, `${key} belongs to the ${link.module} module`).toBe(link.module)
    }
  })
})
