import { useState } from "react"

import { PanelSection, SettingRow } from "@/components/panel"
import { Button } from "@/components/ui/button"
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select"
import { useConfirm } from "@/lib/confirm"
import { modelLabel } from "@/lib/format"
import { useSave, type Outcome } from "./api"
import { Notice } from "./shared"
import type { ChannelDetail, ChannelSettings } from "./types"

export function Model({ c, s }: { c: ChannelDetail; s: ChannelSettings }) {
  const save = useSave(c.id)
  const confirm = useConfirm()
  const [out, setOut] = useState<Outcome | null>(null)
  const pin = s.provider_pin
  const choices = c.can_manage ? s.pin_choices : []
  if (!pin && !choices.length) return null
  const text = pin ? modelLabel(pin.model ?? pin.backend, pin.model_label) : "Default"

  const clear = async () => {
    if (!pin || !(await confirm({ title: "Clear the model?", body: `${text} stops being used here.`, action: "Clear", destructive: true }))) return
    setOut(await save.run("post", "provider-pin/clear", { expected_local: pin.local }))
  }
  const set = async (value: string) => {
    const next = choices.find((x) => x.value === value)
    if (!next || !(await confirm({ title: `Use ${next.label} here?`, body: `Threads in this channel switch to ${next.label} on their next message.`, action: "Use" }))) return
    setOut(await save.run("post", "provider-pin", { value, expected_local: pin?.local ?? null }))
  }
  return (
    <PanelSection title="Model">
      <SettingRow label="Model" overridden={!!pin}>
        <span className="flex min-w-0 items-center gap-2">
          {choices.length ? (
            <Select value="" onValueChange={(v) => void set(v)} disabled={save.pending}>
              <SelectTrigger className="h-8 min-w-40"><SelectValue placeholder={text} /></SelectTrigger>
              <SelectContent>
                {choices.map((x) => (
                  <SelectItem key={x.value} value={x.value}>
                    <span className="flex w-full items-center gap-2">{x.label}{x.price && <span className="ml-auto pl-4 text-xs text-muted-foreground tabular">{x.price}</span>}</span>
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          ) : (
            <span className="truncate">{text}</span>
          )}
          {pin && c.can_manage && <Button size="xs" variant="outline" disabled={save.pending} onClick={() => void clear()}>Clear</Button>}
        </span>
      </SettingRow>
      <Notice out={out} />
    </PanelSection>
  )
}
