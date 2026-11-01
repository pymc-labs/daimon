import { Check, CircleAlert, KeyRound, ShieldCheck, ShieldQuestion, ShieldX, UserRound, type LucideIcon } from "lucide-react"
import { useState } from "react"

import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { cn } from "@/lib/utils"
import type { Card, CardAnswer } from "./types"

type Props = { card: Card; busy: boolean; onAnswer: (a: CardAnswer) => void }

function Icon({ icon: I, className }: { icon: LucideIcon; className?: string }) {
  return (
    <span className={cn("grid size-8 shrink-0 place-items-center rounded-lg bg-muted text-muted-foreground", className)}>
      <I className="size-4" />
    </span>
  )
}

/** An answered card shrinks to one line. */
function Settled({ icon, label, children, tone }: { icon: LucideIcon; label: string; children: React.ReactNode; tone?: "ok" | "no" }) {
  const I = icon
  return (
    <div className="flex min-h-10 items-center gap-2.5 rounded-xl border px-3 py-2 text-sm">
      <I className={cn("size-4 shrink-0", tone === "no" ? "text-muted-foreground" : "text-success")} />
      <span className="font-medium">{label}</span>
      <span className="min-w-0 truncate text-muted-foreground">{children}</span>
    </div>
  )
}

const frame = "rounded-xl border bg-background p-4"

function Approval({ card, busy, onAnswer }: Props & { card: Extract<Card, { kind: "approval" }> }) {
  if (card.state !== "pending") {
    const [label, icon] = card.state === "denied" ? ["Denied", ShieldX] as const : card.state === "always" ? ["Always allowed", ShieldCheck] as const : ["Approved once", ShieldCheck] as const
    return <Settled icon={icon} label={label} tone={card.state === "denied" ? "no" : "ok"}>{card.title.replace(/\?$/, "")}</Settled>
  }
  return (
    <div className={frame}>
      <div className="flex gap-3">
        <Icon icon={ShieldQuestion} />
        <div className="min-w-0 flex-1">
          <div className="text-sm font-medium">{card.title}</div>
          {card.consequence && <p className="mt-0.5 text-sm text-muted-foreground">{card.consequence}</p>}
          <dl className="mt-3 space-y-1.5 rounded-lg bg-muted px-3 py-2.5 text-xs">
            {card.details.map((d) => {
              const i = d.indexOf(": ")
              return (
                <div key={d} className="flex gap-3">
                  <dt className="w-14 shrink-0 leading-5 text-muted-foreground">{d.slice(0, i)}</dt>
                  <dd className="min-w-0 leading-5">{d.slice(i + 2)}</dd>
                </div>
              )
            })}
          </dl>
          <div className="mt-3 flex flex-wrap gap-2">
            <Button size="sm" disabled={busy} onClick={() => onAnswer({ answer: "approve" })}>{card.always_allowed ? "Approve once" : "Approve"}</Button>
            {card.always_allowed && <Button size="sm" variant="outline" disabled={busy} onClick={() => onAnswer({ answer: "always" })}>Always allow</Button>}
            <Button size="sm" variant="ghost" disabled={busy} onClick={() => onAnswer({ answer: "deny" })}>Deny</Button>
          </div>
        </div>
      </div>
    </div>
  )
}

function Credential({ card, busy, onAnswer }: Props & { card: Extract<Card, { kind: "credential" }> }) {
  const [value, setValue] = useState("")
  if (card.state !== "pending") {
    return card.state === "saved"
      ? <Settled icon={Check} label="Saved" tone="ok"><span className="font-mono text-[12.5px]">{card.key}</span></Settled>
      : <Settled icon={Check} label="Skipped" tone="no"><span className="font-mono text-[12.5px]">{card.key}</span></Settled>
  }
  return (
    <form className={frame} onSubmit={(e) => { e.preventDefault(); if (value.trim()) onAnswer({ answer: "save", value: value.trim() }) }}>
      <div className="flex gap-3">
        <Icon icon={KeyRound} />
        <div className="min-w-0 flex-1">
          <div className="text-sm font-medium">{card.headline}</div>
          <p className="mt-0.5 text-sm text-muted-foreground">{card.fact}</p>
          <label className="mt-3 block font-mono text-xs text-muted-foreground" htmlFor={card.id}>{card.key}</label>
          <Input id={card.id} type="password" autoComplete="off" value={value} onChange={(e) => setValue(e.target.value)} className="mt-1 h-8 font-mono" placeholder="Paste the token" />
          <div className="mt-3 flex gap-2">
            <Button size="sm" type="submit" disabled={busy || !value.trim()}>Save</Button>
            <Button size="sm" type="button" variant="ghost" disabled={busy} onClick={() => onAnswer({ answer: "skip" })}>Skip</Button>
          </div>
        </div>
      </div>
    </form>
  )
}

/** A reply that did not start: it says what happened and who can help. */
function Refusal({ card }: { card: Extract<Card, { kind: "refusal" }> }) {
  return (
    <div className="rounded-xl border border-destructive/30 bg-destructive/5 p-4">
      <div className="flex gap-3">
        <Icon icon={CircleAlert} className="bg-destructive/10 text-destructive" />
        <div className="min-w-0 flex-1">
          <div className="text-sm font-medium">{card.title}</div>
          <p className="mt-0.5 text-sm text-muted-foreground">{card.body}</p>
          {card.help && <p className="mt-2.5 flex items-center gap-1.5 text-sm font-medium"><UserRound className="size-4 text-muted-foreground" />{card.help}</p>}
        </div>
      </div>
    </div>
  )
}

export function CardView(p: Props) {
  const c = p.card
  return c.kind === "approval" ? <Approval {...p} card={c} />
    : c.kind === "credential" ? <Credential {...p} card={c} />
      : <Refusal card={c} />
}
export { Refusal }
