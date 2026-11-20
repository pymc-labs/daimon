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
import { isBlindRun, isCase } from "./bits"
import { useCase, useCompare, useItem, useRole, useRun } from "./api"
import { Comments } from "./Comments"
import { ItemColumn } from "./Item"
import type { Anchor, Case, CaseView, CompareColumn } from "./types"

function Column({ col, other, title, blind, diff, c, onPin }: { col: CompareColumn; other?: CompareColumn; title?: string; blind: boolean; diff: boolean; c?: Case | CaseView; onPin?: (a: Anchor) => void }) {
  const item = useItem(col.item_id, blind)
  const before = useItem(other?.item_id ?? "", blind)
  if (item.error) return <p role="alert" className="text-sm text-destructive">{item.error.message}</p>
  if (!item.data) return <Skeleton className="h-64" />
  return (
    <ItemColumn label={col.label} title={title} item={item.data} scores={col.scores} blind={blind} platform={!c ? "web" : isCase(c) ? c.surface.platform : c.platform} case={c} onPin={onPin}
      diff={diff && other ? { before: other.reply ?? "", after: col.reply ?? "", outputs: before.data?.outputs } : undefined} />
  )
}

/** One case across the run's arms, side by side or one arm at a time, blind or with names. Reviewers always get it blind. */
export function Compare() {
  const { run: id = "" } = useParams()
  const run = useRun(id).data
  const forced = useRole(run?.suite.id ?? "") === "reviewer"
  const [picked, setCase] = useParam("case")
  const [repeat, setRepeat] = useParam("repeat")
  const [mode, setMode] = useParam("mode")
  const [blindParam, setBlind] = useParam("blind")
  const [diffParam, setDiff] = useParam("diff")
  const [arm, setArm] = useParam("column")
  const [, openComments] = useParam("comments")
  const [anchor, setAnchor] = useState<Anchor | null>(null)
  const data = useCompare(id, forced || blindParam === "1")
  // The server's word on blinding wins over the address and the role.
  const blind = data.data?.blind ?? (forced || blindParam === "1")
  const fixed = forced || (blind && blindParam !== "1")
  const rows = data.data?.rows ?? []
  const cases = rows.filter((r, i) => rows.findIndex((x) => x.case_id === r.case_id) === i)
  const current = picked || cases[0]?.case_id || ""
  const repeats = rows.filter((r) => r.case_id === current)
  const row = repeats.find((r) => String(r.repeat) === repeat) ?? repeats[0]
  const single = mode === "single"
  const cols = row?.columns ?? []
  const diff = diffParam === "1" && !single
  const shown = single ? cols.filter((x) => x.label === (arm || cols[0]?.label)) : cols
  const c = useCase(run?.suite.id ?? "", current)
  const baseline = (label: string) => !!run && !isBlindRun(run) && run.arms.find((a) => a.label === label)?.kind === "baseline"
  return (
    <Page crumbs={blind ? [{ label: "Evals", to: "/evals" }, { label: "Review" }, { label: "Compare" }] : [{ label: "Evals", to: "/evals" }, { label: run && !isBlindRun(run) ? run.name ?? id : id, to: `/evals/runs/${id}` }, { label: "Compare" }]} flush className="flex flex-col">
      <div className="flex min-h-0 flex-1">
        <nav aria-label="Cases" className="hidden w-64 shrink-0 overflow-y-auto border-r p-2 md:block">
          {cases.map((x) => (
            <button key={x.case_id} onClick={() => setCase(x.case_id)} className={cn("flex min-h-8 w-full items-center rounded-md px-2 py-1 text-left text-sm hover:bg-accent", x.case_id === current && "bg-accent font-medium")}>
              <span className="line-clamp-2">{x.title}</span>
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
                {cols.map((x) => <Button key={x.label} size="sm" variant={x.label === (arm || cols[0]?.label) ? "secondary" : "ghost"} onClick={() => setArm(x.label)}>{x.label}</Button>)}
              </div>
            )}
            {repeats.length > 1 && (
              <div className="flex gap-1" role="group" aria-label="Repeat">
                {repeats.map((x) => <Button key={x.repeat} size="sm" variant={x === row ? "secondary" : "ghost"} onClick={() => setRepeat(String(x.repeat))}>Repeat {x.repeat + 1}</Button>)}
              </div>
            )}
            {!fixed && <label className="flex items-center gap-2">Blind<Switch checked={blind} onCheckedChange={(v) => setBlind(v ? "1" : null)} aria-label="Blind" /></label>}
            {!single && cols.length > 1 && <label className="flex items-center gap-2">Diff<Switch checked={diff} onCheckedChange={(v) => setDiff(v ? "1" : null)} aria-label="Diff" /></label>}
            {!blind && <Button variant="outline" size="sm" className="ml-auto" onClick={() => openComments("open")}><MessageSquare />Comments</Button>}
          </div>
          <div className="p-4 lg:p-6">
            <QueryState query={data} empty="No results for this case" isEmpty={(d) => !d.rows.length} skeleton={<Skeleton className="h-64" />}>
              {() => (
                <div className={cn("grid gap-4", shown.length > 1 && "lg:grid-cols-2", shown.length > 2 && "xl:grid-cols-3")}>
                  {shown.map((col, i) => (
                    <Column key={col.item_id} col={col} title={baseline(col.label) ? `${col.label}, baseline` : col.label} other={diff && i > 0 ? shown[0] : undefined} blind={blind} diff={diff} c={c.data}
                      onPin={blind ? undefined : (a) => { setAnchor(a); openComments("open") }} />
                  ))}
                </div>
              )}
            </QueryState>
          </div>
        </div>
      </div>
      {!blind && (
        <Drawer param="comments" title="Comments">
          {() => (
            <div className="flex flex-1 flex-col">
              <PanelHead title="Comments" subtitle={row?.title} />
              <PanelSection><Comments scope={{ run: id, case: current }} anchor={anchor} onClear={() => setAnchor(null)} /></PanelSection>
            </div>
          )}
        </Drawer>
      )}
    </Page>
  )
}
