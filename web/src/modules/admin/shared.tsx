import { Label } from "@/components/ui/label"
import type { Role } from "@/lib/types"

export const ROLE: Record<Role, string> = { owner: "Owner", admin: "Admin", member: "Member", visitor: "Visitor" }
export const Tag = ({ children }: { children: React.ReactNode }) => <span className="rounded border px-1.5 text-[11px] text-muted-foreground">{children}</span>
export function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return <div className="space-y-1.5"><Label>{label}</Label>{children}</div>
}

/** A titled block of a detail panel. */
export function PanelSection({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <section className="border-t px-5 py-4">
      <h3 className="mb-2 text-xs font-medium text-muted-foreground">{title}</h3>
      {children}
    </section>
  )
}

/** Label and value rows of a detail panel; rows without a value are left out. */
export function Facts({ rows }: { rows: [string, React.ReactNode][] }) {
  return (
    <dl className="space-y-1.5 text-sm">
      {rows.filter(([, v]) => v != null && v !== "").map(([k, v]) => (
        <div key={k} className="grid grid-cols-[112px_1fr] gap-3"><dt className="text-muted-foreground">{k}</dt><dd className="min-w-0 break-words">{v}</dd></div>
      ))}
    </dl>
  )
}
