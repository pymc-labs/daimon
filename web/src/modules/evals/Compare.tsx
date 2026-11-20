import { MessageSquare } from "lucide-react"
import { useState } from "react"
import { useParams } from "react-router"

import { Drawer } from "@/components/Drawer"
import { QueryState } from "@/components/QueryState"
import { Page } from "@/components/shell/Page"
import { PanelHead, PanelSection } from "@/components/panel"
import { Button } from "@/components/ui/button"
import { Switch } from "@/components/ui/switch"
import { Skeleton } from "@/components/ui/skeleton"
import { useParam } from "@/lib/filters"
import { cn } from "@/lib/utils"
import { useCase, useCases, useCompare, useRun, useRunItems } from "./api"
import { Comments } from "./Comments"
import { ItemColumn } from "./Item"
import type { Anchor } from "./types"

/** One case across the run's arms, side by side or one arm at a time, blind or with names. */
export function Compare() {
  const { run: id = "" } = useParams()
  const run = useRun(id).data
  const items = useRunItems(id, false).data ?? []
  const cases = useCases(run?.suite.id ?? "", run?.suite.version).data ?? []
  const ids = [...new Set(items.map((i) => i.case_id))]
  const [picked, setCase] = useParam("case")
  const [mode, setMode] = useParam("mode")
  const [blindParam, setBlind] = useParam("blind")
  const [diffParam, setDiff] = useParam("diff")
  const [arm, setArm] = useParam("column")
  const [, openComments] = useParam("comments")
  const [anchor, setAnchor] = useState<Anchor | null>(null)
  const current = picked || ids[0] || ""
  const blind = blindParam === "1", single = mode === "single", diff = diffParam === "1" && !single
  const data = useCompare(id, current, blind)
  const c = useCase(run?.suite.id ?? "", current)
  const cols = data.data?.columns ?? []
  const shown = single ? cols.filter((x) => x.label === (arm || cols[0]?.label)) : cols
  return (
    <Page crumbs={[{ label: "Evals", to: "/evals" }, { label: run?.name ?? id, to: `/evals/runs/${id}` }, { label: "Compare" }]} flush className="flex flex-col">
      <div className="flex min-h-0 flex-1">
        <nav aria-label="Cases" className="hidden w-64 shrink-0 overflow-y-auto border-r p-2 md:block">
          {ids.map((x) => (
            <button key={x} onClick={() => setCase(x)} className={cn("flex min-h-8 w-full items-center rounded-md px-2 py-1 text-left text-sm hover:bg-accent", x === current && "bg-accent font-medium")}>
              <span className="line-clamp-2">{cases.find((k) => k.id === x)?.title ?? x}</span>
            </button>
          ))}
        </nav>
        <div className="min-w-0 flex-1 overflow-y-auto">
          <div className="flex flex-wrap items-center gap-x-5 gap-y-2 border-b px-4 py-2 text-sm lg:px-6">
            <div className="flex gap-1" role="group" aria-label="Layout">
              <Button size="sm" variant={single ? "ghost" : "secondary"} aria-pressed={!single} onClick={() => setMode(null)}>Side by side</Button>
              <Button size="sm" variant={single ? "secondary" : "ghost"} aria-pressed={single} onClick={() => setMode("single")}>Single</Button>
            </div>
            {single && cols.length > 1 && (
              <div className="flex gap-1" role="group" aria-label="Column">
                {cols.map((x) => <Button key={x.label} size="sm" variant={x.label === (arm || cols[0]?.label) ? "secondary" : "ghost"} onClick={() => setArm(x.label)}>{x.arm?.label ?? x.label}</Button>)}
              </div>
            )}
            <label className="flex items-center gap-2">Blind<Switch checked={blind} onCheckedChange={(v) => setBlind(v ? "1" : null)} aria-label="Blind" /></label>
            {!single && cols.length > 1 && <label className="flex items-center gap-2">Diff<Switch checked={diff} onCheckedChange={(v) => setDiff(v ? "1" : null)} aria-label="Diff" /></label>}
            <Button variant="outline" size="sm" className="ml-auto" onClick={() => openComments("open")}><MessageSquare />Comments</Button>
          </div>
          <div className="p-4 lg:p-6">
            <QueryState query={data} empty="No results for this case" isEmpty={(d) => !d.columns.length} skeleton={<Skeleton className="h-64" />}>
              {() => (
                <div className={cn("grid gap-4", shown.length > 1 && "lg:grid-cols-2", shown.length > 2 && "xl:grid-cols-3")}>
                  {shown.map((col, i) => (
                    <ItemColumn key={col.item.id} col={col} other={diff && i > 0 ? shown[0] : undefined} diff={diff} platform={c.data?.surface.platform ?? "web"} case={c.data}
                      onPin={(a) => { setAnchor(a); openComments("open") }} />
                  ))}
                </div>
              )}
            </QueryState>
          </div>
        </div>
      </div>
      <Drawer param="comments" title="Comments">
        {() => (
          <div className="flex flex-1 flex-col">
            <PanelHead title="Comments" subtitle={c.data?.title} />
            <PanelSection><Comments caseId={current} itemId={cols[0]?.item.id} anchor={anchor} onClear={() => setAnchor(null)} /></PanelSection>
          </div>
        )}
      </Drawer>
    </Page>
  )
}
