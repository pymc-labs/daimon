import { MoreHorizontal } from "lucide-react"

import { Button } from "@/components/ui/button"
import { DropdownMenu, DropdownMenuContent, DropdownMenuItem, DropdownMenuSeparator, DropdownMenuTrigger } from "@/components/ui/dropdown-menu"
import { useParam } from "@/lib/filters"
import { cn } from "@/lib/utils"

export const Tag = ({ children }: { children: React.ReactNode }) => <span className="rounded border px-1.5 text-[11px] text-muted-foreground">{children}</span>

export type MenuAction = { label: string; onSelect: () => void; destructive?: boolean; icon?: React.ReactNode; disabled?: boolean }

/** The `...` menu of a panel; destructive items sit last, after a divider. */
export function PanelMenu({ items, label = "More" }: { items: MenuAction[]; label?: string }) {
  if (!items.length) return null
  const safe = items.filter((i) => !i.destructive), risky = items.filter((i) => i.destructive)
  const row = (i: MenuAction) => (
    <DropdownMenuItem key={i.label} variant={i.destructive ? "destructive" : undefined} disabled={i.disabled} onSelect={i.onSelect}>{i.icon}{i.label}</DropdownMenuItem>
  )
  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild><Button variant="ghost" size="icon-sm" aria-label={label}><MoreHorizontal /></Button></DropdownMenuTrigger>
      <DropdownMenuContent align="end">
        {safe.map(row)}
        {safe.length > 0 && risky.length > 0 && <DropdownMenuSeparator />}
        {risky.map(row)}
      </DropdownMenuContent>
    </DropdownMenu>
  )
}

/** Top of a drawer: icon or avatar, title, one subtitle line, tags; actions on the right (two buttons at most, the rest in `menu`). */
export function PanelHead({ icon, avatar, title, subtitle, tags, actions, menu }: {
  /** A glyph shown in a 40px circle; `avatar` is shown as given, sized 40px by the caller. */
  icon?: React.ReactNode; avatar?: React.ReactNode; title: React.ReactNode; subtitle?: React.ReactNode; tags?: React.ReactNode
  actions?: React.ReactNode; menu?: MenuAction[]
}) {
  return (
    <div className="flex items-center gap-3 px-5 py-5">
      {avatar ?? (icon && <span className="flex size-10 shrink-0 items-center justify-center rounded-full bg-accent">{icon}</span>)}
      <div className="min-w-0 flex-1">
        <div className="truncate text-base font-semibold">{title}</div>
        {(subtitle || tags) && (
          <div className="flex min-w-0 flex-wrap items-center gap-x-2 gap-y-1 text-sm text-muted-foreground">
            {subtitle && <span className="min-w-0 truncate">{subtitle}</span>}
            {tags}
          </div>
        )}
      </div>
      {(actions || menu?.length) && (
        <div className="flex shrink-0 items-center gap-1.5">
          {actions}
          {menu && <PanelMenu items={menu} />}
        </div>
      )}
    </div>
  )
}

/** A titled block of a detail panel. */
export function PanelSection({ title, children, className }: { title?: string; children: React.ReactNode; className?: string }) {
  return (
    <section className={cn("border-t px-5 py-4", className)}>
      {title && <h3 className="mb-2 text-xs font-medium text-muted-foreground">{title}</h3>}
      {children}
    </section>
  )
}

/** Label and value rows of a detail panel; rows without a value are left out. Keep it to about five rows. */
export function Facts({ rows }: { rows: [string, React.ReactNode][] }) {
  return (
    <dl className="space-y-1.5 text-sm">
      {rows.filter(([, v]) => v != null && v !== "" && v !== false).map(([k, v]) => (
        <div key={k} className="grid grid-cols-[112px_1fr] gap-3 max-sm:grid-cols-1 max-sm:gap-0.5"><dt className="text-muted-foreground">{k}</dt><dd className="min-w-0 break-words">{v}</dd></div>
      ))}
    </dl>
  )
}

/** A label and its value or control; the control drops under the label below 640px. `overridden` marks a value set here. */
export function SettingRow({ label, overridden, top, children }: { label: string; overridden?: boolean; top?: boolean; children: React.ReactNode }) {
  return (
    <div className={cn("grid grid-cols-[112px_1fr] gap-3 text-sm max-sm:grid-cols-1 max-sm:gap-1", top ? "items-start" : "items-center")}>
      <span className={cn("flex items-center gap-1.5 text-muted-foreground", top && "pt-1")}>
        {label}
        {overridden && <span className="size-1.5 shrink-0 rounded-full bg-primary" title="Set here" role="img" aria-label="Set here" />}
      </span>
      <div className="min-w-0">{children}</div>
    </div>
  )
}

export type DrawerTab = { value: string; label: string; hidden?: boolean; children: React.ReactNode }

/** Tabs under a panel head, bound to the `tab` URL param; the first tab is the default and leaves the URL clean. */
export function DrawerTabs({ tabs, param = "tab" }: { tabs: DrawerTab[]; param?: string }) {
  const [tab, setTab] = useParam(param)
  const shown = tabs.filter((t) => !t.hidden)
  const active = shown.find((t) => t.value === tab) ?? shown[0]
  if (!active) return null
  return (
    <div>
      <div role="tablist" className="flex gap-4 border-b px-5">
        {shown.map((t, i) => (
          <button
            key={t.value} role="tab" type="button" aria-selected={t === active}
            onClick={() => setTab(i === 0 ? null : t.value)}
            className={cn(
              "-mb-px flex h-10 items-center border-b-2 text-sm",
              t === active ? "border-link font-medium text-foreground" : "border-transparent text-muted-foreground hover:text-foreground",
            )}
          >{t.label}</button>
        ))}
      </div>
      <div role="tabpanel">{active.children}</div>
    </div>
  )
}
