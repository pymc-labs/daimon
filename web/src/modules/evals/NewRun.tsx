import { useMutation, useQuery } from "@tanstack/react-query"
import { useState } from "react"
import { useNavigate, useParams, useSearchParams } from "react-router"

import { Facet } from "@/components/filters"
import { QueryState } from "@/components/QueryState"
import { Page } from "@/components/shell/Page"
import { Checkbox } from "@/components/ui/checkbox"
import { Input } from "@/components/ui/input"
import { Skeleton } from "@/components/ui/skeleton"
import { api } from "@/lib/api"
import { shortDate } from "@/lib/format"
import { cn } from "@/lib/utils"
import { E, useArms, useCases, useIsAdmin, useSuite, useSuites } from "./api"
import { ApproveBar, EstimateView } from "./Approve"
import { canEdit } from "./bits"
import type { Run, RunDraft, RunEstimate } from "./types"

const Section = ({ title, children }: { title: string; children: React.ReactNode }) => <section className="space-y-2"><h2 className="text-sm font-medium">{title}</h2>{children}</section>

/** A card the whole of which is the checkbox. */
function Choice({ checked, onChange, title, detail }: { checked: boolean; onChange: (v: boolean) => void; title: string; detail?: React.ReactNode }) {
  return (
    <label className={cn("flex cursor-pointer items-center gap-3 rounded-lg border p-3 text-sm hover:bg-accent/50", checked && "border-border-strong bg-accent/40")}>
      <Checkbox checked={checked} onCheckedChange={(v) => onChange(v === true)} aria-label={title} />
      <span className="min-w-0 flex-1"><span className="block truncate font-medium">{title}</span>{detail && <span className="block truncate text-xs text-muted-foreground">{detail}</span>}</span>
    </label>
  )
}

export function NewRun() {
  const { suite = "" } = useParams()
  const [params] = useSearchParams()
  const version = params.get("version") ? Number(params.get("version")) : undefined
  const go = useNavigate()
  const info = useSuite(suite, version)
  const cases = useCases(suite, version)
  const suites = useSuites().data
  const role = suites?.find((x) => x.id === suite)?.role
  const arms = useArms(!!role && canEdit(role))
  const admin = useIsAdmin()
  const [chosen, setChosen] = useState<string[]>([])
  const [judges, setJudges] = useState<string[] | null>(null)
  const [repeats, setRepeats] = useState("1")
  const [splits, setSplits] = useState<string[]>([])
  const [tags, setTags] = useState<string[]>([])
  const s = info.data
  const judgeIds = judges ?? s?.defaults?.judges?.map((j) => j.id) ?? []
  const picked = (cases.data ?? []).filter((c) => (!tags.length || tags.some((t) => c.tags?.includes(t))) && (!splits.length || splits.includes(s?.cases.find((x) => x.id === c.id)?.split ?? "test")))
  const draft: RunDraft | null = s && chosen.length && picked.length
    ? { suite: { id: s.id, version: s.version }, case_ids: picked.length === cases.data?.length ? undefined : picked.map((c) => c.id), arm_ids: chosen, repeats: Math.max(1, Math.min(20, Math.floor(Number(repeats)) || 1)), judge_ids: judgeIds }
    : null
  const estimate = useQuery({ queryKey: ["evals", "estimate", draft], queryFn: () => api.post<RunEstimate>(`${E}/runs/estimate`, draft), enabled: !!draft, retry: false })
  const start = useMutation({ mutationFn: (cap: number) => api.post<Run>(`${E}/runs`, { ...draft, cap_usd: cap }), onSuccess: (r) => go(`/evals/runs/${r.id}`) })
  const toggle = (list: string[], id: string, on: boolean) => (on ? [...list, id] : list.filter((x) => x !== id))
  const tagOptions = [...new Set((cases.data ?? []).flatMap((c) => c.tags ?? []))].map((t) => ({ value: t, label: t }))
  if (suites && !canEdit(role ?? "viewer")) return <Page crumbs={[{ label: "Evals", to: "/evals" }, { label: "New run" }]}><div className="py-16 text-center text-sm text-muted-foreground">Only editors and owners can start runs. Ask an owner.</div></Page>
  return (
    <Page crumbs={[{ label: "Evals", to: "/evals" }, { label: s?.name ?? suite, to: `/evals/suites/${suite}` }, { label: "New run" }]}>
      <div className="mx-auto max-w-2xl space-y-8">
        <Section title="Cases">
          <div className="flex flex-wrap items-center gap-2">
            <Facet label="Split" value={splits} onChange={setSplits} options={["dev", "test", "holdout"].map((v) => ({ value: v, label: v[0]!.toUpperCase() + v.slice(1) }))} />
            {tagOptions.length > 0 && <Facet label="Tag" value={tags} onChange={setTags} options={tagOptions} />}
            <span className="ml-auto text-sm text-muted-foreground tabular">{picked.length} of {cases.data?.length ?? 0}</span>
          </div>
        </Section>
        <Section title="Arms">
          <QueryState query={arms} empty="No arms" skeleton={<Skeleton className="h-14" />}>
            {(list) => (
              <div className="grid gap-2 sm:grid-cols-2">
                {list.map((a) => (
                  <Choice key={a.id} checked={chosen.includes(a.id)} onChange={(v) => setChosen(toggle(chosen, a.id, v))} title={a.label}
                    detail={a.kind === "live" ? `Live, ${a.live?.model}${a.live?.effort ? `, ${a.live.effort}` : ""}` : `Baseline${a.baseline?.recorded_from ? `, ${shortDate(a.baseline.recorded_from)}` : ""}`} />
                ))}
              </div>
            )}
          </QueryState>
        </Section>
        <div className="grid gap-8 sm:grid-cols-[120px_1fr]">
          <Section title="Repeats"><Input type="number" min={1} max={20} value={repeats} onChange={(e) => setRepeats(e.target.value)} aria-label="Repeats" className="h-8 tabular" /></Section>
          {!!s?.defaults?.judges?.length && (
            <Section title="Judges">
              <div className="grid gap-2">
                {s.defaults.judges.map((j) => <Choice key={j.id} checked={judgeIds.includes(j.id)} onChange={(v) => setJudges(toggle(judgeIds, j.id, v))} title={j.id} detail={`${j.kind[0]!.toUpperCase()}${j.kind.slice(1)}, ${j.models.length} ${j.models.length === 1 ? "model" : "models"}`} />)}
              </div>
            </Section>
          )}
        </div>
        <Section title="Estimate">
          {!draft ? <div className="rounded-lg border py-8 text-center text-sm text-muted-foreground">Pick at least one arm</div> : (
            <QueryState query={estimate} skeleton={<Skeleton className="h-40" />}>{(e) => <EstimateView estimate={e} arms={arms.data} />}</QueryState>
          )}
        </Section>
        {start.error && <p role="alert" className="text-sm text-destructive">{start.error.message}</p>}
        {estimate.data && draft && <div className="border-t pt-4"><ApproveBar estimate={estimate.data} limit={estimate.data.admin_approval_usd} admin={admin} pending={start.isPending} onApprove={(cap) => start.mutate(cap)} /></div>}
      </div>
    </Page>
  )
}
