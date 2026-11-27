import { MessageSquare } from "lucide-react"
import { Link } from "react-router"

import { Ago } from "@/components/common"
import { Tag } from "@/components/panel"
import { cn } from "@/lib/utils"
import { bytes } from "../chat/format"
import type { ArtifactVersion } from "./types"

export function Versions({ versions, viewing, onPick }: { versions: ArtifactVersion[]; viewing: number | null; onPick: (v: number | null) => void }) {
  return (
    <ul className="divide-y">
      {versions.map((v) => {
        const on = (viewing ?? versions.find((x) => x.current)?.version) === v.version
        return (
          <li key={v.version}>
            <div
              role="button" tabIndex={0} aria-pressed={on} onClick={() => onPick(v.current ? null : v.version)}
              onKeyDown={(e) => { if (e.target === e.currentTarget && (e.key === "Enter" || e.key === " ")) { e.preventDefault(); onPick(v.current ? null : v.version) } }}
              className={cn("flex cursor-pointer items-center gap-3 px-5 py-3 hover:bg-accent/50 focus-visible:bg-accent/50 focus-visible:outline-none", on && "bg-accent/60")}
            >
              <div className="min-w-0 flex-1">
                <div className="flex items-center gap-2 text-sm"><span className="font-medium">Version {v.version}</span>{v.current && <Tag>Current</Tag>}</div>
                <div className="mt-0.5 truncate text-xs text-muted-foreground">{v.by}, {v.agent}</div>
              </div>
              <div className="shrink-0 text-right text-xs text-muted-foreground">
                <div><Ago iso={v.created_at} /></div>
                <div className="tabular">{bytes(v.size_bytes)}</div>
              </div>
              {v.chat_url && (
                <Link to={v.chat_url} onClick={(e) => e.stopPropagation()} aria-label="Open chat" className="shrink-0 rounded-md p-1.5 text-muted-foreground hover:bg-accent hover:text-foreground"><MessageSquare className="size-3.5" /></Link>
              )}
            </div>
          </li>
        )
      })}
    </ul>
  )
}
