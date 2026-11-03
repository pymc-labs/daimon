import { useParam } from "@/lib/filters"
import type { Period } from "./types"

/** The period in the address: the last 30 days unless it names a month. */
export function usePeriod() {
  const [value, set] = useParam("period")
  return [(value || "30d") as Period, (p: Period) => set(p === "30d" ? null : p)] as const
}
