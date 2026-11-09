import { QueryState } from "@/components/QueryState"
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table"
import { useFeatureSchema, useFeatures } from "./api"
import type { FeatureSchema, Tier } from "./types"

const COLUMNS: { tier: Tier; label: string }[] = [
  { tier: "deployment", label: "Built-in" }, { tier: "workspace", label: "Workspace" }, { tier: "agent", label: "Agent" },
]

/** Every feature against the levels an agent inherits from: what each sets and which one wins. */
export function FeatureMatrix({ agent }: { agent: string }) {
  const schema = useFeatureSchema()
  const doc = useFeatures({ agent })
  const label = (s: FeatureSchema, value: string) => s.values.find((v) => v.value === value)?.label ?? value
  return (
    <QueryState query={schema.data ? doc : schema} className="px-3 py-4">{() => (
      <Table>
        <TableHeader>
          <TableRow>
            <TableHead>Feature</TableHead>
            {COLUMNS.map((c) => <TableHead key={c.tier}>{c.label}</TableHead>)}
            <TableHead>In effect</TableHead>
          </TableRow>
        </TableHeader>
        <TableBody>
          {(schema.data ?? []).map((s) => {
            const v = doc.data?.features.find((f) => f.key === s.key)
            if (!v || !s.tiers.some((t) => t === "agent")) return null
            return (
              <TableRow key={s.key}>
                <TableCell>{s.name}</TableCell>
                {COLUMNS.map(({ tier }) => {
                  const own = v.tiers.find((t) => t.tier === tier)
                  const set = own && own.value !== "default"
                  return (
                    <TableCell key={tier} className={set && v.source === tier ? "font-medium" : "text-muted-foreground"}>
                      {set ? label(s, own.value) : s.tiers.includes(tier) ? "-" : ""}
                    </TableCell>
                  )
                })}
                <TableCell>{v.unavailable ? v.summary : label(s, v.value)}</TableCell>
              </TableRow>
            )
          })}
        </TableBody>
      </Table>
    )}</QueryState>
  )
}
