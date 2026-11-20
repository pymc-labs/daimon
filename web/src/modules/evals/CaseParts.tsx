import { useState } from "react"

import { Drawer } from "@/components/Drawer"
import { Facts, PanelHead, PanelSection, Tag } from "@/components/panel"
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table"
import { rowProps, useParam } from "@/lib/filters"
import { absolute } from "@/lib/format"
import { cn } from "@/lib/utils"
import { names } from "./Item"
import { ChatThread, fromMessages, PLATFORMS } from "./previews"
import type { Case, CallPattern, Check, Platform } from "./types"
import { Payload } from "./viewers/Json"

const Group = ({ title, children }: { title: string; children: React.ReactNode }) => <section className="space-y-2"><h3 className="text-xs font-medium text-muted-foreground">{title}</h3>{children}</section>
const Empty = ({ children }: { children: React.ReactNode }) => <div className="py-10 text-center text-sm text-muted-foreground">{children}</div>
const pattern = (p: CallPattern) => [p.effect, p.service, p.server, p.tool].filter(Boolean).join(", ") || p.surface || "any"
const check = (c?: Check) => (c ? [c.op, c.pattern ?? c.value ?? c.values?.join(", ")].filter((x) => x !== undefined).join(" ") : "")
const times = (min: number, max?: number) => (max === undefined ? `at least ${min}` : min === max ? `${min}` : `${min} to ${max}`)
const Hard = ({ hard }: { hard: boolean }) => (hard ? <Tag>Hard</Tag> : null)

function Line({ children, aside }: { children: React.ReactNode; aside?: React.ReactNode }) {
  return <li className="flex min-h-8 items-center gap-2 rounded-md border px-3 py-1 text-sm"><span className="min-w-0 flex-1 break-words">{children}</span>{aside}</li>
}

/** The conversation up to the turn, and the turn itself, in the style of a chosen platform. */
export function Preview({ c }: { c: Case }) {
  const [platform, setPlatform] = useState<Platform>(c.surface.platform)
  const skin = { people: names(c), me: c.surface.participants.find((p) => p.is_requester)?.id }
  return (
    <div className="mx-auto max-w-2xl space-y-3">
      <div className="flex flex-wrap items-center gap-1" role="group" aria-label="Platform">
        {(Object.keys(PLATFORMS) as Platform[]).map((p) => (
          <button key={p} aria-pressed={platform === p} onClick={() => setPlatform(p)} className={cn("h-7 rounded-md px-2.5 text-xs", platform === p ? "bg-accent font-medium" : "text-muted-foreground hover:bg-accent/60")}>{PLATFORMS[p]}</button>
        ))}
      </div>
      <Group title="History"><ChatThread platform={platform} messages={fromMessages(c.history)} skin={skin} /></Group>
      <Group title="Turn"><ChatThread platform={platform} messages={fromMessages(c.turn.messages)} skin={skin} /></Group>
    </div>
  )
}

export function World({ c }: { c: Case }) {
  const w = c.world
  const [open, setOpen] = useParam("exchange")
  const list = w.exchanges ?? []
  const picked = list.find((x) => x.id === open)
  const lists: [string, string[]][] = [
    ["Resources", (w.resources ?? []).map((r) => `${r.service} ${r.type} ${r.key}`)], ["Files", (w.files ?? []).map((f) => f.path)],
    ["Repositories", (w.repos ?? []).map((r) => `${r.url} (${r.default_branch})`)], ["Web pages", (w.web ?? []).map((p) => `${p.status} ${p.url}`)], ["Identities", (w.identities ?? []).map((i) => `${i.service} ${i.login}`)],
  ]
  return (
    <div className="mx-auto max-w-[1040px] space-y-6">
      <Group title="Exchanges">
        {!list.length ? <Empty>No recorded exchanges</Empty> : (
          <Table>
            <TableHeader><TableRow className="hover:bg-transparent"><TableHead>Call</TableHead><TableHead className="max-sm:hidden">Surface</TableHead><TableHead>Kind</TableHead><TableHead className="max-md:hidden">Origin</TableHead><TableHead className="max-lg:hidden">Effects</TableHead></TableRow></TableHeader>
            <TableBody>
              {list.map((x) => (
                <TableRow key={x.id} {...rowProps(() => setOpen(x.id), x.id === open)}>
                  <TableCell className="font-mono text-xs">{[x.service ?? x.server, x.tool].filter(Boolean).join(".") || x.id}</TableCell>
                  <TableCell className="text-muted-foreground max-sm:hidden">{x.surface}</TableCell>
                  <TableCell>{x.kind === "write" ? <span className="text-warning">Write</span> : "Read"}</TableCell>
                  <TableCell className="text-muted-foreground max-md:hidden">{x.origin}</TableCell>
                  <TableCell className="text-muted-foreground max-lg:hidden">{x.effects?.map((e) => e.verb).join(", ")}</TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        )}
      </Group>
      {lists.filter(([, l]) => l.length).map(([title, l]) => (
        <Group key={title} title={title}><ul className="space-y-1">{l.map((t) => <Line key={t}><span className="font-mono text-xs">{t}</span></Line>)}</ul></Group>
      ))}
      <Drawer param="exchange" title="Exchange" ids={list.map((x) => x.id)} wide>
        {() => picked && (
          <div className="flex flex-1 flex-col">
            <PanelHead title={[picked.service ?? picked.server, picked.tool].filter(Boolean).join(".") || picked.id} subtitle={picked.surface} tags={<Tag>{picked.origin}</Tag>} />
            <PanelSection title="Request"><Payload value={picked.request} /></PanelSection>
            <PanelSection title="Response"><Payload value={picked.response} /></PanelSection>
          </div>
        )}
      </Drawer>
    </div>
  )
}

export function Script({ c }: { c: Case }) {
  const steps = c.script?.steps ?? []
  const d = c.script?.defaults
  return (
    <div className="mx-auto max-w-2xl space-y-6">
      <Group title="Steps">
        {!steps.length ? <Empty>No scripted actions</Empty> : (
          <ol className="space-y-1.5">
            {steps.map((s, i) => (
              <Line key={s.id} aside={s.delay_ms ? <span className="text-xs text-muted-foreground tabular">{s.delay_ms} ms</span> : null}>
                <span className="text-faint tabular">{i + 1}. </span>On {s.on.event.replace("_", " ")}{s.on.card_kind ? ` (${s.on.card_kind.replace("_", " ")})` : ""}{s.on.text_contains ? ` containing "${s.on.text_contains}"` : ""}
                <span className="text-muted-foreground"> then {s.do.map((a) => [a.action, a.button?.action, a.emoji, a.text].filter(Boolean).join(" ")).join(", ")}</span>
              </Line>
            ))}
          </ol>
        )}
      </Group>
      {d && <Group title="When nothing is scripted"><Facts rows={Object.entries(d).map(([k, v]) => [k.replace("_", " ").replace(/^./, (x) => x.toUpperCase()), v])} /></Group>}
    </div>
  )
}

export function Expectations({ c }: { c: Case }) {
  const e = c.expect
  return (
    <div className="mx-auto max-w-2xl space-y-6">
      {e.tool_calls && <Group title={`Tool calls, ${e.tool_calls.mode}`}><ul className="space-y-1">{e.tool_calls.calls.map((x, i) => <Line key={i} aside={<Hard hard={x.hard} />}>{pattern(x.match)} <span className="text-muted-foreground">{times(x.min, x.max)}</span></Line>)}</ul></Group>}
      {e.effects && <Group title={`Effects, ${e.effects.mode}`}><ul className="space-y-1">{e.effects.items.map((x, i) => <Line key={i} aside={<Hard hard={x.hard} />}>{x.verb}{x.target ? ` ${x.target}` : ""} <span className="text-muted-foreground">{times(x.min, x.max)}</span></Line>)}</ul></Group>}
      {(!!e.forbidden_calls?.length || !!e.forbidden_effects?.length) && (
        <Group title="Never"><ul className="space-y-1">{[...(e.forbidden_calls ?? []).map(pattern), ...(e.forbidden_effects ?? [])].map((x) => <Line key={x}>{x}</Line>)}</ul></Group>
      )}
      {!!e.outputs?.length && <Group title="Outputs"><ul className="space-y-1">{e.outputs.map((o, i) => <Line key={i} aside={<Hard hard={o.hard} />}>{o.kind.replace("_", " ")} <span className="text-muted-foreground">{[o.where, o.card_kind, check(o.text)].filter(Boolean).join(", ")}</span></Line>)}</ul></Group>}
      {!!e.rubric?.length && <Group title="Rubric"><ul className="space-y-1">{e.rubric.map((r) => <Line key={r.id} aside={<Hard hard={r.hard} />}>{r.criterion}</Line>)}</ul></Group>}
      {e.reference?.text && <Group title="Reference"><p className="whitespace-pre-wrap rounded-md border bg-muted/40 p-3 text-sm">{e.reference.text}</p></Group>}
      {e.limits && <Group title="Limits"><Facts rows={[["Tool calls", e.limits.max_tool_calls], ["Duration", e.limits.max_duration_s && `${e.limits.max_duration_s} s`], ["Cost", e.limits.max_cost_usd && `$${e.limits.max_cost_usd}`], ["Messages", e.limits.max_messages]].map(([k, v]) => [k as string, v === undefined ? null : String(v)])} /></Group>}
    </div>
  )
}

export function Provenance({ c }: { c: Case }) {
  const p = c.provenance
  return (
    <div className="mx-auto max-w-2xl space-y-6">
      <Facts rows={[
        ["Origin", p.kind.replace(/^./, (x) => x.toUpperCase())], ["Source", p.source_system], ["Source thread", c.source_thread], ["Recorded", p.recorded_at && absolute(p.recorded_at)],
        ["Fidelity", c.fidelity.level.replace(/^./, (x) => x.toUpperCase())], ["Missing", c.fidelity.missing?.join(", ")], ["Note", c.fidelity.note],
        ["Scrub", p.scrub && `${p.scrub.status.replace(/^./, (x) => x.toUpperCase())}${p.scrub.entities ? `, ${p.scrub.entities} replaced` : ""}`], ["Checked by a person", p.scrub && (p.scrub.human_checked ? "Yes" : "No")],
      ]} />
      {!!p.expectations_by?.length && (
        <Group title="Expectations">
          <ul className="space-y-1">{p.expectations_by.map((x, i) => <Line key={i} aside={<span className="text-xs text-muted-foreground">{absolute(x.at)}</span>}>{x.action ?? "Written"} by {x.kind === "model" ? "a model" : x.kind}</Line>)}</ul>
        </Group>
      )}
    </div>
  )
}
