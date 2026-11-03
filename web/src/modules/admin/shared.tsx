import { Label } from "@/components/ui/label"

export { Facts, PanelSection, Tag } from "@/components/panel"
export function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return <div className="space-y-1.5"><Label>{label}</Label>{children}</div>
}

export function Row({ children, label }: { children: React.ReactNode; label: string }) {
  return <div className="flex min-h-11 items-center gap-2.5 px-4 py-1.5 text-sm"><span className="flex-1">{label}</span>{children}</div>
}

export function Block({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <section>
      <h2 className="mb-2 text-sm font-semibold">{title}</h2>
      <div className="divide-y rounded-lg border">{children}</div>
    </section>
  )
}
