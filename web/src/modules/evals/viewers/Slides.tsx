import { useState } from "react"

import { Button } from "@/components/ui/button"
import { cn } from "@/lib/utils"
import { blobUrl } from "../api"
import type { Output, Pin } from "../types"

/** A deck as its rendered slides: a thumbnail strip, one slide large, two decks side by side per slide. */
export function Slides({ o, other, pin }: { o: Output; other?: Output; pin?: Pin }) {
  const [at, setAt] = useState(0)
  const slides = o.renders ?? []
  const shown = (d: Output) => d.renders?.[Math.min(at, (d.renders?.length ?? 1) - 1)]
  if (!slides.length) return <div className="py-6 text-center text-sm text-muted-foreground">No rendered slides</div>
  return (
    <div className="space-y-2">
      <div className="flex gap-1.5 overflow-x-auto pb-1">
        {slides.map((s, i) => (
          <button key={s.sha256} aria-label={`Slide ${i + 1}`} onClick={() => setAt(i)} className={cn("w-24 shrink-0 overflow-hidden rounded-md border", i === at && "ring-2 ring-ring")}>
            <img src={blobUrl(s.sha256)} alt="" className="block w-full" />
          </button>
        ))}
      </div>
      <div className={cn("grid gap-2", other && "grid-cols-2")}>
        {[o, ...(other ? [other] : [])].map((d) => { const r = shown(d); return r && <img key={d.id} src={blobUrl(r.sha256)} alt={`Slide ${at + 1}`} className="w-full rounded-md border" /> })}
      </div>
      <div className="flex items-center gap-2 text-xs text-muted-foreground tabular">
        Slide {at + 1} of {slides.length}
        {pin && <Button size="xs" variant="outline" className="ml-auto" onClick={() => pin({ target: "page", ref: o.id, page: at + 1 })}>Comment on slide {at + 1}</Button>}
      </div>
    </div>
  )
}
