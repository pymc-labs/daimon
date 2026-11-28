// Cmd/Ctrl + wheel zoom with a speed cap. Smooth-scrolling tools (Mos, mouse drivers) turn one wheel notch
// into a burst of pixel events that the stock handler sums into a 3x jump; a plain notch stays as it was.
import { type ReactFlowInstance } from "@xyflow/react"
import { type RefObject, useEffect } from "react"

type Wheel = Pick<WheelEvent, "deltaY" | "deltaMode" | "ctrlKey" | "metaKey" | "timeStamp">

const RATE = 1 // most the wheel zooms a second, in doublings
const BURST = 0.2 // most one notch zooms at once, in doublings: x1.15, a Windows notch
const PINCH = 0.5

/** Doublings of zoom per wheel event. A trackpad pinch (ctrl, fractional on Windows) follows the fingers uncapped. */
export function wheelZoom(mac: boolean) {
  let budget = BURST, last = -Infinity
  return (e: Wheel) => {
    const pinch = e.ctrlKey && !e.metaKey && (mac || !Number.isInteger(e.deltaY))
    const d = -e.deltaY * (e.deltaMode === 1 ? 0.05 : e.deltaMode ? 1 : 0.002) * (pinch ? 10 : 1)
    if (pinch) return Math.max(-PINCH, Math.min(PINCH, d))
    budget = Math.min(BURST, budget + ((e.timeStamp - last) / 1000) * RATE)
    last = e.timeStamp
    const step = Math.sign(d) * Math.min(Math.abs(d), budget)
    budget -= Math.abs(step)
    return step
  }
}

/** Takes Cmd/Ctrl + wheel over `ref` from React Flow and zooms round the pointer, capped by `wheelZoom`. */
export function useWheelZoom(ref: RefObject<HTMLElement | null>, rf: ReactFlowInstance, min: number, max: number) {
  useEffect(() => {
    const el = ref.current
    if (!el) return
    const zoomBy = wheelZoom(/Mac/.test(navigator.userAgent))
    const onWheel = (e: WheelEvent) => {
      if (!e.ctrlKey && !e.metaKey) return
      e.preventDefault()
      e.stopPropagation()
      const d = zoomBy(e)
      const { x, y, zoom } = rf.getViewport()
      const k = Math.min(max, Math.max(min, zoom * 2 ** d))
      if (k === zoom) return
      const r = el.getBoundingClientRect(), px = e.clientX - r.left, py = e.clientY - r.top
      void rf.setViewport({ x: px - ((px - x) * k) / zoom, y: py - ((py - y) * k) / zoom, zoom: k })
    }
    el.addEventListener("wheel", onWheel, { capture: true, passive: false })
    return () => el.removeEventListener("wheel", onWheel, { capture: true })
  }, [ref, rf, min, max])
}
