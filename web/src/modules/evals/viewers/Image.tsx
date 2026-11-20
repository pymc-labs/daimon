import pixelmatch from "pixelmatch"
import { useEffect, useRef, useState } from "react"
import { ReactCompareSlider, ReactCompareSliderImage } from "react-compare-slider"

import { Button } from "@/components/ui/button"
import { cn } from "@/lib/utils"
import { blobUrl } from "../api"
import type { BlobRef, Pin } from "../types"

type Box = { x: number; y: number; w: number; h: number }

const load = (src: string) => new Promise<HTMLImageElement>((ok, no) => { const i = new Image(); i.onload = () => ok(i); i.onerror = no; i.src = src })

/** Pixels that differ between two images, drawn into the canvas; the second is fitted to the first. */
async function paint(canvas: HTMLCanvasElement, a: string, b: string) {
  const [x, y] = await Promise.all([load(a), load(b)])
  const w = x.naturalWidth || 800, h = x.naturalHeight || 600
  const data = (img: HTMLImageElement) => {
    const c = document.createElement("canvas"); c.width = w; c.height = h
    const g = c.getContext("2d")!; g.drawImage(img, 0, 0, w, h)
    return g.getImageData(0, 0, w, h)
  }
  canvas.width = w; canvas.height = h
  const out = canvas.getContext("2d")!.createImageData(w, h)
  const n = pixelmatch(data(x).data, data(y).data, out.data, w, h, { threshold: 0.1 })
  canvas.getContext("2d")!.putImageData(out, 0, 0)
  return n / (w * h)
}

/** An image with region anchors: drag a box, it is kept as a 0 to 1 box so it survives resizing. */
function Regions({ blob, pin, name }: { blob: BlobRef; pin?: Pin; name: string }) {
  const [boxes, setBoxes] = useState<Box[]>([])
  const [drag, setDrag] = useState<{ x: number; y: number; box: Box } | null>(null)
  const at = (e: React.PointerEvent) => { const r = e.currentTarget.getBoundingClientRect(); return [(e.clientX - r.left) / r.width, (e.clientY - r.top) / r.height] as const }
  const box = (x: number, y: number, e: React.PointerEvent): Box => { const [px, py] = at(e); return { x: Math.min(x, px), y: Math.min(y, py), w: Math.abs(px - x), h: Math.abs(py - y) } }
  return (
    <div className="relative select-none overflow-hidden rounded-md border" onPointerDown={pin ? (e) => { const [x, y] = at(e); e.currentTarget.setPointerCapture(e.pointerId); setDrag({ x, y, box: { x, y, w: 0, h: 0 } }) } : undefined}
      onPointerMove={(e) => drag && setDrag({ ...drag, box: box(drag.x, drag.y, e) })}
      onPointerUp={() => { if (drag && drag.box.w > 0.01 && drag.box.h > 0.01) { setBoxes((b) => [...b, drag.box]); pin?.({ target: "image_region", ref: name, region: drag.box }) } setDrag(null) }}>
      <img src={blobUrl(blob.sha256)} alt={name} draggable={false} className={cn("block w-full", pin && "cursor-crosshair")} />
      {[...boxes, ...(drag ? [drag.box] : [])].map((b, i) => (
        <span key={i} className="absolute border-2 border-link bg-link/15" style={{ left: `${b.x * 100}%`, top: `${b.y * 100}%`, width: `${b.w * 100}%`, height: `${b.h * 100}%` }} />
      ))}
    </div>
  )
}

/** One image, or two to compare: a slider between them or the pixels that differ. */
export function ImageView({ a, b, pin }: { a: BlobRef; b?: BlobRef; pin?: Pin }) {
  const [mode, setMode] = useState<"slider" | "diff">("slider")
  const canvas = useRef<HTMLCanvasElement>(null)
  const [share, setShare] = useState<number | null>(null)
  useEffect(() => {
    if (mode !== "diff" || !b || !canvas.current) return
    let live = true
    paint(canvas.current, blobUrl(a.sha256), blobUrl(b.sha256)).then((s) => live && setShare(s), () => live && setShare(null))
    return () => { live = false }
  }, [mode, a.sha256, b])
  if (!b) return <Regions blob={a} pin={pin} name={a.name ?? "image"} />
  return (
    <div className="space-y-2">
      <div className="flex items-center gap-1">
        <Button size="xs" variant={mode === "slider" ? "secondary" : "ghost"} onClick={() => setMode("slider")}>Slider</Button>
        <Button size="xs" variant={mode === "diff" ? "secondary" : "ghost"} onClick={() => setMode("diff")}>Pixel diff</Button>
        {mode === "diff" && share !== null && <span className="ml-auto text-xs text-muted-foreground tabular">{(share * 100).toFixed(1)}% differ</span>}
      </div>
      {mode === "slider"
        ? <div className="overflow-hidden rounded-md border"><ReactCompareSlider itemOne={<ReactCompareSliderImage src={blobUrl(a.sha256)} alt="First" />} itemTwo={<ReactCompareSliderImage src={blobUrl(b.sha256)} alt="Second" />} /></div>
        : <canvas ref={canvas} className="w-full rounded-md border bg-muted" />}
    </div>
  )
}
