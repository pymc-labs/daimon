import { useState } from "react"

import { Button } from "@/components/ui/button"
import { useConfirm } from "@/lib/confirm"
import { PanelSection } from "@/modules/admin/shared"
import { useSave, type Outcome } from "./api"
import { Line, Notice } from "./shared"
import type { ChannelDetail, ChannelSettings } from "./types"

export function Model({ c, s }: { c: ChannelDetail; s: ChannelSettings }) {
  const save = useSave(c.id)
  const confirm = useConfirm()
  const [out, setOut] = useState<Outcome | null>(null)
  const pin = s.provider_pin
  if (!pin) return null
  const text = [pin.backend, pin.model].filter(Boolean).join(", ")

  const clear = async () => {
    if (!(await confirm({ title: "Clear the fixed model?", body: text, action: "Clear", destructive: true }))) return
    setOut(await save.run("post", "provider-pin/clear", { expected_local: pin.local }))
  }
  return (
    <PanelSection title="Model">
      <div className="text-sm">
        <Line label="Fixed model">
          <span className="flex min-w-0 items-center gap-2">
            <span className="truncate">{text}</span>
            {c.can_manage && <Button size="xs" variant="outline" disabled={save.pending} onClick={() => void clear()}>Clear</Button>}
          </span>
        </Line>
      </div>
      <Notice out={out} />
    </PanelSection>
  )
}
