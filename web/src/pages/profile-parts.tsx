export function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <section aria-label={title}>
      <h2 className="mb-3 text-sm font-semibold">{title}</h2>
      <div className="divide-y rounded-lg border">{children}</div>
    </section>
  )
}

export const Empty = ({ children }: { children: React.ReactNode }) => <div className="px-4 py-6 text-center text-sm text-muted-foreground">{children}</div>
export const Row = ({ children }: { children: React.ReactNode }) => <div className="flex min-h-14 items-center gap-3 px-4 py-3 text-sm">{children}</div>
