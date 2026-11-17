import { ChevronRight, Send } from "lucide-react"
import { useEffect, useState } from "react"
import { toast } from "sonner"

import { Drawer } from "@/components/Drawer"
import { PanelHead, PanelSection, SettingRow } from "@/components/panel"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { useConfirm } from "@/lib/confirm"
import { ago, plural } from "@/lib/format"
import { useParam } from "@/lib/filters"
import { useSupportSave } from "./api"
import { Actions, Failure } from "./Limits"
import { chargeRange, credits } from "./lib"
import type { SupportPage, Undelivered } from "./types"

/** The Not delivered count, as a button that opens the list. */
export function UndeliveredCount({ n }: { n: number }) {
  const [, open] = useParam("undelivered")
  return <Button variant="ghost" size="xs" className="text-destructive" onClick={() => open("all")}>{n}<ChevronRight /></Button>
}

/** Requests no support channel received: settle one with the credits to charge, or decline it. */
export function UndeliveredDrawer({ d }: { d: SupportPage }) {
  return <Drawer param="undelivered" title="Not delivered">{() => <Requests list={d.undelivered} />}</Drawer>
}

function Requests({ list }: { list: Undelivered[] }) {
  const [, setOpen] = useParam("undelivered")
  // A reload can empty the list (another admin settled them): nothing is left to show.
  useEffect(() => { if (!list.length) setOpen(null) }, [list.length, setOpen])
  return (
    <div className="flex min-h-full flex-col">
      <PanelHead icon={<Send className="size-4 text-muted-foreground" />} title="Not delivered" subtitle={plural(list.length, "request")} />
      <PanelSection><SettingRow label="Person told">No</SettingRow></PanelSection>
      {list.map((r) => <Request key={r.id} r={r} />)}
    </div>
  )
}

/** The charge as typed, or why it cannot be sent; the server has the last word. */
function chargeError(text: string, r: Undelivered): string | null {
  const range = chargeRange(r.price)
  if (!/^\d+(\.\d{0,2})?$/.test(text.trim())) return null
  const n = Number(text)
  if (n < range.min || (range.max !== null && n > range.max)) return `Credits ${range.max === null ? `from ${credits(range.min)}` : range.min === range.max ? `are ${credits(range.min)}` : `${credits(range.min)} to ${credits(range.max)}`}`
  return null
}

function Request({ r }: { r: Undelivered }) {
  const save = useSupportSave()
  const confirm = useConfirm()
  const range = chargeRange(r.price)
  const fixed = range.max === range.min
  const [charge, setCharge] = useState(r.price.min)
  const [error, setError] = useState<{ at: string | null; rest: string | null }>({ at: null, rest: null })
  const bound = chargeError(charge, r)
  const act = async (what: "settle" | "decline") => {
    const out = await save.run(`requests/${encodeURIComponent(r.id)}/${what}`, what === "settle" ? { charge: charge.trim() } : {}, true)
    if (out.error) { setError({ at: out.problems.find((p) => p.loc[0] === "charge")?.msg ?? null, rest: out.problems.some((p) => p.loc[0] === "charge") ? null : out.error }); return }
    toast(what === "settle" ? "Request settled" : "Request declined")
  }
  const decline = async () => {
    if (await confirm({ title: "Decline request?", body: `${r.person}: ${r.what}`, action: "Decline", destructive: true })) await act("decline")
  }
  return (
    <PanelSection>
      <div className="flex items-baseline justify-between gap-3 text-sm">
        <span className="min-w-0 truncate font-medium">{r.person}</span>
        <span className="shrink-0 text-xs text-muted-foreground">{ago(r.at)}</span>
      </div>
      <p className="mt-0.5 break-words text-sm text-muted-foreground">{r.what}</p>
      <div className="mt-3 space-y-3">
        <SettingRow label="Held"><span className="tabular">{credits(Number(r.credits))}</span></SettingRow>
        <SettingRow label="Charge">
          <div className="space-y-1">
            <Input aria-label="Charge" aria-invalid={!!(error.at ?? bound)} inputMode="decimal" className="h-8 w-28" disabled={fixed} value={charge} onChange={(e) => { setCharge(e.target.value.replace(/[^\d.]/g, "")); setError({ at: null, rest: null }) }} />
            {(error.at ?? bound) && <p className="text-xs text-destructive">{error.at ?? bound}</p>}
          </div>
        </SettingRow>
      </div>
      <div className="mt-4">
        <Actions>
          <Button variant="outline" disabled={save.pending} onClick={() => void decline()}>Decline</Button>
          <Button disabled={save.pending || !charge.trim() || !!bound} onClick={() => void act("settle")}>Settle</Button>
        </Actions>
        <Failure>{error.rest}</Failure>
      </div>
    </PanelSection>
  )
}
