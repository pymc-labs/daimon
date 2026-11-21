import { CircleCheck, CircleX, ListChecks, MessageCircleQuestion, Rocket, ShieldCheck, Check } from "lucide-react"
import { useState } from "react"
import { Link } from "react-router"

import { Ago, Prose } from "@/components/common"
import { Drawer } from "@/components/Drawer"
import { FilterBar } from "@/components/filters"
import { Facts, PanelHead, PanelSection, Tag } from "@/components/panel"
import { QueryState } from "@/components/QueryState"
import { Page } from "@/components/shell/Page"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table"
import { Textarea } from "@/components/ui/textarea"
import { ToggleGroup, ToggleGroupItem } from "@/components/ui/toggle-group"
import { rowProps, useParam } from "@/lib/filters"
import { plural } from "@/lib/format"
import { cn } from "@/lib/utils"
import { useCatalog, useDecide, useInbox, useInboxItem, useNow } from "./api"
import { GRANT } from "./kinds"
import { choice, until } from "./run/lib"
import { FileList, JsonTree } from "./run/parts"
import { answerRows, sentRows } from "./run/rows"
import type { Field, InboxDetail, InboxItem, InboxKind } from "./types"

const KIND: Record<InboxKind, { icon: typeof ShieldCheck; label: string }> = {
  approval: { icon: ShieldCheck, label: "Approval" },
  question: { icon: MessageCircleQuestion, label: "Question" },
  proposals: { icon: ListChecks, label: "Proposals" },
  publish: { icon: Rocket, label: "Publish" },
}

/** Who asks: an agent's key as its name, else the name the server gave. */
const useAsker = () => {
  const { data: catalog } = useCatalog()
  return (by: string) => catalog?.agents.find((a) => a.key === by)?.name ?? by
}

const soon = (iso: string, now: number) => new Date(iso).getTime() - now < 30 * 60_000

/** Everything waiting on you, and what was decided. */
export function InboxPage() {
  const query = useInbox()
  const asker = useAsker()
  const [view, setView] = useParam("view")
  const decided = view === "decided"
  const [, setItem] = useParam("item")
  const now = useNow(true, 30_000)
  const rows = (query.data ?? []).filter((i) => !!i.decision === decided)
  return (
    <Page crumbs={[{ label: "Inbox" }]} flush>
      <FilterBar>
        <ToggleGroup type="single" value={decided ? "decided" : "pending"} onValueChange={(x) => x && setView(x === "pending" ? null : x)} variant="segmented" size="sm">
          <ToggleGroupItem value="pending" className="px-3 text-xs">Pending</ToggleGroupItem>
          <ToggleGroupItem value="decided" className="px-3 text-xs">Decided</ToggleGroupItem>
        </ToggleGroup>
        <span className="ml-auto text-xs text-muted-foreground tabular">{plural(rows.length, "item")}</span>
      </FilterBar>
      <QueryState query={query} className="px-6" isEmpty={() => !rows.length} empty={decided ? "Nothing decided yet" : "Nothing waiting"}>{() => (
        <Table>
          <TableHeader>
            <TableRow className="hover:bg-transparent">
              <TableHead className="pl-4 lg:pl-6">Item</TableHead>
              <TableHead className="max-md:hidden">Step</TableHead>
              <TableHead className="w-20 max-sm:hidden">Run</TableHead>
              <TableHead className="max-lg:hidden">Asked by</TableHead>
              <TableHead className="max-sm:hidden">{decided ? "Decided" : "Waiting"}</TableHead>
              <TableHead className="pr-4 text-right lg:pr-6">{decided ? "Outcome" : "Expires in"}</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {rows.map((i) => {
              const K = KIND[i.kind]
              return (
                <TableRow key={i.id} {...rowProps(() => setItem(i.id))} className="cursor-pointer">
                  <TableCell className="max-w-0 w-full pl-4 lg:pl-6">
                    <span className="flex items-center gap-2"><K.icon className="size-4 shrink-0 text-muted-foreground" /><span className="truncate font-medium">{i.title}</span></span>
                    <span className="block truncate pl-6 text-xs text-muted-foreground">{i.workflow.name}</span>
                  </TableCell>
                  <TableCell className="whitespace-nowrap text-muted-foreground max-md:hidden">{i.step_title ?? i.step ?? "–"}</TableCell>
                  <TableCell className="tabular max-sm:hidden">{i.run?.number ?? "–"}</TableCell>
                  <TableCell className="whitespace-nowrap text-muted-foreground max-lg:hidden">{asker(i.asked_by)}</TableCell>
                  <TableCell className="whitespace-nowrap max-sm:hidden"><Ago iso={i.decision?.at ?? i.since} /></TableCell>
                  <TableCell className="whitespace-nowrap pr-4 text-right tabular lg:pr-6">
                    {i.decision ? <Outcome i={i} /> : i.expires_at ? <span className={cn(soon(i.expires_at, now) ? "text-warning" : "text-muted-foreground")}>{until(i.expires_at, now)}</span> : <span className="text-faint">–</span>}
                  </TableCell>
                </TableRow>
              )
            })}
          </TableBody>
        </Table>
      )}</QueryState>
      <Drawer param="item" title="Inbox" ids={rows.map((i) => i.id)} wide>{(id) => <ItemSheet id={id} />}</Drawer>
    </Page>
  )
}

const first = (n: string) => n.split(" ")[0]

/** "Approved by Maya", or the matching reject, with when and the comment. */
function Outcome({ i }: { i: InboxItem }) {
  const d = i.decision!
  const bad = d.route === "reject"
  const Icon = bad ? CircleX : CircleCheck
  return <span className="inline-flex items-center gap-1.5 text-foreground"><Icon className={cn("size-3.5", bad ? "text-destructive" : "text-success")} />{d.label} by {first(d.by.name)}</span>
}

const multiline = (f: Field, v: unknown) => typeof v === "string" && (v.includes("\n") || v.length > 90) || f.maxLength != null && f.maxLength > 200

/** The form of an approval or question: choices as large cards, the rest as inputs. */
function FormFields({ d, vals, set, off }: { d: InboxDetail; vals: Record<string, unknown>; set: (k: string, v: unknown) => void; off: boolean }) {
  const props = Object.entries(d.form?.properties ?? {})
  return (
    <div className="space-y-4">
      {props.map(([k, f]) => {
        const label = f.title ?? "Answer"
        const v = vals[k]
        if (f.enum) return (
          <div key={k} className="space-y-1.5">
            {f.title && <Label>{label}</Label>}
            <div className="grid gap-2 sm:grid-cols-3" role="radiogroup" aria-label={label}>
              {f.enum.map((o) => (
                <button key={o} type="button" role="radio" aria-checked={v === o} disabled={off} onClick={() => set(k, o)} className={cn(choice(v === o), "relative min-h-11 justify-center pr-9 disabled:opacity-60")}>
                  <span className="text-sm font-medium">{o}</span>
                  {v === o && <Check className="absolute right-3 top-1/2 size-4 -translate-y-1/2 text-link" />}
                </button>
              ))}
            </div>
          </div>
        )
        return (
          <div key={k} className="space-y-1.5">
            {f.title ? <Label htmlFor={`f-${k}`}>{label}</Label> : null}
            {multiline(f, d.values[k]) || k === "body"
              ? <Textarea id={`f-${k}`} aria-label={f.title ? undefined : label} value={String(v ?? "")} disabled={off} rows={8} onChange={(e) => set(k, e.target.value)} />
              : <Input id={`f-${k}`} aria-label={f.title ? undefined : label} value={String(v ?? "")} disabled={off} onChange={(e) => set(k, e.target.value)} />}
          </div>
        )
      })}
    </div>
  )
}

function ItemSheet({ id }: { id: string }) {
  const query = useInboxItem(id)
  const d = query.data
  return (
    <QueryState query={query} className="px-5 pt-5">{() => d && <Detail key={d.id} d={d} />}</QueryState>
  )
}

function Detail({ d }: { d: InboxDetail }) {
  const [, setItem] = useParam("item")
  const K = KIND[d.kind]
  const asker = useAsker()
  const done = !!d.decision
  const [vals, setVals] = useState<Record<string, unknown>>(d.values)
  const [picked, setPicked] = useState(() => new Set(d.proposals.map((p) => p.id)))
  const [asking, setAsking] = useState<string | null>(null)
  const [comment, setComment] = useState("")
  const decide = useDecide(d.id)
  const send = (route: string, withComment?: boolean) => decide.mutate({
    route, comment: withComment ? comment.trim() : undefined, selected: d.kind === "proposals" ? [...picked] : undefined, values: d.form ? vals : undefined,
  })
  const ask = d.routes.find((r) => r.name === asking)
  const pick = (pid: string) => setPicked((s) => { const n = new Set(s); if (n.has(pid)) n.delete(pid); else n.add(pid); return n })
  const req = d.publish
  return (
    <div className="flex min-h-full flex-col">
      <PanelHead icon={<K.icon className="size-4" />} title={d.title} subtitle={d.workflow.name} tags={<Tag>{K.label}</Tag>} />
      {d.decision && (
        <PanelSection>
          <div className="flex items-start gap-2 text-sm">
            {d.decision.route === "reject" ? <CircleX className="mt-0.5 size-4 shrink-0 text-destructive" /> : <CircleCheck className="mt-0.5 size-4 shrink-0 text-success" />}
            <div className="min-w-0">
              <div className="font-medium">{d.decision.label} by {first(d.decision.by.name)} <span className="font-normal text-muted-foreground"><Ago iso={d.decision.at} /></span></div>
              {d.decision.comment && <p className="mt-1 text-muted-foreground">{d.decision.comment}</p>}
            </div>
          </div>
        </PanelSection>
      )}
      <PanelSection>
        <Facts rows={[
          ["Workflow", <Link key="w" to={`/workflows/${d.workflow.id}`} className="text-link hover:underline">{d.workflow.name}</Link>],
          ["Run", d.run && <Link key="r" to={`/workflows/${d.workflow.id}/runs/${d.run.number}${d.step ? `?step=${encodeURIComponent(d.step)}` : ""}`} className="text-link hover:underline tabular">{d.run.number}</Link>],
          ["Step", d.step_title ?? d.step],
          ["Asked by", asker(d.asked_by)],
          [done ? "Asked" : "Waiting since", <Ago key="s" iso={d.since} />],
          ["Expires", d.expires_at && !done && `In ${until(d.expires_at)}`],
          ...sentRows(d.sent),
          ["Attached", d.attachments.length > 0 && <div key="a" className="-my-1"><FileList files={d.attachments} /></div>],
          ...(d.answer?.text || d.answer?.files.length || d.answer?.labels ? answerRows(d.answer) : []),
        ]} />
      </PanelSection>
      {d.question && <PanelSection title="Question"><p className="text-sm">{d.question}</p></PanelSection>}
      {req && (
        <PanelSection title="Publish request">
          <Facts rows={[["Version", `v${req.version}`], ["Note", req.note || "–"], ["Requested by", req.by.name]]} />
          {req.grants.length > 0 && (
            <ul className="mt-3 space-y-1.5 text-sm">
              {req.grants.map((g, i) => <li key={i} className="flex gap-2"><ShieldCheck className="mt-0.5 size-4 shrink-0 text-warning" /><span><span className="font-medium">{GRANT[g.kind]}</span> <span className="text-muted-foreground">{g.detail}</span></span></li>)}
            </ul>
          )}
        </PanelSection>
      )}
      {d.show && <PanelSection title="Shown to you"><div className="rounded-lg border bg-muted/40 px-4 py-3"><Prose text={d.show} breaks /></div></PanelSection>}
      {d.proposals.length > 0 && (
        <PanelSection title={`Proposals, ${picked.size} of ${d.proposals.length} selected`}>
          <div className="space-y-2">
            {d.proposals.map((p) => {
              const on = picked.has(p.id)
              return (
                <button key={p.id} type="button" role="checkbox" aria-checked={on} disabled={done} onClick={() => pick(p.id)} className={cn(choice(on), "relative w-full pr-12 disabled:opacity-60")}>
                  <span className="text-sm font-medium">{p.title}</span>
                  <span className="text-sm text-muted-foreground">{p.detail}</span>
                  <span className="font-mono text-xs text-faint">{p.tool}</span>
                  <span className={cn("absolute right-4 top-3.5 flex size-5 items-center justify-center rounded-full border", on ? "border-link bg-link text-white" : "border-border-strong")}>{on && <Check className="size-3.5" />}</span>
                </button>
              )
            })}
          </div>
        </PanelSection>
      )}
      {d.bound.length > 0 && (
        <PanelSection title="Exactly what runs on approval">
          <div className="space-y-3">
            {d.bound.map((b, i) => (
              <div key={i} className="rounded-lg border">
                <div className="flex items-center gap-2 border-b px-3 py-2 text-sm"><span className="font-medium">{b.label}</span><span className="font-mono text-xs text-muted-foreground">{b.tool}</span><span className="ml-auto text-xs text-muted-foreground">{b.step}</span></div>
                <div className="px-3 py-2.5"><JsonTree value={{ ...b.args, ...(d.form && "body" in b.args && typeof vals.body === "string" ? { body: vals.body } : {}) }} root="args" /></div>
              </div>
            ))}
          </div>
        </PanelSection>
      )}
      {d.form && !done && <PanelSection title={d.kind === "question" ? "Answer" : "Edit before sending"}><FormFields d={d} vals={vals} set={(k, v) => setVals((x) => ({ ...x, [k]: v }))} off={decide.isPending} /></PanelSection>}
      {!done && (
        <div className="sticky bottom-0 mt-auto space-y-3 border-t bg-popover px-5 py-3">
          {ask && (
            <div className="space-y-1.5">
              <Label htmlFor="ib-comment">Comment</Label>
              <Textarea id="ib-comment" autoFocus value={comment} rows={3} onChange={(e) => setComment(e.target.value)} />
            </div>
          )}
          <div className="flex justify-end gap-2 max-sm:[&>*]:flex-1">
            {ask ? (
              <>
                <Button variant="ghost" onClick={() => { setAsking(null); setComment("") }}>Back</Button>
                <Button disabled={decide.isPending || !comment.trim()} onClick={() => send(ask.name, true)}>{ask.label}</Button>
              </>
            ) : (
              <>
                <Button variant="ghost" onClick={() => setItem(null)}>Later</Button>
                {d.routes.map((r) => (
                  <Button key={r.name} variant={r.primary ? "default" : "outline"} disabled={decide.isPending || (r.primary && d.kind === "proposals" && !picked.size)} onClick={() => (r.comment ? setAsking(r.name) : send(r.name))}>{r.label}</Button>
                ))}
              </>
            )}
          </div>
          {decide.error && <p className="text-right text-sm text-destructive">{decide.error.message}</p>}
        </div>
      )}
    </div>
  )
}
