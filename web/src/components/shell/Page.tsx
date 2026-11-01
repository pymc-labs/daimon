import { Menu } from "lucide-react"
import { Fragment, useEffect, useRef } from "react"
import { Link, NavLink, useLocation } from "react-router"

import { cn } from "@/lib/utils"
import { useShell } from "./shell-context"

type Crumb = { label: string; to?: string }

export function Page({ crumbs, actions, tabs, children, className, flush }: {
  crumbs: Crumb[]; actions?: React.ReactNode; tabs?: { label: React.ReactNode; to: string; end?: boolean }[]
  children: React.ReactNode; className?: string; flush?: boolean
}) {
  const { openNav } = useShell()
  const bar = useRef<HTMLDivElement>(null)
  const { pathname } = useLocation()
  const title = [...crumbs].reverse().map((c) => c.label).join(", ")
  useEffect(() => { document.title = title ? `${title} - Daimon` : "Daimon" }, [title])
  useEffect(() => { bar.current?.querySelector("[aria-current=page]")?.scrollIntoView({ block: "nearest", inline: "nearest" }) }, [pathname])
  return (
    <div className="flex min-h-0 min-w-0 flex-1 flex-col">
      <header className="flex h-12 shrink-0 items-center gap-2 border-b px-4 lg:px-6">
        <button className="-ml-1 rounded-md p-1 text-muted-foreground hover:bg-accent lg:hidden" onClick={openNav} aria-label="Menu">
          <Menu className="size-4" />
        </button>
        <nav className="flex min-w-0 items-center gap-1.5 text-sm">
          {crumbs.map((c, i) => (
            <Fragment key={i}>
              {i > 0 && <span className="text-faint">/</span>}
              {c.to && i < crumbs.length - 1 ? (
                <Link to={c.to} className="truncate text-muted-foreground hover:text-foreground">{c.label}</Link>
              ) : (
                <span className={cn("truncate", i === crumbs.length - 1 ? "font-medium" : "text-muted-foreground")}>{c.label}</span>
              )}
            </Fragment>
          ))}
        </nav>
        <div className="ml-auto flex items-center gap-2">{actions}</div>
      </header>
      {tabs && (
        <div ref={bar} className="flex shrink-0 gap-4 overflow-x-auto overflow-y-hidden px-4 shadow-[inset_0_-1px_0_var(--border)] [scrollbar-width:none] lg:px-6">
          {tabs.map((t) => (
            <NavLink
              key={t.to}
              to={t.to}
              end={t.end}
              className={({ isActive }) => cn(
                "flex h-10 shrink-0 items-center border-b-2 text-sm whitespace-nowrap transition-colors",
                isActive ? "border-link font-medium text-foreground" : "border-transparent text-muted-foreground hover:text-foreground",
              )}
            >
              {t.label}
            </NavLink>
          ))}
        </div>
      )}
      <main className={cn("min-h-0 flex-1 overflow-y-auto", !flush && "px-4 py-6 lg:px-6", className)}>{children}</main>
    </div>
  )
}
