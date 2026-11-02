import { useMutation } from "@tanstack/react-query"
import { Check, ChevronRight, CircleAlert, Download, FileSpreadsheet, FileText, Loader2, Paperclip, ThumbsDown, ThumbsUp } from "lucide-react"
import { useState } from "react"
import ReactMarkdown from "react-markdown"
import remarkGfm from "remark-gfm"

import { AgentAvatar, CopyButton } from "@/components/common"
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip"
import { api } from "@/lib/api"
import type { AgentRef } from "@/lib/types"
import { usd } from "@/lib/format"
import { cn } from "@/lib/utils"
import { CardView } from "./Cards"
import { bytes, secs } from "./format"
import type { Card, CardAnswer, FileOut, Msg, Step } from "./types"

export function UserMessage({ m }: { m: Msg }) {
  return (
    <div className="flex flex-col items-end gap-1.5">
      {m.attachments?.map((f, i) => (
        <span key={`${i}-${f.name}`} className="inline-flex h-7 items-center gap-1.5 rounded-md border px-2 text-xs">
          <Paperclip className="size-3.5 text-muted-foreground" />{f.name}<span className="text-faint tabular">{bytes(f.size)}</span>
        </span>
      ))}
      <div className="max-w-[85%] rounded-xl bg-muted px-4 py-2.5 text-[15px] leading-6 whitespace-pre-wrap">{m.content}</div>
    </div>
  )
}

/** While streaming, hold back a table until its separator row arrives, and its unfinished last row. */
function settled(text: string) {
  const k = text.lastIndexOf("\n\n")
  const i = k < 0 ? 0 : k + 2
  const lines = text.slice(i).split("\n")
  if (!lines[0]!.startsWith("|")) return text
  if (lines.length < 2 || !/^\|[\s:|-]+\|$/.test(lines[1]!)) return text.slice(0, i)
  return lines.at(-1)!.endsWith("|") ? text : text.slice(0, i) + lines.slice(0, -1).join("\n")
}

function Markdown({ text }: { text: string }) {
  return (
    <div className="prose-md">
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        urlTransform={(u) => u}
        components={{
          a: ({ href, children }) => <a href={href} target="_blank" rel="noreferrer">{children}</a>,
          // An image in a reply is a request to its host from the reader's browser: show a link instead.
          img: ({ src, alt }) => /^https?:\/\//i.test(src ?? "")
            ? <a href={src} target="_blank" rel="noreferrer">{alt || src}</a>
            : <span>{alt}</span>,
          table: ({ children }) => <div className="my-1 overflow-x-auto rounded-lg border px-3 py-1 [&_tr:last-child_td]:border-b-0"><table>{children}</table></div>,
        }}
      >
        {text}
      </ReactMarkdown>
    </div>
  )
}

function StepRow({ s }: { s: Step }) {
  return (
    <div className="flex min-w-0 items-center gap-2 text-xs">
      {s.status === "running" ? <Loader2 className="size-3.5 shrink-0 animate-spin text-muted-foreground" />
        : s.status === "error" ? <CircleAlert className="size-3.5 shrink-0 text-destructive" />
          : <Check className="size-3.5 shrink-0 text-success" />}
      <span className={cn("shrink-0 font-medium", s.status === "running" ? "shimmer" : "text-foreground/80")}>{s.label}</span>
      {s.detail && <span className="min-w-0 truncate text-faint">{s.detail}</span>}
      <span className="ml-auto shrink-0 text-faint tabular">{s.ms != null ? secs(s.ms) : s.status === "error" ? "Stopped" : ""}</span>
    </div>
  )
}

/** Tool calls: listed while the turn waits on them, then collapsed to one line. */
function Activity({ steps, streaming, started }: { steps: Step[]; streaming: boolean; started: boolean }) {
  const [user, setUser] = useState<boolean | null>(null)
  const open = user ?? (streaming && !started)
  if (!steps.length) return streaming && !started ? <div className="mb-2 text-sm"><span className="shimmer">Working…</span></div> : null
  const total = steps.reduce((a, s) => a + (s.ms ?? 0), 0)
  return (
    <div className="mb-2.5 text-sm text-muted-foreground">
      <button onClick={() => setUser(!open)} className="inline-flex items-center gap-1 hover:text-foreground">
        <span className={cn(streaming && !started && "shimmer")}>{`Used ${steps.length} tool${steps.length === 1 ? "" : "s"}`}</span>
        {!streaming && <span className="text-faint tabular">{secs(total)}</span>}
        <ChevronRight className={cn("size-3.5 transition-transform", open && "rotate-90")} />
      </button>
      {open && <div className="mt-2 space-y-2 rounded-lg border px-3 py-2.5">{steps.map((s) => <StepRow key={s.id} s={s} />)}</div>}
    </div>
  )
}

function FileCard({ f }: { f: FileOut }) {
  const Icon = f.kind === "csv" || f.kind === "xlsx" ? FileSpreadsheet : FileText
  return (
    <a href={`/api/chat/files/${f.id}`} download={f.name} className="mt-4 flex h-12 w-full max-w-sm items-center gap-3 rounded-lg border px-3 hover:bg-accent">
      <span className="grid size-8 shrink-0 place-items-center rounded-md bg-muted text-muted-foreground"><Icon className="size-4" /></span>
      <span className="min-w-0 flex-1">
        <span className="block truncate text-sm font-medium">{f.name}</span>
        <span className="flex gap-1.5 text-xs text-muted-foreground"><span>{f.kind.toUpperCase()}</span><span className="tabular">{bytes(f.size)}</span></span>
      </span>
      <Download className="size-4 shrink-0 text-muted-foreground" />
    </a>
  )
}

function Feedback({ m }: { m: Msg }) {
  const [rating, setRating] = useState(m.feedback ?? null)
  const send = useMutation({
    mutationFn: (r: "up" | "down" | "none") => api.post(`/api/chat/messages/${m.id}/feedback`, { rating: r }),
    onSuccess: (_, r) => setRating(r === "none" ? null : r),
  })
  const btn = "inline-flex size-7 items-center justify-center rounded-md text-muted-foreground hover:bg-accent hover:text-foreground"
  return (
    <>
      <button className={cn(btn, rating === "up" && "text-foreground")} aria-label="Good answer" onClick={() => send.mutate(rating === "up" ? "none" : "up")}>
        <ThumbsUp className={cn("size-3.5", rating === "up" && "fill-current")} />
      </button>
      <button className={cn(btn, rating === "down" && "text-foreground")} aria-label="Bad answer" onClick={() => send.mutate(rating === "down" ? "none" : "down")}>
        <ThumbsDown className={cn("size-3.5", rating === "down" && "fill-current")} />
      </button>
    </>
  )
}

export function AssistantMessage({ m, agent, first, last, busy, onAnswer }: {
  m: Msg; agent: AgentRef; first: boolean; last: boolean; busy: boolean
  onAnswer: (card: Card, a: CardAnswer) => void
}) {
  const done = !m.streaming && !!m.content && !m.card && !m.stopped
  return (
    <div className="group/msg">
      {first && (
        <div className="mb-2.5 flex items-center gap-2 text-xs">
          <AgentAvatar src={agent.avatar} name={agent.name} className="size-5" />
          <span className="font-medium">{agent.name}</span>
        </div>
      )}
      <Activity steps={m.steps ?? []} streaming={!!m.streaming} started={!!m.content} />
      {m.content && (
        <div className="text-[15px] leading-6">
          <Markdown text={m.streaming ? settled(m.content) : m.content} />
        </div>
      )}
      {m.streaming && m.content && <span className="mt-2 inline-block size-1.5 animate-pulse rounded-full bg-foreground/60" />}
      {!m.streaming && m.files?.map((f) => <FileCard key={f.id} f={f} />)}
      {m.card && (
        <div className={m.content ? "mt-3" : ""}>
          <CardView card={m.card} busy={busy} onAnswer={(a) => onAnswer(m.card!, a)} />
        </div>
      )}
      {(done || m.stopped) && (
        <div className="mt-3 flex items-center gap-1">
          {done && <CopyButton text={m.content} label="Copy answer" />}
          {done && m.id && !m.id.startsWith("tmp") && <Feedback m={m} />}
          {m.stopped && <span className="px-1 text-xs text-faint">Stopped</span>}
          {m.cost_usd != null && m.cost_usd > 0 && (
            <Tooltip>
              <TooltipTrigger asChild>
                <span className={cn("ml-auto flex gap-2 text-xs text-faint tabular", !last && "opacity-0 transition-opacity group-hover/msg:opacity-100")}>
                  <span>{usd(m.cost_usd)}</span>{!!m.ms && <span>{secs(m.ms)}</span>}
                </span>
              </TooltipTrigger>
              <TooltipContent>Cost of this reply</TooltipContent>
            </Tooltip>
          )}
        </div>
      )}
    </div>
  )
}
