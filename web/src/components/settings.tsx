import { cn } from "@/lib/utils"

export type SettingsRow = { key?: string; label: React.ReactNode; control: React.ReactNode }

/** A bordered card of settings: label on the left, control on the right, `h-11` rows. */
export function SettingsList({ title, rows, className }: { title?: string; rows: SettingsRow[]; className?: string }) {
  return (
    <section className={cn("mx-auto w-full max-w-3xl", className)}>
      {title && <h2 className="mb-2 text-sm font-semibold">{title}</h2>}
      <div className="divide-y rounded-lg border">
        {rows.map((r, i) => (
          <div key={r.key ?? String(i)} className="flex min-h-11 items-center gap-3 px-4 py-1.5 text-sm max-sm:flex-wrap">
            <span className="min-w-0 flex-1 max-sm:basis-full">{r.label}</span>
            <div className="flex min-w-0 items-center justify-end gap-2 max-sm:w-full max-sm:justify-start">{r.control}</div>
          </div>
        ))}
      </div>
    </section>
  )
}
