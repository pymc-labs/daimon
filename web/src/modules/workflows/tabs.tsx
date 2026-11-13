import { Ago, Avatar, Money, Mono } from "@/components/common"
import { Facts, Tag } from "@/components/panel"
import { QueryState } from "@/components/QueryState"
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table"
import { absolute } from "@/lib/format"
import { useVersions } from "./api"
import type { Person, Trigger, Workflow } from "./types"
import { cronLabel, TRIGGER } from "./kinds"

const H = "pl-4 lg:pl-6", T = "pr-4 text-right lg:pr-6"

function triggerFacts(t: Trigger): [string, React.ReactNode][] {
  switch (t.kind) {
    case "schedule": return [["When", cronLabel(t.cron)], ["Cron", <Mono key="c">{t.cron}</Mono>], ["Time zone", t.timezone ?? "UTC"], ["Missed runs", t.catch_up === "run-once" ? "Run once" : "Skip"]]
    case "webhook": return [["Verify", t.verify ?? "None"], ["Inputs", t.inputs && <Mono key="i">{t.inputs}</Mono>], ["Responds", t.respond === "result" ? "With the result" : "Accepted"]]
    case "event": return [["Source", t.source], ["Match", t.match && <Mono key="m">{t.match}</Mono>]]
    default: return [["Who", "Anyone who can run"]]
  }
}

export function TriggersTab({ wf }: { wf: Workflow }) {
  const ts = wf.spec.triggers ?? []
  if (!ts.length) return <p className="px-6 pt-6 text-sm text-muted-foreground">No triggers</p>
  return (
    <div className="grid max-w-5xl gap-4 px-4 py-6 md:grid-cols-2 lg:px-6">
      {ts.map((t) => {
        const K = TRIGGER[t.kind]
        return (
          <section key={t.id} className="rounded-xl border bg-background">
            <h3 className="flex h-11 items-center gap-2 border-b px-4 text-sm font-medium"><K.icon className="size-4 text-muted-foreground" />{K.label}<Mono className="ml-auto text-xs text-faint">{t.id}</Mono></h3>
            <div className="px-4 py-3"><Facts rows={triggerFacts(t)} /></div>
          </section>
        )
      })}
    </div>
  )
}

function People({ title, people }: { title: string; people: Person[] }) {
  return (
    <section className="rounded-xl border bg-background">
      <h3 className="flex h-11 items-center border-b px-4 text-sm font-medium">{title}<span className="ml-auto text-xs text-muted-foreground tabular">{people.length}</span></h3>
      <ul className="py-1">
        {people.map((p) => <li key={p.id} className="flex h-9 items-center gap-2 px-4 text-sm"><Avatar name={p.name} />{p.name}</li>)}
        {!people.length && <li className="flex h-9 items-center px-4 text-sm text-faint">None</li>}
      </ul>
    </section>
  )
}

export function AccessTab({ wf }: { wf: Workflow }) {
  return (
    <div className="max-w-5xl space-y-4 px-4 py-6 lg:px-6">
      <section className="rounded-xl border bg-background px-4 py-3">
        <Facts rows={[["Owner", wf.owner.name], ["Runs as", wf.run_as?.name ?? "Whoever runs it"], ["Visibility", wf.visibility === "shared" ? "Shared" : "Private"], ["Home", wf.home_name], ["Monthly cap", wf.monthly_cap_usd != null && <Money key="cap" usd={wf.monthly_cap_usd} />]]} />
      </section>
      <div className="grid gap-4 md:grid-cols-3">
        <People title="Editors" people={wf.editors} /><People title="Runners" people={wf.runners} /><People title="Viewers" people={wf.viewers} />
      </div>
    </div>
  )
}

export function VersionsTab({ wf }: { wf: Workflow }) {
  const query = useVersions(wf.id)
  return (
    <QueryState query={query} className="px-6 pt-6" isEmpty={(r) => !r.length} empty="Not published">{(vs) => (
      <Table>
        <TableHeader>
          <TableRow className="hover:bg-transparent">
            <TableHead className={`w-28 ${H}`}>Version</TableHead><TableHead>Note</TableHead><TableHead className="max-md:hidden">Published by</TableHead>
            <TableHead>Published</TableHead><TableHead className={T}>Runs</TableHead>
          </TableRow>
        </TableHeader>
        <TableBody>
          {vs.map((v) => (
            <TableRow key={v.number}>
              <TableCell className={H}><span className="flex items-center gap-2 tabular">v{v.number}{v.live && <Tag>Live</Tag>}</span></TableCell>
              <TableCell className="max-w-96 truncate">{v.note || <span className="text-faint">–</span>}</TableCell>
              <TableCell className="max-md:hidden"><span className="flex items-center gap-2"><Avatar name={v.published_by.name} />{v.published_by.name}</span></TableCell>
              <TableCell title={absolute(v.published_at)}><Ago iso={v.published_at} /></TableCell>
              <TableCell className={`tabular ${T}`}>{v.runs}</TableCell>
            </TableRow>
          ))}
        </TableBody>
      </Table>
    )}</QueryState>
  )
}
