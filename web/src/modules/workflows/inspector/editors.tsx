import { PenLine, X } from "lucide-react"
import { useState } from "react"

import { Prose } from "@/components/common"
import { Input } from "@/components/ui/input"
import { Switch } from "@/components/ui/switch"
import { cn } from "@/lib/utils"
import { fileRefs, finishDeliver, setAnswer, titleOf, toolWrites, upstreamRefs } from "../spec"
import type { AnswerShape, AskWhere, Node } from "../types"
import { approverLabel, KIND } from "../kinds"
import { ANSWER, asOptions, BUILTINS, NETWORK, NOWHERE, NOWHERE_LABEL, OUTCOME, placeOptions, SESSION, WHERE, WRITES } from "../options"
import { OutputTable } from "./OutputTable"
import { AddButton, Field, Pick, RemoveButton, RouteRows, Section, Segmented, TargetSelect, type EditorProps } from "./parts"
import { useLocal } from "./useLocal"
import { RefPicker } from "./RefPicker"

function Text({ value, onChange, ro, mono, className, placeholder }: { value: string; onChange: (v: string) => void; ro: boolean; mono?: boolean; className?: string; placeholder?: string }) {
  const [v, setV] = useLocal(value, onChange)
  return <Input value={v} readOnly={ro} placeholder={placeholder} onChange={(e) => setV(e.target.value)} spellCheck={!mono} className={cn("h-8", mono && "font-mono text-[12px]", className)} />
}

function Prompt({ value, onChange, ro }: { value: string; onChange: (v: string) => void; ro: boolean }) {
  const [tab, setTab] = useState<"write" | "preview">("write")
  const [v, setV] = useLocal(value, onChange)
  return (
    <Section title="Instructions" action={<Segmented value={tab} onChange={setTab} options={[["write", ro ? "Source" : "Write"], ["preview", "Preview"]]} />}>
      {tab === "write"
        ? <textarea value={v} readOnly={ro} onChange={(e) => setV(e.target.value)} spellCheck={false} className="min-h-40 w-full resize-y rounded-md border bg-background px-3 py-2 font-mono text-[12.5px] leading-[1.7] outline-none focus-visible:border-ring focus-visible:ring-3 focus-visible:ring-ring/15" />
        : <div className="min-h-40 rounded-md border px-3 py-2"><Prose text={v} /></div>}
    </Section>
  )
}

/** name -> value rows; values come from the reference picker. */
function Bindings({ title, rows, p, onChange, add }: { title: string; rows: Record<string, string>; p: EditorProps; onChange: (r: Record<string, string>) => void; add: string }) {
  const groups = upstreamRefs(p.spec, p.id)
  const entries = Object.entries(rows)
  return (
    <Section title={title} action={!p.ro && <AddButton label={add} onClick={() => onChange({ ...rows, [`value_${entries.length + 1}`]: "" })} />}>
      <div className="space-y-2">
        {entries.map(([k, v]) => (
          <div key={k} className="grid grid-cols-[112px_minmax(0,1fr)_auto] items-center gap-1.5">
            <span className="truncate font-mono text-[12px]" title={k}>{k}</span>
            <RefPicker value={v} groups={groups} ro={p.ro} onChange={(x) => onChange({ ...rows, [k]: x })} />
            {!p.ro && <RemoveButton onClick={() => onChange(Object.fromEntries(entries.filter(([x]) => x !== k)))} />}
          </div>
        ))}
        {!entries.length && <span className="text-sm text-faint">None</span>}
      </div>
    </Section>
  )
}

function Limits({ n, set, ro }: EditorProps) {
  const timeout = typeof n.timeout === "object" ? n.timeout.after : n.timeout ?? ""
  return (
    <Section title="Limits">
      <div className="grid grid-cols-3 gap-2">
        {([
          ["Timeout", timeout, "15m", (v: string) => set((x) => { x.timeout = typeof x.timeout === "object" ? { ...x.timeout, after: v } : v || undefined }, 400)],
          ["Budget", n.budget_usd != null ? `$${n.budget_usd}` : "", "$1", (v: string) => set((x) => { const k = parseFloat(v.replace("$", "")); x.budget_usd = Number.isFinite(k) ? k : undefined }, 400)],
          ["Tries", n.retry ? String(n.retry.attempts) : "1", "1", (v: string) => set((x) => { const k = parseInt(v); x.retry = k > 1 ? { attempts: k } : undefined }, 400)],
        ] as const).map(([l, v, ph, w]) => (
          <label key={l} className="space-y-1.5">
            <span className="block text-xs text-muted-foreground">{l}</span>
            <Text value={v} onChange={w} ro={ro} placeholder={ph} className="tabular" />
          </label>
        ))}
      </div>
    </Section>
  )
}

const routeRows = (n: Node) => Object.entries(n.routes ?? {}).map(([name, r]) => ({ name, to: typeof r === "string" ? r : r.to, when: typeof r === "string" ? undefined : r.when }))
const setRoute = (x: Node, name: string, to: string) => {
  const r = x.routes![name]!
  x.routes![name] = typeof r === "string" ? to : { ...r, to }
}

export function AgentEditor(p: EditorProps) {
  const { n, set, ro, catalog } = p
  const key = n.agent ?? p.spec.defaults?.agent ?? "daimon"
  const agent = catalog?.agents.find((a) => a.key === key)
  return (
    <>
      <Section>
        <div className="space-y-3">
          <Field label="Agent"><Pick value={key} ro={ro} onChange={(v) => set((x) => { x.agent = v })} options={(catalog?.agents ?? []).map((a) => ({ value: a.key, label: a.name }))} /></Field>
          <Field label="Works in"><Pick value={n.at ?? NOWHERE} ro={ro || n.memory === "personal"} onChange={(v) => set((x) => { x.at = v === NOWHERE ? undefined : v })}
            options={[{ value: NOWHERE, label: NOWHERE_LABEL }, ...placeOptions(catalog, n.at, false)]} /></Field>
          <Field label="Personal memory"><Switch checked={n.memory === "personal"} disabled={ro} onCheckedChange={(v) => set((x) => { x.memory = v ? "personal" : undefined; if (v) x.at = undefined })} /></Field>
          <Field label="Session"><Pick value={n.session ?? "main"} ro={ro} onChange={(v) => set((x) => { x.session = v === "main" ? undefined : v })} options={asOptions(SESSION)} /></Field>
        </div>
      </Section>
      <Prompt value={n.instructions ?? ""} ro={ro} onChange={(v) => set((x) => { x.instructions = v }, 500)} />
      <Bindings title="Inputs" add="Add input" rows={n.inputs ?? {}} p={p} onChange={(r) => set((x) => { x.inputs = r })} />
      <Section title="Output">
        <OutputTable fields={typeof n.output === "object" ? n.output : {}} ro={ro} onChange={(f) => set((x) => { x.output = f })} />
      </Section>
      <Section title="Routes" action={!ro && <AddButton label="Add route" onClick={() => set((x) => { x.routes = { ...x.routes, [`route_${Object.keys(x.routes ?? {}).length + 1}`]: { to: "", when: "" } } })} />}>
        {n.routes ? <RouteRows rows={routeRows(n)} spec={p.spec} self={p.id} ro={ro} onTo={(name, to) => set((x) => setRoute(x, name, to))} />
          : <Field label="Next"><TargetSelect spec={p.spec} self={p.id} value={typeof n.next === "string" ? n.next : undefined} ro={ro} onChange={(to) => set((x) => { x.next = to })} /></Field>}
      </Section>
      <Section title="Tools">
        <div className="space-y-3">
          <Field label="Tools"><Pick value={Array.isArray(n.tools) ? "custom" : "inherit"} ro={ro} onChange={(v) => set((x) => { x.tools = v === "inherit" ? undefined : [] })}
            options={[{ value: "inherit", label: `Inherit (${agent?.tools ?? 0})` }, { value: "custom", label: Array.isArray(n.tools) ? `Custom (${n.tools.length})` : "Custom" }]} /></Field>
          <Field label="Writes"><Pick value={n.writes ?? "refuse"} ro={ro} onChange={(v) => set((x) => { x.writes = v === "refuse" ? undefined : (v as Node["writes"]) })}
            options={asOptions(WRITES)} /></Field>
          <Field label="Built-ins"><Segmented value={n.builtins ?? "none"} ro={ro} onChange={(v) => set((x) => { x.builtins = v === "none" ? undefined : v })} options={BUILTINS} /></Field>
        </div>
      </Section>
      <Limits {...p} />
    </>
  )
}

export function ApprovalEditor(p: EditorProps) {
  const { n, set, ro, catalog, spec } = p
  const approvers = [n.approvers ?? []].flat()
  const binds = [n.binds ?? []].flat()
  const where: AskWhere = typeof n.deliver === "string" ? n.deliver : "dm"
  const shape: AnswerShape = n.answer ?? "approve"
  const chip = (label: React.ReactNode, onRemove: () => void, key: string) => (
    <span key={key} className="inline-flex h-6 items-center gap-1 rounded-md border bg-background pr-0.5 pl-2 text-xs">
      {label}{!ro && <button className="flex size-5 items-center justify-center rounded text-muted-foreground hover:bg-accent" onClick={onRemove} aria-label="Remove"><X className="size-3" /></button>}
    </span>
  )
  const people = (catalog?.people ?? []).map((x) => ({ value: `person:${x.id}`, label: x.name }))
  const options = [{ value: "owner", label: "Owner" }, { value: "triggered_by", label: approverLabel("triggered_by") }, ...(where === "thread" ? [{ value: "thread", label: approverLabel("thread") }] : []), ...people]
  const routes = Object.entries(n.routes ?? {})
  return (
    <>
      <Section>
        <div className="space-y-3">
          <Field label="Approvers" top>
            <div className="flex flex-wrap items-center gap-1.5 pt-1">
              {approvers.map((a) => chip(approverLabel(a, catalog), () => set((x) => { x.approvers = approvers.filter((y) => y !== a) }), a))}
              {!ro && (
                <Pick value="" placeholder="Add" className="h-6 w-20 text-xs" onChange={(v) => set((x) => { x.approvers = [...approvers, v] })}
                  options={options.filter((o) => !approvers.includes(o.value))} />
              )}
            </div>
          </Field>
          <Field label="Needs"><Pick value={String(n.min_approvals ?? 1)} ro={ro} onChange={(v) => set((x) => { x.min_approvals = Number(v) })} options={approvers.map((_, i) => ({ value: String(i + 1), label: `${i + 1} of ${approvers.length}` }))} /></Field>
          <Field label="Where">
            <Segmented value={where} ro={ro} options={WHERE}
              onChange={(v) => set((x) => { x.deliver = v; if (v !== "thread") { x.channel = undefined; x.approvers = [x.approvers ?? []].flat().filter((a) => a !== "thread") } })} />
          </Field>
          {where === "thread" && (
            <Field label="Channel">
              <Pick value={n.channel ?? ""} ro={ro} placeholder="Home channel" onChange={(v) => set((x) => { x.channel = v })}
                options={(catalog?.channels ?? []).map((c) => ({ value: c.id, label: `#${c.name}` }))} />
            </Field>
          )}
          <Field label="Answer">
            <Segmented value={shape} ro={ro} options={ANSWER} onChange={(v) => set((x) => setAnswer(x, v))} />
          </Field>
          <Field label="Remind every"><Text value={n.remind_every ?? ""} placeholder="Never" ro={ro} onChange={(v) => set((x) => { x.remind_every = v || undefined }, 400)} /></Field>
          <Field label="Timeout"><Text value={typeof n.timeout === "object" ? n.timeout.after : n.timeout ?? ""} placeholder="None" ro={ro} onChange={(v) => set((x) => { x.timeout = v ? { after: v, route: typeof x.timeout === "object" ? x.timeout.route : "reject" } : undefined }, 400)} /></Field>
        </div>
      </Section>
      <Section title="Shows">
        <RefPicker value={n.show ?? ""} groups={upstreamRefs(spec, p.id)} ro={ro} onChange={(v) => set((x) => { x.show = v })} />
      </Section>
      {(shape === "approve" || shape === "form") && <Section title="Form"><OutputTable fields={n.form ?? {}} ro={ro} onChange={(f) => set((x) => { x.form = f })} /></Section>}
      <Section title="Attach">
        <RefPicker value={n.attach ?? ""} groups={fileRefs(spec, p.id)} ro={ro} onChange={(v) => set((x) => { x.attach = v || undefined })} />
      </Section>
      {shape === "approve" && (
        <Section title="Covers">
          <div className="flex flex-wrap items-center gap-1.5">
            {binds.map((b) => {
              const K = KIND[spec.nodes[b]?.kind ?? "action"]
              return chip(<span className="flex items-center gap-1.5"><K.icon className={cn("size-3.5", K.text)} />{titleOf(b, spec.nodes[b])}<PenLine className="size-3 text-muted-foreground" /></span>, () => set((x) => { x.binds = binds.filter((y) => y !== b) }), b)
            })}
            {!ro && (
              <Pick value="" placeholder="Add step" className="h-6 w-28 text-xs" onChange={(v) => set((x) => { x.binds = [...binds, v] })}
                options={Object.entries(spec.nodes).filter(([id, s]) => !binds.includes(id) && (s.kind === "action" || s.kind === "agent")).map(([id, s]) => ({ value: id, label: titleOf(id, s) }))} />
            )}
          </div>
        </Section>
      )}
      {shape === "choice" ? (
        <Section title="Options" action={!ro && <AddButton label="Add option" onClick={() => set((x) => { x.routes = { ...x.routes, [`option_${routes.length + 1}`]: routes[0] ? routeRows(x)[0]!.to : "" } })} />}>
          <div className="space-y-2">
            {routes.map(([name], i) => (
              <div key={i} className="grid grid-cols-[112px_minmax(0,1fr)_28px] items-center gap-1.5">
                <Text value={name} mono ro={ro} onChange={(v) => set((x) => { x.routes = Object.fromEntries(Object.entries(x.routes ?? {}).map(([k, r]) => [k === name ? v || k : k, r])) }, 400)} />
                <TargetSelect spec={spec} self={p.id} value={routeRows(n)[i]!.to} ro={ro} onChange={(to) => set((x) => setRoute(x, name, to))} />
                {!ro && routes.length > 2 ? <RemoveButton onClick={() => set((x) => { x.routes = Object.fromEntries(Object.entries(x.routes ?? {}).filter(([k]) => k !== name)) })} /> : <span />}
              </div>
            ))}
          </div>
        </Section>
      ) : shape === "approve" ? (
        <Section title="Routes">
          <RouteRows rows={routeRows(n)} spec={spec} self={p.id} ro={ro} onTo={(name, to) => set((x) => setRoute(x, name, to))} />
        </Section>
      ) : null}
      {(shape === "approve" || shape === "choice") && (
        <Section title="Other replies">
          <div className="flex items-center gap-1.5">
            <TargetSelect spec={spec} self={p.id} value={n.otherwise} ro={ro} onChange={(to) => set((x) => { x.otherwise = to })} />
            {!ro && n.otherwise && <RemoveButton onClick={() => set((x) => { x.otherwise = undefined })} />}
          </div>
        </Section>
      )}
      {shape === "approve" || shape === "choice" ? null : (
        <Section title="Then">
          <TargetSelect spec={spec} self={p.id} value={typeof n.next === "string" ? n.next : undefined} ro={ro} onChange={(to) => set((x) => { x.next = to })} />
        </Section>
      )}
    </>
  )
}

export function BranchEditor(p: EditorProps) {
  const { n, set, ro, spec } = p
  const cases = n.cases ?? []
  return (
    <Section title="Cases" action={!ro && <AddButton label="Add case" onClick={() => set((x) => { x.cases = [...cases, { when: "", to: "" }] })} />}>
      <div className="space-y-3">
        {cases.map((c, i) => (
          <div key={i} className="space-y-1.5">
            <div className="flex items-center gap-1.5">
              <span className="w-8 shrink-0 text-xs text-muted-foreground">When</span>
              <Text value={c.when} mono ro={ro} onChange={(v) => set((x) => { x.cases![i]!.when = v }, 400)} />
              {!ro && <RemoveButton onClick={() => set((x) => { x.cases = cases.filter((_, j) => j !== i) })} />}
            </div>
            <div className="flex items-center gap-1.5">
              <span className="w-8 shrink-0 text-xs text-muted-foreground">Go to</span>
              <TargetSelect spec={spec} self={p.id} value={c.to} ro={ro} onChange={(to) => set((x) => { x.cases![i]!.to = to })} />
              {!ro && <span className="w-7 shrink-0" />}
            </div>
          </div>
        ))}
        <div className="border-t pt-3"><Field label="Else"><TargetSelect spec={spec} self={p.id} value={n.else} ro={ro} onChange={(to) => set((x) => { x.else = to })} /></Field></div>
      </div>
    </Section>
  )
}

export function ActionEditor(p: EditorProps) {
  const { n, set, ro, catalog, spec } = p
  return (
    <>
      <Section>
        <div className="space-y-3">
          <Field label="Tool">
            <Pick value={n.tool ?? ""} ro={ro} placeholder="Choose tool" onChange={(v) => set((x) => { x.tool = v })}
              options={(catalog?.tools ?? []).map((t) => ({ value: t.id, label: <span className="flex items-center gap-2">{t.label}{t.writes && <PenLine className="size-3 text-muted-foreground" />}</span> }))} />
          </Field>
          <Field label="Writes"><span className="flex items-center gap-1.5 text-sm">{toolWrites(n.tool) ? <><PenLine className="size-3.5 text-muted-foreground" />Yes</> : "No"}</span></Field>
          <Field label="Idempotent"><Switch checked={!!n.idempotent} disabled={ro} onCheckedChange={(v) => set((x) => { x.idempotent = v || undefined })} /></Field>
        </div>
      </Section>
      <Bindings title="Arguments" add="Add argument" rows={n.args ?? {}} p={p} onChange={(r) => set((x) => { x.args = r })} />
      <Section title="Next">
        <div className="space-y-3">
          <Field label="Then"><TargetSelect spec={spec} self={p.id} value={typeof n.next === "string" ? n.next : undefined} ro={ro} onChange={(to) => set((x) => { x.next = to })} /></Field>
          <Field label="On error"><TargetSelect spec={spec} self={p.id} value={n.on_error} ro={ro} onChange={(to) => set((x) => { x.on_error = to })} /></Field>
        </div>
      </Section>
      <Limits {...p} />
    </>
  )
}

export function WaitEditor(p: EditorProps) {
  const { n, set, ro, spec } = p
  const mode = n.poll ? "poll" : n.event ? "event" : n.until ? "until" : "for"
  return (
    <Section>
      <div className="space-y-3">
        <Field label="Wait"><Segmented value={mode} ro={ro} options={[["for", "For"], ["until", "Until"], ["event", "Event"], ["poll", "Poll"]]}
          onChange={(m) => set((x) => { delete x.for; delete x.until; delete x.event; delete x.poll; if (m === "for") x.for = "1h"; else if (m === "until") x.until = ""; else if (m === "event") x.event = { source: "" }; else x.poll = { tool: "", every: "5m", until: "" } })} /></Field>
        {mode === "for" && <Field label="Duration"><Text value={n.for ?? ""} ro={ro} onChange={(v) => set((x) => { x.for = v }, 400)} /></Field>}
        {mode === "until" && <Field label="Until"><RefPicker value={n.until ?? ""} groups={upstreamRefs(spec, p.id)} ro={ro} onChange={(v) => set((x) => { x.until = v })} /></Field>}
        {mode === "event" && <>
          <Field label="Source"><Text value={n.event?.source ?? ""} mono ro={ro} onChange={(v) => set((x) => { x.event = { ...x.event, source: v } }, 400)} /></Field>
          <Field label="Match"><Text value={n.event?.match ?? ""} mono ro={ro} onChange={(v) => set((x) => { x.event = { source: x.event?.source ?? "", match: v } }, 400)} /></Field>
        </>}
        {mode === "poll" && <>
          <Field label="Tool"><Text value={n.poll?.tool ?? ""} mono ro={ro} onChange={(v) => set((x) => { x.poll = { ...x.poll!, tool: v } }, 400)} /></Field>
          <Field label="Every"><Text value={n.poll?.every ?? ""} ro={ro} onChange={(v) => set((x) => { x.poll = { ...x.poll!, every: v } }, 400)} /></Field>
          <Field label="Until"><Text value={n.poll?.until ?? ""} mono ro={ro} placeholder="result.status == 'done'" onChange={(v) => set((x) => { x.poll = { ...x.poll!, until: v } }, 400)} /></Field>
        </>}
        <Field label="Timeout"><Text value={typeof n.timeout === "object" ? n.timeout.after : n.timeout ?? ""} placeholder="None" ro={ro} onChange={(v) => set((x) => { x.timeout = v || undefined }, 400)} /></Field>
        {n.routes ? <RouteRows rows={routeRows(n)} spec={spec} self={p.id} ro={ro} onTo={(name, to) => set((x) => setRoute(x, name, to))} />
          : <Field label="Then"><TargetSelect spec={spec} self={p.id} value={typeof n.next === "string" ? n.next : undefined} ro={ro} onChange={(to) => set((x) => { x.next = to })} /></Field>}
      </div>
    </Section>
  )
}

export function ForEachEditor(p: EditorProps) {
  const { n, set, ro, spec, catalog } = p
  const groups = upstreamRefs(spec, p.id)
  return (
    <>
      <Section>
        <div className="space-y-3">
          <Field label="Items"><RefPicker value={n.items ?? ""} groups={groups} ro={ro} onChange={(v) => set((x) => { x.items = v })} /></Field>
          <Field label="Each as"><Text value={n.as ?? "item"} mono ro={ro} onChange={(v) => set((x) => { x.as = v }, 400)} /></Field>
          <Field label="At a time"><Text value={String(n.concurrency ?? 1)} ro={ro} className="tabular" onChange={(v) => set((x) => { x.concurrency = parseInt(v) || 1 }, 400)} /></Field>
          <Field label="Allowed failures"><Text value={String(n.tolerate_failures ?? 0)} ro={ro} className="tabular" onChange={(v) => set((x) => { x.tolerate_failures = parseInt(v) || 0 }, 400)} /></Field>
        </div>
      </Section>
      <Section title="Each item">
        <Field label="Agent"><Pick value={n.do?.agent ?? "daimon"} ro={ro} onChange={(v) => set((x) => { x.do = { ...x.do!, agent: v } })} options={(catalog?.agents ?? []).map((a) => ({ value: a.key, label: a.name }))} /></Field>
      </Section>
      <Prompt value={n.do?.instructions ?? ""} ro={ro} onChange={(v) => set((x) => { x.do = { ...x.do!, instructions: v } }, 500)} />
      <Section title="Next"><Field label="Then"><TargetSelect spec={spec} self={p.id} value={typeof n.next === "string" ? n.next : undefined} ro={ro} onChange={(to) => set((x) => { x.next = to })} /></Field></Section>
    </>
  )
}

export function FinishEditor(p: EditorProps) {
  const { n, set, ro, catalog, spec } = p
  const to = finishDeliver(n)
  const [msg, setMsg] = useLocal(to?.message ?? "", (v) => set((x) => { x.deliver = { to: finishDeliver(x)?.to ?? "", message: v } }, 500))
  return (
    <>
      <Section>
        <div className="space-y-3">
          <Field label="Outcome"><Segmented value={n.outcome ?? "succeeded"} ro={ro} onChange={(v) => set((x) => { x.outcome = v })} options={OUTCOME} /></Field>
          <Field label="Output"><RefPicker value={typeof n.output === "string" ? n.output : ""} groups={upstreamRefs(spec, p.id)} ro={ro} onChange={(v) => set((x) => { x.output = v })} /></Field>
          <Field label="Deliver to"><Pick value={to?.to ?? "none"} ro={ro} onChange={(v) => set((x) => { x.deliver = v === "none" ? undefined : { ...finishDeliver(x), to: v } })}
            options={[{ value: "none", label: "Nowhere" }, ...(catalog?.channels ?? []).map((c) => ({ value: `#${c.name}`, label: `#${c.name}` }))]} /></Field>
        </div>
      </Section>
      {to && (
        <Section title="Message">
          <textarea value={msg} readOnly={ro} onChange={(e) => setMsg(e.target.value)} spellCheck={false} className="min-h-28 w-full resize-y rounded-md border bg-background px-3 py-2 font-mono text-[12.5px] leading-[1.7] outline-none focus-visible:border-ring" />
        </Section>
      )}
    </>
  )
}

/** A post step: where it goes, the thread it finds or opens by title, and the words. */
export function PostEditor(p: EditorProps) {
  const { n, set, ro, catalog, spec } = p
  const [msg, setMsg] = useLocal(n.message ?? "", (v) => set((x) => { x.message = v || undefined }, 500))
  const to = n.to
  return (
    <>
      <Section>
        <div className="space-y-3">
          <Field label="Post to"><Pick value={to ?? ""} ro={ro} placeholder="Choose" onChange={(v) => set((x) => { x.to = v })}
            options={placeOptions(catalog, to, true)} /></Field>
          <Field label="Post as"><Pick value={n.as_agent ?? NOWHERE} ro={ro} onChange={(v) => set((x) => { x.as_agent = v === NOWHERE ? undefined : v })}
            options={[{ value: NOWHERE, label: "Daimon" }, ...(catalog?.agents ?? []).map((a) => ({ value: a.key, label: a.name }))]} /></Field>
          <Field label="Thread"><Text value={n.thread ?? ""} ro={ro} placeholder="None" onChange={(v) => set((x) => { x.thread = v || undefined }, 400)} /></Field>
        </div>
      </Section>
      <Section title="Message">
        <textarea value={msg} readOnly={ro} onChange={(e) => setMsg(e.target.value)} spellCheck={false} aria-label="Message" className="min-h-28 w-full resize-y rounded-md border bg-background px-3 py-2 font-mono text-[12.5px] leading-[1.7] outline-none focus-visible:border-ring" />
      </Section>
      <Section title="Next"><Field label="Then"><TargetSelect spec={spec} self={p.id} value={typeof n.next === "string" ? n.next : undefined} ro={ro} onChange={(to) => set((x) => { x.next = to })} /></Field></Section>
    </>
  )
}

export function BasicEditor(p: EditorProps) {
  const { n, set, ro, spec } = p
  const [src, setSrc] = useLocal(n.source ?? "", (v) => set((x) => { x.source = v }, 500))
  return (
    <>
      {n.kind === "transform" && <Bindings title="Set" add="Add field" rows={n.set ?? {}} p={p} onChange={(r) => set((x) => { x.set = r })} />}
      {n.kind === "code" && <>
        <Section><Field label="Network"><Segmented value={n.network ?? "off"} ro={ro} onChange={(v) => set((x) => { x.network = v })} options={NETWORK} /></Field></Section>
        <Section title="Python">
          <textarea value={src} readOnly={ro} onChange={(e) => setSrc(e.target.value)} spellCheck={false} className="min-h-48 w-full resize-y rounded-md border bg-background px-3 py-2 font-mono text-[12.5px] leading-[1.7] outline-none focus-visible:border-ring" />
        </Section>
        <Section title="Output"><OutputTable fields={typeof n.output === "object" ? n.output : {}} ro={ro} onChange={(f) => set((x) => { x.output = f })} /></Section>
      </>}
      {n.kind === "workflow" && (
        <Section>
          <div className="space-y-3">
            <Field label="Workflow"><Text value={n.workflow ?? ""} mono ro={ro} onChange={(v) => set((x) => { x.workflow = v }, 400)} /></Field>
            <Field label="Version"><Text value={String(n.version ?? "live")} ro={ro} onChange={(v) => set((x) => { x.version = v === "live" ? "live" : Number(v) || "live" }, 400)} /></Field>
          </div>
        </Section>
      )}
      <Section title="Next"><Field label="Then"><TargetSelect spec={spec} self={p.id} value={typeof n.next === "string" ? n.next : undefined} ro={ro} onChange={(to) => set((x) => { x.next = to })} /></Field></Section>
    </>
  )
}
