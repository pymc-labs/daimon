import { Label } from "@/components/ui/label"
import type { Role } from "@/lib/types"

export const ROLE: Record<Role, string> = { owner: "Owner", admin: "Admin", member: "Member", visitor: "Visitor" }
export const Tag = ({ children }: { children: React.ReactNode }) => <span className="rounded border px-1.5 text-[11px] text-muted-foreground">{children}</span>
export function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return <div className="space-y-1.5"><Label>{label}</Label>{children}</div>
}
