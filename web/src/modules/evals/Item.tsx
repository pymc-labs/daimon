import { Check, ChevronDown, ChevronRight, MessageSquarePlus, X } from "lucide-react"
import { useState } from "react"

import { Money } from "@/components/common"
import { cn } from "@/lib/utils"
import { duration } from "@/lib/format"
import { isCase } from "./bits"
import { ChatThread, fromEvents } from "./previews"
import type { Skin } from "./previews/parts"
import type { Anchor, Case, CaseView, Origin, Output, Platform, RunItem, ScoreLine, WorldCall } from "./types"
import { FileDiff } from "./viewers"
import { Artifact } from "./viewers"
import { Payload } from "./viewers/Json"

const ORIGIN: Record<Origin, string> = {
  recorded: "text-muted-foreground", overlay: "text-muted-foreground", simulated: "border-warning/50 text-warning", denied: "border-destructive/50 text-destructive",
  passthrough: "text-muted-foreground", sandbox: "text-muted-foreground", live: "text-muted-foreground",
}

export const names = (c?: Case): Record<string, string> =>
  Object.fromEntries([...(c?.surface.participants ?? []), ...(c ? [c.surface.bot] : [])].map((p) => [p.id, p.display_name]))
/** Who wrote what, from the full case or from the reviewers' cut of it. */
export const peopleOf = (c?: Case | CaseView): Record<string, string> => (!c ? {} : isCase(c) ? names(c) : c.participants)

const Section = ({ title, children, aside }: { title: string; children: React.ReactNode; aside?: React.ReactNode }) => (
  <section className="border-t px-3 py-3">
    <div className="mb-2 flex items-center justify-between"><h3 className="text-xs font-medium text-muted-foreground">{title}</h3>{aside}</div>
    {children}
  </section>
)

function Calls({ calls, pin, blind }: { calls: WorldCall[]; pin?: (a: Omit<Anchor, "item_id">) => void; blind: boolean }) {
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
            {!blind && <span className={cn("shrink-0 rounded border px-1.5 text-[11px]", ORIGIN[c.origin])}>{c.origin}</span>}
            {pin && <button aria-label="Comment" className="opacity-0 group-hover/call:opacity-100 focus-visible:opacity-100" onClick={() => pin({ target: "world_call", ref: String(c.seq) })}><MessageSquarePlus className="size-3.5" /></button>}
          </div>
          {open === c.seq && <div className="space-y-2 pb-2 pl-6"><Payload value={c.request ?? null} /><Payload value={c.response ?? c.reason ?? null} /></div>}
        </li>
      ))}
    </ol>
  )
}

function Scores({ scores }: { scores: ScoreLine[] }) {
  if (!scores.length) return <div className="text-sm text-faint">Not scored</div>
  return (
    <ul className="space-y-1.5">
      {scores.map((s, i) => (
        <li key={`${s.criterion}:${s.grader}:${i}`} className="flex items-start gap-2 text-sm">
          <span className="mt-0.5 shrink-0">{s.unknown ? <span className="text-faint">?</span> : s.passed === false ? <X className="size-4 text-destructive" /> : s.passed || s.score != null ? <Check className="size-4 text-success" /> : null}</span>
          <span className="min-w-0 flex-1">
            <span>{s.criterion}</span>{s.score != null && <span className="tabular text-muted-foreground">, {s.score}</span>}
            <span className="text-xs text-faint">, {s.grader}</span>
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

/** One answer on a case: chat, files, calls and, when named, scores. Blind hides everything that could tell the arms apart: cost, time, origins and simulated counts.
 * With `diff`, the reply and files are set against another answer's. `anchorRef` is what comments name this column by. */
export function ItemColumn({ label, title, item, scores, blind, diff, platform, case: c, anchorRef, onPin }: {
  label: string; title?: string; item: RunItem; scores?: ScoreLine[]; blind: boolean; diff?: { before: string; after: string; outputs?: Output[] }
  platform: Platform; case?: Case | CaseView; anchorRef?: string; onPin?: (a: Anchor) => void
}) {
  const pin = onPin && ((a: Omit<Anchor, "item_id">) => onPin({ ...a, item_id: anchorRef ?? item.id }))
  const skin: Skin = { people: peopleOf(c), pin, me: c && isCase(c) ? c.surface.participants.find((p) => p.is_requester)?.id : undefined }
  return (
    <div className="min-w-0 overflow-hidden rounded-lg border bg-card" data-column={label}>
      <div className="flex h-11 items-center gap-2 px-3">
        <span className="flex h-6 min-w-6 items-center justify-center rounded-md bg-accent px-1.5 text-xs font-semibold">{blind ? label : label.slice(0, 1)}</span>
        {title && !blind && <span className="truncate text-sm font-medium">{title}</span>}
        <span className="ml-auto flex items-center gap-2 text-xs text-muted-foreground tabular">
          {item.status !== "done" && (blind ? <span>no answer</span> : <span className={item.status === "error" ? "text-destructive" : ""}>{item.status}</span>)}
          {!blind && item.usage?.duration_ms != null && <span>{duration(item.usage.duration_ms)}</span>}
          {!blind && item.usage?.cost_usd != null && <Money usd={item.usage.cost_usd} />}
        </span>
      </div>
      <Section title={diff ? "Reply difference" : "Chat"}>
        {diff ? <FileDiff before={diff.before} after={diff.after} name="reply.md" /> : <ChatThread platform={platform} messages={fromEvents(item.chat ?? [], item.outputs)} skin={skin} />}
      </Section>
      <Section title="Files"><Files outputs={item.outputs ?? []} others={diff?.outputs} onPin={pin} /></Section>
      <Section title="Calls" aside={!blind && item.enclosure?.simulated_calls ? <span className="text-xs text-warning">{item.enclosure.simulated_calls} simulated</span> : null}><Calls calls={item.world_calls ?? []} pin={pin} blind={blind} /></Section>
      {scores && !blind && <Section title="Scores"><Scores scores={scores} /></Section>}
    </div>
  )
}
