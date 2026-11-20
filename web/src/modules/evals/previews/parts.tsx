import { MessageSquarePlus, Paperclip } from "lucide-react"

import { Prose } from "@/components/common"
import { Button } from "@/components/ui/button"
import { cn } from "@/lib/utils"
import type { ButtonAction, CardButton, NeutralCard } from "../types"
import type { Msg, Pin } from "./timeline"

/** What each skin draws around the shared parts. */
export type Skin = { people: Record<string, string>; pin?: Pin; me?: string }

export const nameOf = (s: Skin, id: string) => s.people[id] ?? id
const ANSWER: Partial<Record<ButtonAction, string>> = { approve: "Approved", deny: "Denied", always: "Always allowed" }

export function Body({ m }: { m: Msg }) {
  if (m.deleted) return <span className="text-sm italic text-faint">Message deleted</span>
  return (
    <>
      {m.text && (m.markdown ? <Prose text={m.text} breaks className="text-sm leading-[1.4]" /> : <p className="whitespace-pre-wrap break-words text-sm">{m.text}</p>)}
      {m.edited && <span className="text-[11px] text-faint">(edited)</span>}
    </>
  )
}

export function Files({ m }: { m: Msg }) {
  if (!m.files.length) return null
  return (
    <div className="mt-1.5 flex flex-wrap gap-1.5">
      {m.files.map((f) => <span key={f} className="inline-flex h-6 items-center gap-1 rounded-md border bg-background px-2 text-xs"><Paperclip className="size-3" />{f}</span>)}
    </div>
  )
}

export function Reactions({ m, round }: { m: Msg; round?: boolean }) {
  if (!m.reactions.length) return null
  return (
    <div className="mt-1.5 flex flex-wrap gap-1">
      {m.reactions.map((r) => (
        <span key={r.emoji} className={cn("inline-flex h-6 items-center gap-1 border bg-muted px-1.5 text-xs", round ? "rounded-full" : "rounded-md")}>{r.emoji}<span className="tabular text-muted-foreground">{r.by.length}</span></span>
      ))}
    </div>
  )
}

/** A card with the buttons the agent offered; the pressed one is marked once the card is answered. */
export function Card({ card, skin, className, button }: {
  card: NeutralCard; skin: Skin; className?: string; button?: (b: CardButton, pressed: boolean) => string
}) {
  const answered = card.state !== "open"
  return (
    <div className={cn("mt-1.5 max-w-md space-y-2 p-3 text-sm", className)} data-card={card.id}>
      {card.headline && <div className="font-semibold">{card.headline}</div>}
      {card.body && <p className="whitespace-pre-wrap break-words text-muted-foreground">{card.body}</p>}
      {!!card.facts?.length && (
        <dl className="grid grid-cols-[auto_1fr] gap-x-3 gap-y-0.5 text-xs">
          {card.facts.map((f) => <div key={f.label} className="contents"><dt className="text-muted-foreground">{f.label}</dt><dd className="break-words">{f.value}</dd></div>)}
        </dl>
      )}
      {!!card.fields?.length && (
        <div className="space-y-1.5">
          {card.fields.map((f) => (
            <div key={f.id}>
              <div className="text-xs text-muted-foreground">{f.label}</div>
              <div className="flex h-8 items-center rounded-md border bg-background px-2 text-xs text-faint">{card.answer?.fields?.[f.id] ?? (f.kind === "secret" ? "••••••" : f.options?.[0] ?? "")}</div>
            </div>
          ))}
        </div>
      )}
      {!!card.buttons?.length && (
        <div className="flex flex-wrap gap-1.5">
          {card.buttons.map((b) => {
            const pressed = card.answer?.button === b.id
            return (
              <span key={b.id} className={cn(
                "inline-flex h-8 items-center rounded-md border px-2.5 text-xs font-medium",
                b.style === "primary" && "border-transparent bg-primary text-primary-foreground", b.style === "danger" && "text-destructive", b.style === "link" && "border-transparent text-link",
                answered && !pressed && "opacity-50", pressed && "ring-2 ring-ring",
              )}>{button?.(b, pressed) ?? b.label}</span>
            )
          })}
        </div>
      )}
      {(card.footer || answered) && (
        <div className="text-xs text-faint">
          {answered && (card.answer?.button ? `${ANSWER[card.buttons?.find((b) => b.id === card.answer?.button)?.action ?? "custom"] ?? "Answered"}${card.answer.by ? ` by ${nameOf(skin, card.answer.by)}` : ""}` : card.state === "answered" ? "Answered" : card.state === "expired" ? "Expired" : "Cancelled")}
          {answered && card.footer ? " · " : ""}{card.footer}
        </div>
      )}
    </div>
  )
}

/** The small button that anchors a comment to a message or a card. */
export function PinButton({ pin, id, card, quote }: { pin?: Pin; id: string; card?: string; quote?: string }) {
  if (!pin) return null
  return (
    <Button
      variant="ghost" size="icon-xs" aria-label="Comment" className="opacity-0 group-hover/msg:opacity-100 focus-visible:opacity-100"
      onClick={() => pin(card ? { target: "card_element", ref: id, element: card, quote } : { target: "message", ref: id, quote })}
    ><MessageSquarePlus /></Button>
  )
}
