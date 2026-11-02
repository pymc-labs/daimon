import { Kbd } from "@/components/common"
import { Dialog, DialogContent, DialogHeader, DialogTitle } from "@/components/ui/dialog"
import { useMe } from "@/lib/hooks"
import { MOD } from "@/lib/utils"
import { navFor } from "@/modules/registry"

const FIXED: [string, [string, string[]][]][] = [
  ["General", [["Search", [MOD, "K"]], ["Filter list", ["/"]], ["New thread", [MOD, "O"]], ["Shortcuts", ["?"]]]],
  ["Lists", [["Open row", ["↵"]], ["Next / previous in drawer", ["J", "K"]], ["Close drawer", ["Esc"]]]],
  ["Chat", [["Send", ["↵"]], ["New line", ["⇧", "↵"]], ["Stop", ["Esc"]]]],
]

export function ShortcutsDialog({ open, onOpenChange }: { open: boolean; onOpenChange: (o: boolean) => void }) {
  const { data: me } = useMe()
  const goTo: [string, string[]][] = me ? navFor(me).filter((n) => n.g).map((n) => [n.label, ["G", n.g!.toUpperCase()]]) : []
  const groups = [FIXED[0]!, ["Go to", goTo] as (typeof FIXED)[number], ...FIXED.slice(1)]
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="sm:max-w-sm">
        <DialogHeader><DialogTitle>Keyboard shortcuts</DialogTitle></DialogHeader>
        <div className="space-y-5">
          {groups.map(([name, rows]) => rows.length > 0 && (
            <div key={name}>
              <div className="mb-1.5 text-xs font-medium text-muted-foreground">{name}</div>
              {rows.map(([label, keys]) => (
                <div key={label} className="flex h-8 items-center justify-between text-sm">
                  {label}
                  <span className="flex gap-1">{keys.map((k) => <Kbd key={k}>{k}</Kbd>)}</span>
                </div>
              ))}
            </div>
          ))}
        </div>
      </DialogContent>
    </Dialog>
  )
}
