import { useParam } from "@/lib/filters"
import { dayMonth } from "@/lib/format"
import type { OpsArea, OpsPeriod } from "./types"

/** The ops pages refetch this often while they are visible. */
export const REFRESH_MS = 60_000

export const AREAS: Record<OpsArea, string> = {
  turn: "Answers", routine: "Routines", bridge: "Bridged tools", dm: "Direct messages", watch: "Watches", delivery: "Delivery", wake: "Follow-ups",
}

/** The period in the address; 24 hours unless it names another. */
export function usePeriod() {
  const [value, set] = useParam("period")
  return [(value || "24h") as OpsPeriod, (p: OpsPeriod) => set(p === "24h" ? null : p)] as const
}

const hour = (d: Date) => `${String(d.getHours()).padStart(2, "0")}:00`
/** A bucket's label: its hour for 24h, its day otherwise. */
export const bucketLabel = (at: Date, period: OpsPeriod) => (period === "24h" ? hour(at) : dayMonth(at))
