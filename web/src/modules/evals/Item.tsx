import { Check, ChevronDown, ChevronRight, MessageSquarePlus, X } from "lucide-react"
import { useState } from "react"

import { Money } from "@/components/common"
import { cn } from "@/lib/utils"
import { duration } from "@/lib/format"
import { ChatThread, fromEvents } from "./previews"
import type { Skin } from "./previews/parts"
import type { Anchor, Case, Column, Origin, Output, Platform, Score, WorldCall } from "./types"
import { FileDiff } from "./viewers"
import { Artifact } from "./viewers"
import { Payload } from "./viewers/Json"

const ORIGIN: Record<Origin, string> = {
  recorded: "text-muted-foreground", overlay: "text-muted-foreground", simulated: "border-warning/50 text-warning", denied: "border-destructive/50 text-destructive",
  passthrough: "text-muted-foreground", sandbox: "text-muted-foreground", live: "text-muted-foreground",
}

export const names = (c?: Case): Record<string, string> =>
  Object.fromEntries([...(c?.surface.participants ?? []), ...(c ? [c.surface.bot] : [])].map((p) => [p.id, p.display_name]))

const Section = ({ title, children, aside }: { title: string; children: React.ReactNode; aside?: React.ReactNode }) => (
  <section className="border-t px-3 py-3">
    <div className="mb-2 flex items-center justify-between"><h3 className="text-xs font-medium text-muted-foreground">{title}</h3>{aside}</div>
    {children}
  </section>
)

/** The last message the agent sent, for a text diff between arms. */
const reply = (col: Column, bot: string) => fromEvents(col.item.chat ?? []).filter((m) => m.author === bot && !m.deleted && m.text).at(-1)?.text ?? ""

function Calls({ calls, pin }: { calls: WorldCall[]; pin?: (a: Omit<Anchor, "item_id">) => void }) {
  const [open, setOpen] = useState<number | null>(null)
  if (!calls.length) return <div className="text-sm text-faint">No calls</div>
  return (
    <ol className="space-y-1">
      {calls.map((c) => (
        <li key={c.seq} className="group/call">
          <div className="flex h-8 items-center gap-2 rounded-md px-1.5 text-left text-xs hover:bg-accent">
            <button className="flex min-w-0 flex-1 items-center gap-2 text-left" aria-expanded={open === c.seq} onClick={() => setOpen(open === c.seq ? null : c.seq)}>
              {open === c.seq ? <ChevronDown className="size-3 shrink-0" /> : <ChevronRight className="size-3 shrink-0" />}
              <span className="w-5 shrink-0 text-faint tabular">{c.seq}</span>
              <span className="truncate font-mono">{[c.service ?? c.server, c.tool].filter(Boolean).join(".") || c.surface}</span>
              {c.effects?.[0] && <span className="truncate text-muted-foreground">{c.effects[0].verb}</span>}
            </button>
            <span className={cn("shrink-0 rounded border px-1.5 text-[11px]", ORIGIN[c.origin])}>{c.origin}</span>
            {pin && <button aria-label="Comment" className="opacity-0 group-hover/call:opacity-100 focus-visible:opacity-100" onClick={() => pin({ target: "world_call", ref: String(c.seq) })}><MessageSquarePlus className="size-3.5" /></button>}
          </div>
          {open === c.seq && <div className="space-y-2 pb-2 pl-6"><Payload value={c.request ?? null} /><Payload value={c.response ?? c.reason ?? null} /></div>}
        </li>
      ))}
    </ol>
  )
}

function Scores({ scores }: { scores: Score[] }) {
  if (!scores.length) return <div className="text-sm text-faint">Not scored</div>
  return (
    <ul className="space-y-1.5">
      {scores.map((s) => (
        <li key={s.id} className="flex items-start gap-2 text-sm">
          <span className="mt-0.5 shrink-0">{s.value.unknown ? <span className="text-faint">?</span> : s.value.pass === false ? <X className="size-4 text-destructive" /> : s.value.pass || s.value.score !== undefined ? <Check className="size-4 text-success" /> : null}</span>
          <span className="min-w-0 flex-1">
            <span>{s.criterion}</span>{s.value.score !== undefined && <span className="tabular text-muted-foreground">, {s.value.score}</span>}
            <span className="text-xs text-faint">, {s.grader.kind}</span>
            {s.rationale && <span className="block text-xs text-muted-foreground">{s.rationale}</span>}
          </span>
        </li>
      ))}
    </ul>
  )
}

function Files({ outputs, others, onPin }: { outputs: Output[]; others?: Output[]; onPin?: (a: Omit<Anchor, "item_id">) => void }) {
  const [open, setOpen] = useState<string | null>(outputs[0]?.id ?? null)
  if (!outputs.length) return <div className="text-sm text-faint">No files</div>
  return (
    <div className="space-y-1">
      {outputs.map((o) => (
        <div key={o.id}>
          <button className="flex h-8 w-full items-center gap-2 rounded-md px-1.5 text-left text-sm hover:bg-accent" aria-expanded={open === o.id} onClick={() => setOpen(open === o.id ? null : o.id)}>
            {open === o.id ? <ChevronDown className="size-3" /> : <ChevronRight className="size-3" />}<span className="truncate">{o.name}</span>
          </button>
          {open === o.id && <div className="py-1"><Artifact o={o} other={others?.find((x) => x.name === o.name)} pin={onPin} /></div>}
        </div>
      ))}
    </div>
  )
}

/** One arm's result on a case: chat, files, calls and scores. With `other` and `diff`, the difference to the other arm. */
export function ItemColumn({ col, other, diff, platform, case: c, onPin }: {
  col: Column; other?: Column; diff?: boolean; platform: Platform; case?: Case; onPin?: (a: Anchor) => void
}) {
  const { item } = col
  const bot = c?.surface.bot.id ?? "bot"
  const pin = onPin && ((a: Omit<Anchor, "item_id">) => onPin({ ...a, item_id: item.id }))
  const skin: Skin = { people: names(c), pin, me: c?.surface.participants.find((p) => p.is_requester)?.id }
  return (
    <div className="min-w-0 overflow-hidden rounded-lg border bg-card" data-column={col.label}>
      <div className="flex h-11 items-center gap-2 px-3">
        <span className="flex size-6 items-center justify-center rounded-md bg-accent text-xs font-semibold">{col.label.slice(0, 1)}</span>
        {col.arm && <span className="truncate text-sm font-medium">{col.arm.label}{col.arm.kind === "baseline" && <span className="font-normal text-muted-foreground">, baseline</span>}</span>}
        <span className="ml-auto flex items-center gap-2 text-xs text-muted-foreground tabular">
          {item.status !== "done" && <span className={item.status === "error" ? "text-destructive" : ""}>{item.status}</span>}
          {item.usage?.duration_ms != null && <span>{duration(item.usage.duration_ms)}</span>}
          {item.usage?.cost_usd != null && <Money usd={item.usage.cost_usd} />}
        </span>
      </div>
      <Section title={diff && other ? "Reply difference" : "Chat"}>
        {diff && other ? <FileDiff before={reply(other, bot)} after={reply(col, bot)} name="reply.md" /> : <ChatThread platform={platform} messages={fromEvents(item.chat ?? [], item.outputs)} skin={skin} />}
      </Section>
      <Section title="Files"><Files outputs={item.outputs ?? []} others={diff ? other?.item.outputs : undefined} onPin={pin} /></Section>
      <Section title="Calls" aside={item.enclosure?.simulated_calls ? <span className="text-xs text-warning">{item.enclosure.simulated_calls} simulated</span> : null}><Calls calls={item.world_calls ?? []} pin={pin} /></Section>
      <Section title="Scores"><Scores scores={col.scores} /></Section>
    </div>
  )
}
