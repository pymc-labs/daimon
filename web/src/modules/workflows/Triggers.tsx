import { CircleAlert, CircleCheck, CircleMinus, Copy, LoaderCircle, Pause, Play, Radio, RotateCw, ShieldCheck, ShieldX, TriangleAlert, CircleX } from "lucide-react"
import { useMemo, useState } from "react"
import { Link } from "react-router"

import { Ago, Avatar, CopyButton, Mono } from "@/components/common"
import { Facts, SettingRow, Tag } from "@/components/panel"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select"
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table"
import { Textarea } from "@/components/ui/textarea"
import { absolute } from "@/lib/format"
import { cn } from "@/lib/utils"
import { useDebounced, useDeliveries, useDraft, useDraftEditor, useListen, useNow, usePatch, usePreviewMapping, useReplay, useWebhook } from "./api"
import { cronWords, fromCron, KINDS, nextFires, toCron, WEEKDAYS, type Kind, type Spec as Sched } from "./cron"
import { TRIGGER } from "./kinds"
import { checkInputs, mappingText, parseMapping, suggestMapping, type Row } from "./mapping"
import { RotateDialog } from "./RotateDialog"
import { JsonTree } from "./run/parts"
import { listeningAt, shortId, until } from "./run/lib"
import { clone } from "./spec"
import { ChoiceCards, EditStrip, Part } from "./tabparts"
import type { DeliveryRow, Spec, Trigger, Workflow } from "./types"

const zonesOf = (current: string) => [...new Set(["UTC", current, ...(typeof Intl.supportedValuesOf === "function" ? Intl.supportedValuesOf("timeZone") : [])])]
const localZone = () => Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC"

const VERIFY = [
  { value: "standard-webhooks", label: "Standard Webhooks", fact: "webhook-signature" }, { value: "github", label: "GitHub", fact: "X-Hub-Signature-256" },
  { value: "stripe", label: "Stripe", fact: "Stripe-Signature" }, { value: "slack", label: "Slack", fact: "X-Slack-Signature" },
  { value: "bearer", label: "Static bearer", fact: "Authorization header" }, { value: "none", label: "None", fact: "Needs an admin" },
]

type Ctx = { wf: Workflow; spec: Spec; ro: boolean; set: (id: string, fn: (t: Trigger) => void, delay?: number) => void }

export function TriggersTab({ wf }: { wf: Workflow }) {
  const ro = !wf.can_edit
  const { data: draft } = useDraft(wf.id, !ro)
  const editor = useDraftEditor(wf, draft)
  const spec = draft?.spec ?? wf.spec
  const set: Ctx["set"] = (id, fn, delay) => {
    const s = clone(spec)
    const t = s.triggers?.find((x) => x.id === id)
    if (!t) return
    fn(t)
    editor.save(s, delay)
  }
  const ts = spec.triggers ?? []
  return (
    <>
      <EditStrip wf={wf} />
      <div className="max-w-3xl space-y-8 px-4 py-6 lg:px-6">
        {ts.map((t) => <TriggerBlock key={t.id} t={t} c={{ wf, spec, ro, set }} />)}
        {!ts.length && <p className="text-sm text-muted-foreground">No triggers</p>}
      </div>
    </>
  )
}

function TriggerBlock({ t, c }: { t: Trigger; c: Ctx }) {
  const K = TRIGGER[t.kind]
  return (
    <section className="[&:not(:first-child)]:border-t [&:not(:first-child)]:pt-8">
      <h2 className="mb-5 flex items-center gap-2 text-base font-semibold"><K.icon className="size-4 text-muted-foreground" />{K.label}<Mono className="text-xs font-normal text-faint">{t.id}</Mono></h2>
      {t.kind === "schedule" && (t.at ? <OneTime t={t} /> : <Schedule t={t} c={c} />)}
      {t.kind === "webhook" && <Webhook t={t} c={c} />}
      {t.kind === "manual" && <Manual c={c} />}
      {t.kind === "event" && <Event t={t} />}
      {t.kind === "poll" && <Poll t={t} c={c} />}
      {t.kind === "call" && <Part><Facts rows={[["Called by", "Other workflows"]]} /></Part>}
    </section>
  )
}

// ---------- schedule ----------

/** A one-time schedule: it runs at its time, once, and is done. */
function OneTime({ t }: { t: Trigger }) {
  return <Part title="When"><Facts rows={[["Runs once at", <span key="a" className="font-medium">{absolute(t.at)}</span>], ["Time zone", t.timezone ?? "UTC"]]} /></Part>
}

function Schedule({ t, c }: { t: Trigger; c: Ctx }) {
  const cron = t.cron ?? "0 9 * * *"
  const zone = t.timezone ?? "UTC"
  const [pick, setPick] = useState<Sched>(() => fromCron(cron))
  const list = useMemo(() => zonesOf(zone === "UTC" ? localZone() : zone), [zone])
  const patch = usePatch(c.wf.id)
  const paused = c.wf.status === "paused"
  const [now] = useState(Date.now)
  const fires = useMemo(() => nextFires(cron, zone, now, 3), [cron, zone, now])
  const change = (over: Partial<Sched>) => {
    const next = { ...pick, ...over }
    setPick(next)
    c.set(t.id, (x) => { x.cron = toCron(next) }, 400)
  }
  return (
    <>
      <Part title="When" aside={c.wf.live_version != null && !c.ro && (
        <Button variant="outline" size="sm" disabled={patch.isPending} onClick={() => patch.mutate({ paused: !paused })}>{paused ? <Play /> : <Pause />}{paused ? "Resume" : "Pause"}</Button>
      )}>
        <div className="space-y-2.5">
          <SettingRow label="Repeats">
            <Select value={pick.kind} disabled={c.ro} onValueChange={(k) => change({ kind: k as Kind, cron: k === "custom" ? toCron(pick) : pick.cron })}>
              <SelectTrigger aria-label="Repeats" className="h-8 w-full"><SelectValue /></SelectTrigger>
              <SelectContent>{KINDS.map((k) => <SelectItem key={k.value} value={k.value}>{k.label}</SelectItem>)}</SelectContent>
            </Select>
          </SettingRow>
          {pick.kind === "weekly" && (
            <SettingRow label="On">
              <Select value={String(pick.weekday)} disabled={c.ro} onValueChange={(d) => change({ weekday: Number(d) })}>
                <SelectTrigger aria-label="On" className="h-8 w-full"><SelectValue /></SelectTrigger>
                <SelectContent>{WEEKDAYS.map((d, i) => <SelectItem key={d} value={String(i)}>{d}</SelectItem>)}</SelectContent>
              </Select>
            </SettingRow>
          )}
          {pick.kind === "monthly" && (
            <SettingRow label="On day">
              <Select value={String(pick.day)} disabled={c.ro} onValueChange={(d) => change({ day: Number(d) })}>
                <SelectTrigger aria-label="On day" className="h-8 w-full"><SelectValue /></SelectTrigger>
                <SelectContent>{Array.from({ length: 28 }, (_, i) => <SelectItem key={i} value={String(i + 1)}>{i + 1}</SelectItem>)}</SelectContent>
              </Select>
            </SettingRow>
          )}
          {pick.kind === "hourly" && (
            <SettingRow label="At minute">
              <Input aria-label="At minute" type="number" min={0} max={59} className="h-8" disabled={c.ro} value={Number(pick.time.split(":")[1])}
                onChange={(e) => change({ time: `00:${String(Math.min(59, Math.max(0, Number(e.target.value) || 0))).padStart(2, "0")}` })} />
            </SettingRow>
          )}
          {["daily", "weekdays", "weekly", "monthly"].includes(pick.kind) && (
            <SettingRow label="At"><Input aria-label="At" type="time" className="h-8" disabled={c.ro} value={pick.time} onChange={(e) => change({ time: e.target.value || "09:00" })} /></SettingRow>
          )}
          {pick.kind === "custom" && (
            <SettingRow label="Cron"><Input aria-label="Cron expression" className="h-8 font-mono" disabled={c.ro} value={pick.cron} onChange={(e) => change({ cron: e.target.value })} /></SettingRow>
          )}
          <SettingRow label="Time zone">
            <Select value={zone} disabled={c.ro} onValueChange={(z) => c.set(t.id, (x) => { x.timezone = z })}>
              <SelectTrigger aria-label="Time zone" className="h-8 w-full"><SelectValue /></SelectTrigger>
              <SelectContent>{list.map((z) => <SelectItem key={z} value={z}>{z}</SelectItem>)}</SelectContent>
            </Select>
          </SettingRow>
        </div>
        <div className="mt-4"><Facts rows={[["In words", <span key="w" className="font-medium">{cronWords(cron)}</span>], ["Cron", <Mono key="c" className="text-xs">{cron}</Mono>], ["Every", t.every_weeks ? `${t.every_weeks} weeks, from the week of ${t.anchor ?? "the first run"}` : ""]]} /></div>
      </Part>
      <Part title="If a run is missed">
        <ChoiceCards label="If a run is missed" disabled={c.ro} value={t.catch_up ?? "skip"} onChange={(v) => c.set(t.id, (x) => { x.catch_up = v })}
          options={[{ value: "skip", label: "Skip", fact: "Wait for the next time" }, { value: "run-once", label: "Run once", fact: "One run when it is back" }]} />
      </Part>
      {!t.every_weeks && <Part title="Next fires">
        {paused ? (
          <p className="flex items-center gap-2 text-sm"><Pause className="size-4 text-muted-foreground" />Paused</p>
        ) : (
          <ul className="space-y-1.5 text-sm">
            {fires.map((f) => (
              <li key={f} className="flex items-center gap-3">
                <span className="w-40 tabular">{new Date(f).toLocaleString("en-GB", { weekday: "short", day: "numeric", month: "short", hour: "2-digit", minute: "2-digit", timeZone: zone })}</span>
                <span className="text-muted-foreground tabular">In {until(f)}</span>
              </li>
            ))}
          </ul>
        )}
      </Part>}
    </>
  )
}

// ---------- webhook ----------

const STATE: Record<Row["state"], { icon: typeof CircleCheck; cls: string; label: string }> = {
  ok: { icon: CircleCheck, cls: "text-success", label: "Matches" },
  missing: { icon: CircleX, cls: "text-destructive", label: "Missing" },
  wrong_type: { icon: TriangleAlert, cls: "text-warning", label: "Wrong type" },
  empty: { icon: CircleMinus, cls: "text-muted-foreground", label: "Empty" },
}
const OUTCOME = { accepted: { icon: CircleCheck, cls: "text-success", label: "Accepted" }, skipped: { icon: CircleMinus, cls: "text-muted-foreground", label: "Skipped" }, rejected: { icon: CircleX, cls: "text-destructive", label: "Rejected" }, duplicate: { icon: Copy, cls: "text-muted-foreground", label: "Duplicate" } }

const show = (v: unknown) => (v === undefined ? "" : typeof v === "string" ? v : JSON.stringify(v))
const isTest = (d: DeliveryRow) => d.id.startsWith("dlv_test_")

function UrlRow({ label, url, action }: { label: string; url: string; action?: React.ReactNode }) {
  return (
    <SettingRow label={label}>
      <div className="flex h-8 items-center gap-1 rounded-md border bg-muted/40 pl-3 pr-1">
        <span className="min-w-0 flex-1 truncate font-mono text-xs">{url}</span>
        <CopyButton text={url} label={`Copy ${label} URL`} />
        {action}
      </div>
    </SettingRow>
  )
}

function Webhook({ t, c }: { t: Trigger; c: Ctx }) {
  const id = c.wf.id
  const [since, setSince] = useState<number | null>(null)
  const [rotate, setRotate] = useState(false)
  const info = useWebhook(id, t.id, true, since != null)
  const deliveries = useDeliveries(id, t.id, true, since != null)
  const listen = useListen(id, t.id)
  const replay = useReplay(id, t.id)
  const w = info.data
  const rows = deliveries.data ?? []
  const now = useNow(!!w?.listening_until, 5_000)
  const listening = listeningAt(w?.listening_until, now)
  const captured = since != null ? rows.find((d) => isTest(d) && Date.parse(d.received_at) >= since) : undefined
  const last = rows.find((d) => d.outcome !== "rejected")
  const expr = useDebounced(t.inputs ?? "", 300)
  const preview = usePreviewMapping(id, t.id, expr, last?.id).data
  const mapped = t.inputs && preview ? preview : null
  const checked = mapped?.ok ? checkInputs(c.spec.inputs, mapped.value) : null
  const start = () => { setSince(Date.now()); listen.start.mutate() }
  const stop = () => { listen.stop.mutate(); setSince(null) }
  const fill = () => { if (!captured) return; c.set(t.id, (x) => { x.inputs = suggestMapping(c.spec.inputs, captured.payload) }, 0); setSince(null) }
  return (
    <>
      <Part title="Endpoints">
        <div className="space-y-2.5">
          <UrlRow label="Live" url={w?.live_url ?? ""} />
          <UrlRow label="Test" url={w?.test_url ?? ""} action={!c.ro && !listening && !captured && (
            <Button variant="ghost" size="sm" disabled={listen.start.isPending} onClick={start}><Radio />Listen</Button>
          )} />
        </div>
        {(listening || captured) && (
          <div className="mt-4 space-y-3">
            {!captured && (
              <div role="status" className="flex items-center gap-2.5 text-sm">
                <LoaderCircle className="size-4 animate-spin text-link" />
                <span className="font-medium">Listening on the test URL</span>
                {w?.listening_until && <span className="text-muted-foreground tabular">{until(w.listening_until, now)} left</span>}
                <Button variant="outline" size="sm" className="ml-auto" onClick={stop}>Stop</Button>
              </div>
            )}
            {captured && (
              <>
                <div className="flex items-center gap-2 text-sm"><CircleCheck className="size-4 text-success" /><span className="font-medium">Test delivery captured</span><Mono className="text-xs text-faint">{shortId(captured.id)}</Mono></div>
                <div className="rounded-lg border px-3 py-2.5"><JsonTree value={captured.payload} root="payload" /></div>
                <div className="flex justify-end gap-2 max-sm:[&>*]:flex-1">
                  <Button variant="ghost" onClick={() => setSince(null)}>Dismiss</Button>
                  <Button onClick={fill}>Fill inputs</Button>
                </div>
              </>
            )}
          </div>
        )}
        {listen.start.error && <p className="mt-2 text-sm text-destructive">{listen.start.error.message}</p>}
      </Part>
      <Part title="Verification">
        <ChoiceCards label="Verification" cols={3} disabled={c.ro} value={t.verify ?? "none"} onChange={(v) => c.set(t.id, (x) => { if (v === "none") delete x.verify; else x.verify = v })} options={VERIFY} />
        <div className="mt-4">
          <SettingRow label="Secret">
            <div className="flex min-h-8 flex-wrap items-center gap-x-3 gap-y-1">
              {w && !w.secret_hint ? <span className="text-sm text-muted-foreground">No secret</span> : <Mono className="text-sm">{w?.secret_hint ?? "whsec_..."}</Mono>}
              {w?.secret_created_at && <span className="text-xs text-muted-foreground">Created {absolute(w.secret_created_at).split(",")[0]}</span>}
              {w?.previous_expires_at && <Tag>Previous valid for {until(w.previous_expires_at)}</Tag>}
              {!c.ro && <Button variant="outline" size="sm" className="ml-auto" onClick={() => setRotate(true)}><RotateCw />{w && !w.secret_hint ? "Create" : "Rotate"}</Button>}
            </div>
          </SettingRow>
        </div>
        <RotateDialog id={id} trigger={t.id} open={rotate} onOpenChange={setRotate} />
      </Part>
      <Part title="Response">
        <ChoiceCards label="Response" disabled={c.ro} value={t.respond ?? "accepted"} onChange={(v) => c.set(t.id, (x) => { x.respond = v })}
          options={[{ value: "accepted", label: "Accepted", fact: "202 right away" }, { value: "result", label: "With the result", fact: "Waits for the run" }]} />
      </Part>
      <Part title="Input mapping">
        <Input aria-label="Inputs expression" className="h-8 font-mono text-xs" disabled={c.ro} placeholder="${{ payload.data }}" value={mappingText(t.inputs)} onChange={(e) => c.set(t.id, (x) => { if (e.target.value) x.inputs = parseMapping(e.target.value); else delete x.inputs }, 400)} />
        <div className="mt-4">
          {!last && <p className="text-sm text-muted-foreground">No delivery to preview</p>}
          {last && (
            <div className="space-y-2">
              <div className="flex items-center gap-2 text-xs text-muted-foreground"><span>Preview against</span><Mono>{shortId(last.id)}</Mono><Ago iso={last.received_at} /></div>
              {!t.inputs && <p className="text-sm text-muted-foreground">No mapping</p>}
              {mapped && !mapped.ok && <p className="flex items-center gap-2 text-sm text-destructive"><CircleAlert className="size-4" />{mapped.error}</p>}
              {checked && (
                <div className="overflow-hidden rounded-lg border">
                  <Table>
                    <TableHeader><TableRow className="hover:bg-transparent"><TableHead className="pl-3">Input</TableHead><TableHead>Value</TableHead><TableHead className="pr-3 text-right">Check</TableHead></TableRow></TableHeader>
                    <TableBody>
                      {checked.rows.map((r) => {
                        const S = STATE[r.state]
                        return (
                          <TableRow key={r.field}>
                            <TableCell className="pl-3 font-medium">{r.field}</TableCell>
                            <TableCell className="max-w-0 w-full"><span className="block truncate font-mono text-xs">{show(r.value) || <span className="text-faint">–</span>}</span></TableCell>
                            <TableCell className="pr-3 text-right"><span className="inline-flex items-center gap-1.5 whitespace-nowrap"><S.icon className={cn("size-3.5", S.cls)} />{S.label}</span></TableCell>
                          </TableRow>
                        )
                      })}
                    </TableBody>
                  </Table>
                </div>
              )}
              {checked && checked.extra.length > 0 && <p className="text-xs text-muted-foreground">Not in inputs: <Mono>{checked.extra.join(", ")}</Mono></p>}
            </div>
          )}
        </div>
      </Part>
      <Part title="Deliveries" aside={<span className="text-xs text-muted-foreground tabular">{rows.length}</span>}>
        {!rows.length ? <p className="text-sm text-muted-foreground">No deliveries</p> : (
          <div className="overflow-hidden rounded-lg border">
            <Table>
              <TableHeader>
                <TableRow className="hover:bg-transparent">
                  <TableHead className="pl-3">Received</TableHead><TableHead className="max-md:hidden">ID</TableHead><TableHead className="max-sm:hidden">Verified</TableHead>
                  <TableHead>Result</TableHead><TableHead className="max-sm:hidden">HTTP</TableHead><TableHead>Run</TableHead><TableHead className="w-24 pr-3" />
                </TableRow>
              </TableHeader>
              <TableBody>
                {rows.map((d) => {
                  const O = OUTCOME[d.outcome]
                  return (
                    <TableRow key={d.id} className="h-12">
                      <TableCell className="pl-3" title={absolute(d.received_at, true)}><Ago iso={d.received_at} /></TableCell>
                      <TableCell className="max-md:hidden"><span className="flex items-center gap-1.5"><Mono className="text-xs">{shortId(d.id)}</Mono>{isTest(d) && <Tag>Test</Tag>}</span></TableCell>
                      <TableCell className="max-sm:hidden"><span className="inline-flex items-center gap-1.5 whitespace-nowrap">{d.verified ? <ShieldCheck className="size-3.5 text-success" /> : <ShieldX className="size-3.5 text-destructive" />}{d.verified ? "Verified" : "Not verified"}</span></TableCell>
                      <TableCell><span className="inline-flex items-center gap-1.5 whitespace-nowrap"><O.icon className={cn("size-3.5", O.cls)} />{O.label}</span></TableCell>
                      <TableCell className="tabular max-sm:hidden">{d.status}</TableCell>
                      <TableCell className="tabular">{d.run ? <Link to={`/workflows/${id}/runs/${d.run.number}`} className="text-link hover:underline">{d.run.number}</Link> : <span className="text-faint">–</span>}</TableCell>
                      <TableCell className="pr-3 text-right">
                        {!c.ro && d.outcome === "accepted" && <Button variant="ghost" size="sm" disabled={replay.isPending} onClick={() => replay.mutate(d.id)}><RotateCw />Replay</Button>}
                      </TableCell>
                    </TableRow>
                  )
                })}
              </TableBody>
            </Table>
          </div>
        )}
      </Part>
    </>
  )
}

// ---------- manual and event ----------

function Manual({ c }: { c: Ctx }) {
  const props = Object.entries(c.spec.inputs?.properties ?? {})
  const req = new Set(c.spec.inputs?.required ?? [])
  const who = [["Owner", [c.wf.owner]], ["Editors", c.wf.editors], ["Runners", c.wf.runners]] as const
  return (
    <>
      <Part title="Who may run">
        <Facts rows={who.filter(([, p]) => p.length).map(([k, p]) => [k, <span key={k} className="flex flex-wrap gap-x-4 gap-y-1">{p.map((x) => <span key={x.id} className="inline-flex items-center gap-1.5"><Avatar name={x.name} className="size-5 text-[9px]" />{x.name}</span>)}</span>])} />
      </Part>
      <Part title="Form">
        {!props.length ? <p className="text-sm text-muted-foreground">No inputs</p> : (
          <div className="max-w-md space-y-3" aria-label="Form preview">
            {props.map(([k, f]) => (
              <div key={k} className="space-y-1.5">
                <Label>{f.title ?? k}{!req.has(k) && <span className="font-normal text-muted-foreground"> optional</span>}</Label>
                {f.enum ? (
                  <Select disabled><SelectTrigger className="h-8 w-full"><SelectValue placeholder="Choose" /></SelectTrigger><SelectContent>{f.enum.map((o) => <SelectItem key={o} value={o}>{o}</SelectItem>)}</SelectContent></Select>
                ) : (f.maxLength ?? 0) > 200 || k === "message" ? <Textarea disabled rows={3} /> : <Input disabled className="h-8" />}
              </div>
            ))}
          </div>
        )}
      </Part>
    </>
  )
}

function Event({ t }: { t: Trigger }) {
  return <Part><Facts rows={[["Source", t.source ? <Mono key="s">{t.source}</Mono> : "–"], ["Filter", t.match ? <Mono key="m" className="text-xs">{t.match}</Mono> : "–"], ["Last match", <span key="l" className="text-faint">None</span>]]} /></Part>
}


// ---------- poll ----------

function Poll({ t, c }: { t: Trigger; c: Ctx }) {
  const text = (label: string, key: "agent" | "tool" | "every" | "items" | "key" | "start_when", opts?: { mono?: boolean; placeholder?: string }) => (
    <SettingRow label={label}>
      <Input aria-label={label} className={cn("h-8 text-xs", opts?.mono && "font-mono")} disabled={c.ro} placeholder={opts?.placeholder} value={t[key] ?? ""}
        onChange={(e) => c.set(t.id, (x) => { if (e.target.value) x[key] = e.target.value; else delete x[key] }, 400)} />
    </SettingRow>
  )
  return (
    <>
      <Part title="Source">
        <div className="space-y-2.5">
          {text("Agent", "agent")}
          {text("Tool", "tool", { mono: true, placeholder: "server/tool" })}
          {text("Every", "every", { placeholder: "5m" })}
        </div>
      </Part>
      <Part title="Rows">
        <div className="space-y-2.5">
          {text("Rows at", "items", { mono: true, placeholder: ".results" })}
          {text("Row key", "key", { mono: true, placeholder: ".id" })}
        </div>
        <div className="mt-4">
          <ChoiceCards label="Runs for" disabled={c.ro} value={t.rule ?? "added"} onChange={(v) => c.set(t.id, (x) => { x.rule = v })}
            options={[{ value: "added", label: "New rows", fact: "Once per row" }, { value: "updated", label: "Changed rows", fact: "When a watched field changes" }]} />
        </div>
      </Part>
      <Part title="Start">
        <div className="space-y-2.5">
          {text("Only when", "start_when", { mono: true, placeholder: "item.status == 'Todo'" })}
          <SettingRow label="Inputs">
            <Input aria-label="Inputs expression" className="h-8 font-mono text-xs" disabled={c.ro} placeholder="${{ item }}" value={mappingText(t.inputs)}
              onChange={(e) => c.set(t.id, (x) => { if (e.target.value) x.inputs = parseMapping(e.target.value); else delete x.inputs }, 400)} />
          </SettingRow>
        </div>
      </Part>
    </>
  )
}
