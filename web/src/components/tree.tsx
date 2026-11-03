import { ChevronRight } from "lucide-react"
import { Fragment, useMemo } from "react"

import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table"
import { plural } from "@/lib/format"
import { cn } from "@/lib/utils"
import { flatten, type Expanded, type TreeRow } from "./tree-state"

/** The first column holds the chevron and the indent; `className` applies to its head and cells (hide below a width with `max-sm:hidden`). */
export type TreeColumn<R> = { key: string; header: React.ReactNode; render: (row: R, node: TreeRow<R>) => React.ReactNode; className?: string }

const indent = (depth: number) => ({ paddingLeft: `calc(var(--tree-step, 16px) * ${depth})` })

/** A table whose first column is a tree: groups only fold, items open their drawer. */
export function TreeTable<R>({ rows, columns, onOpen, expanded, onToggle, filter, count, className }: {
  rows: TreeRow<R>[]
  columns: TreeColumn<R>[]
  onOpen: (row: R, node: TreeRow<R>) => void
  expanded: Expanded
  onToggle: (id: string, open: boolean) => void
  filter?: ((row: R) => boolean) | null
  /** Muted text beside a collapsed row, e.g. "3 threads". */
  count?: (node: TreeRow<R>, children: number) => string
  className?: string
}) {
  const flat = useMemo(() => flatten(rows, expanded, filter), [rows, expanded, filter])
  const [first, ...rest] = columns
  const filtered = !!filter
  if (!first) return null
  return (
    <Table className={cn("[--tree-step:16px] max-sm:[--tree-step:12px]", className)}>
      <TableHeader>
        <TableRow className="hover:bg-transparent">
          {columns.map((c, i) => <TableHead key={c.key} className={cn(i === 0 && "pl-4 lg:pl-6", i === columns.length - 1 && "pr-4 lg:pr-6", c.className)}>{c.header}</TableHead>)}
        </TableRow>
      </TableHeader>
      <TableBody>
        {flat.map(({ node, depth, children, open }) => {
          const group = node.kind === "group"
          const toggle = () => onToggle(node.id, !open)
          const label = !open && children > 0 ? (count?.(node, children) ?? plural(children, "item")) : null
          const chevron = children > 0 ? (
            <button
              type="button" aria-expanded={open} aria-label={open ? "Collapse" : "Expand"} disabled={filtered}
              onClick={(e) => { e.stopPropagation(); toggle() }}
              className="flex size-5 shrink-0 items-center justify-center rounded text-muted-foreground hover:bg-accent disabled:pointer-events-none"
            ><ChevronRight className={cn("size-4 transition-transform", open && "rotate-90")} /></button>
          ) : <span className="size-5 shrink-0" />
          const press = group ? toggle : () => onOpen(node.row, node)
          return (
            <Fragment key={node.id}>
              <TableRow
                tabIndex={0} data-depth={depth} data-kind={node.kind} aria-expanded={children > 0 ? open : undefined}
                className={cn("cursor-pointer", group && "bg-muted/40 hover:bg-muted/60")}
                onClick={press}
                onKeyDown={(e) => {
                  if (e.target !== e.currentTarget) return
                  if (e.key === "Enter" || e.key === " ") { e.preventDefault(); press() }
                  else if (e.key === "ArrowRight" && children > 0 && !open && !filtered) { e.preventDefault(); onToggle(node.id, true) }
                  else if (e.key === "ArrowLeft" && children > 0 && open && !filtered) { e.preventDefault(); onToggle(node.id, false) }
                }}
              >
                <TableCell colSpan={group ? columns.length : 1} className="pl-4 lg:pl-6">
                  <span className="flex min-w-0 items-center" style={indent(depth)}>
                    {chevron}
                    <span className={cn("flex min-w-0 flex-1 items-center gap-2", group && "text-xs font-medium text-muted-foreground")}>
                      {first.render(node.row, node)}
                      {label && <span className="shrink-0 text-xs font-normal text-muted-foreground">{label}</span>}
                    </span>
                  </span>
                </TableCell>
                {!group && rest.map((c, i) => (
                  <TableCell key={c.key} className={cn(i === rest.length - 1 && "pr-4 lg:pr-6", c.className)}>{c.render(node.row, node)}</TableCell>
                ))}
              </TableRow>
            </Fragment>
          )
        })}
      </TableBody>
    </Table>
  )
}
