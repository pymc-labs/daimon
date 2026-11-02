import { ChevronRight, RotateCcw } from "lucide-react"
import { useState } from "react"

import { Ago, Avatar } from "@/components/common"
import { useConfirm } from "@/lib/confirm"
import { Button } from "@/components/ui/button"
import { Skeleton } from "@/components/ui/skeleton"
import { cn } from "@/lib/utils"
import { useRevert, useVersions } from "./api"
import { ChangeList } from "./Draft"
import type { Agent, Change } from "./types"

export function History({ agent }: { agent: Agent }) {
  const { data } = useVersions(agent.id)
  const revert = useRevert(agent.id)
  const confirm = useConfirm()
  const [open, setOpen] = useState<number | null>(null)
  if (!data) return <div className="space-y-2 p-5">{Array.from({ length: 5 }, (_, i) => <Skeleton key={i} className="h-12" />)}</div>
  return (
    <div className="py-2">
      {data.map((v) => {
        const cur = v.version === agent.version
        const on = open === v.version
        const summary = v.note ?? summarize(v.changes)
        return (
          <div key={v.version} className={cn("border-b last:border-b-0", on && "bg-muted/40")}>
            <button onClick={() => setOpen(on ? null : v.version)} className="flex w-full items-start gap-3 px-5 py-3 text-left hover:bg-accent/60">
              <span className={cn("mt-0.5 inline-flex h-5 min-w-8 items-center justify-center rounded border px-1 font-mono text-[11px]", cur ? "border-ring/40 bg-ring/10 text-foreground" : "text-muted-foreground")}>
                v{v.version}
              </span>
              <span className="min-w-0 flex-1">
                <span className="flex items-center gap-2 text-sm">
                  <span className="truncate font-medium">{summary}</span>
                  {cur && <span className="shrink-0 text-xs text-success">Current</span>}
                </span>
                <span className="mt-0.5 flex items-center gap-1.5 text-xs text-muted-foreground">
                  <Avatar name={v.by.name} className="size-4 text-[8px]" />{v.by.name}<span className="text-faint">·</span><Ago iso={v.at} />
                  {v.changes.length > 0 && <><span className="text-faint">·</span>{v.changes.length} {v.changes.length === 1 ? "change" : "changes"}</>}
                </span>
              </span>
              <ChevronRight className={cn("mt-1 size-4 shrink-0 text-faint transition-transform", on && "rotate-90")} />
            </button>
            {on && (
              <div className="space-y-3 px-5 pb-4">
                {v.changes.length > 0 && <ChangeList changes={v.changes} showEffect={false} />}
                {!cur && agent.can_edit && (
                  <Button variant="outline" size="sm" disabled={revert.isPending} onClick={async () => {
                    if (await confirm({ title: `Revert to v${v.version}?`, body: `Saved as v${agent.version + 1}.`, action: "Revert" })) revert.mutate(v.version)
                  }}>
                    <RotateCcw />Revert to v{v.version}
                  </Button>
                )}
              </div>
            )}
          </div>
        )
      })}
    </div>
  )
}

function summarize(cs: Change[]) {
  if (!cs.length) return "No changes"
  const k = cs.every((c) => c.kind === cs[0]!.kind) ? cs[0]!.kind : "edit"
  const list = cs.slice(0, 2).map((c) => c.label).join(", ") + (cs.length > 2 ? ` +${cs.length - 2}` : "")
  return k === "add" ? `Added ${list}` : k === "remove" ? `Removed ${list}` : list
}
