import type { CreditMeter as Meter } from "@/lib/types"

export const creditTone = (m: Meter) => {
  const pct = m.total_usd ? m.left_usd / m.total_usd : 0
  return pct <= 0 ? "out" : pct < 0.2 ? "low" : "ok"
}
