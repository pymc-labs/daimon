import { Check } from "lucide-react"
import { useState } from "react"

import { CopyButton, Mono } from "@/components/common"
import { Button } from "@/components/ui/button"
import { useRotate } from "./api"
import { Shell } from "./run/RunDialogs"
import { ChoiceCards } from "./tabparts"

const OVERLAP = [
  { value: "none", label: "None", fact: "Old secret stops now" }, { value: "1h", label: "1 hour", fact: "Old secret still works" },
  { value: "24h", label: "24 hours", fact: "Default" }, { value: "7d", label: "7 days", fact: "Old secret still works" },
]

/** Rotate the signing secret: pick how long the old one keeps working, then the new one is shown once. */
export function RotateDialog({ id, trigger, open, onOpenChange }: { id: string; trigger: string; open: boolean; onOpenChange: (o: boolean) => void }) {
  const [overlap, setOverlap] = useState("24h")
  const rotate = useRotate(id, trigger)
  const secret = rotate.data?.secret
  const close = (o: boolean) => { onOpenChange(o); if (!o) rotate.reset() }
  return (
    <Shell title="Rotate secret" open={open} onOpenChange={close} error={rotate.error?.message}
      footer={secret ? <Button onClick={() => close(false)}><Check />Done</Button> : <>
        <Button variant="ghost" onClick={() => close(false)}>Cancel</Button>
        <Button disabled={rotate.isPending} onClick={() => rotate.mutate(overlap)}>Rotate secret</Button>
      </>}>
      {secret ? (
        <div className="space-y-1.5">
          <span className="text-xs font-medium text-muted-foreground">New secret, shown once</span>
          <div className="flex min-h-9 items-center gap-1 rounded-md border bg-muted/40 py-1 pl-3 pr-1"><Mono className="min-w-0 flex-1 break-all text-xs">{secret}</Mono><CopyButton text={secret} label="Copy secret" /></div>
        </div>
      ) : (
        <div className="space-y-1.5">
          <span className="text-xs font-medium text-muted-foreground">Old secret keeps working for</span>
          <ChoiceCards label="Overlap" value={overlap} onChange={setOverlap} options={OVERLAP} />
        </div>
      )}
    </Shell>
  )
}
