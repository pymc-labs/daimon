import { ChevronDown, ChevronUp, Link2, X } from "lucide-react"
import { toast } from "sonner"

import { useParam } from "@/components/filters"
import { Button } from "@/components/ui/button"
import { Sheet, SheetClose, SheetContent, SheetTitle } from "@/components/ui/sheet"
import { cn } from "@/lib/utils"

/** Right drawer bound to a URL param. `ids` enables J/K stepping. */
export function Drawer({ param, title, children, ids, wide }: {
  param: string; title: string; children: (id: string) => React.ReactNode; ids?: string[]; wide?: boolean
}) {
  const [id, setId] = useParam(param)
  const i = id && ids ? ids.indexOf(id) : -1
  const step = (d: number) => { const n = ids?.[i + d]; if (i >= 0 && n) setId(n) }
  return (
    <Sheet open={!!id} onOpenChange={(o) => !o && setId(null)}>
      <SheetContent
        showCloseButton={false}
        className={cn("w-full gap-0 overflow-y-auto p-0 sm:max-w-[560px]", wide && "sm:max-w-[680px]")}
        onKeyDown={(e) => {
          if ((e.target as HTMLElement).closest("input, textarea, [contenteditable]")) return
          if (e.key === "ArrowDown" || e.key === "j") { e.preventDefault(); step(1) }
          if (e.key === "ArrowUp" || e.key === "k") { e.preventDefault(); step(-1) }
        }}
      >
        <div className="sticky top-0 z-10 flex h-11 shrink-0 items-center gap-1 border-b bg-popover px-3">
          <SheetTitle className="mr-auto pl-1 text-xs font-medium text-muted-foreground">{title}</SheetTitle>
          {ids && i >= 0 && (
            <>
              <span className="mr-1 text-xs text-faint tabular">{i + 1} / {ids.length}</span>
              <Button variant="ghost" size="icon-sm" aria-label="Previous" disabled={i <= 0} onClick={() => step(-1)}><ChevronUp /></Button>
              <Button variant="ghost" size="icon-sm" aria-label="Next" disabled={i >= ids.length - 1} onClick={() => step(1)}><ChevronDown /></Button>
            </>
          )}
          <Button variant="ghost" size="icon-sm" aria-label="Copy link" onClick={() => { navigator.clipboard.writeText(location.href); toast("Link copied") }}><Link2 /></Button>
          <SheetClose asChild><Button variant="ghost" size="icon-sm" aria-label="Close"><X /></Button></SheetClose>
        </div>
        {id && children(id)}
      </SheetContent>
    </Sheet>
  )
}
