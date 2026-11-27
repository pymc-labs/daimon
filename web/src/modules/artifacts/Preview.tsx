import { ExternalLink, Monitor, RefreshCw, Smartphone, Tablet } from "lucide-react"
import { useState } from "react"

import { Button } from "@/components/ui/button"
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select"
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip"
import { cn } from "@/lib/utils"
import type { ArtifactSummary, ArtifactVersion } from "./types"

const DEVICES = {
  desktop: { icon: Monitor, label: "Desktop", width: null },
  tablet: { icon: Tablet, label: "Tablet", width: 768 },
  phone: { icon: Smartphone, label: "Phone", width: 390 },
} as const
export type Device = keyof typeof DEVICES

/** The page in a frame on a muted canvas, with its toolbar. `version` previews an older one; `actions` sit at the right of the toolbar. */
export function Preview({ artifact: a, version, versions, onVersion, banner, actions, title, device: initial = "desktop", className }: {
  artifact: Pick<ArtifactSummary, "url" | "title" | "version">
  version?: number | null; versions?: ArtifactVersion[]; onVersion?: (v: number | null) => void
  banner?: React.ReactNode; actions?: React.ReactNode; title?: boolean; device?: Device; className?: string
}) {
  const [device, setDevice] = useState<Device>(initial)
  const [reload, setReload] = useState(0)
  const old = version != null && version !== a.version
  const src = old ? `${a.url}?v=${version}` : a.url
  const width = DEVICES[device].width
  return (
    <div className={cn("flex min-h-0 min-w-0 flex-1 flex-col", className)}>
      <div className="flex h-11 shrink-0 items-center gap-2 border-b px-3">
        {title && <span className="mr-1 min-w-0 truncate text-sm font-medium">{a.title}</span>}
        {versions && onVersion && (
          <Select value={String(version ?? a.version)} onValueChange={(v) => onVersion(Number(v) === a.version ? null : Number(v))}>
            <SelectTrigger size="sm" className="w-auto min-w-32 gap-2 text-xs" aria-label="Version"><SelectValue /></SelectTrigger>
            <SelectContent>
              {versions.map((v) => <SelectItem key={v.version} value={String(v.version)}>{`Version ${v.version}${v.current ? " (current)" : ""}`}</SelectItem>)}
            </SelectContent>
          </Select>
        )}
        <div role="group" aria-label="Device" className="ml-auto flex items-center gap-0.5 rounded-md bg-muted p-0.5">
          {(Object.entries(DEVICES) as [Device, (typeof DEVICES)[Device]][]).map(([k, d]) => (
            <Tooltip key={k}>
              <TooltipTrigger asChild>
                <button
                  type="button" aria-label={d.label} aria-pressed={device === k} onClick={() => setDevice(k)}
                  className={cn("flex h-6 w-7 items-center justify-center rounded text-muted-foreground hover:text-foreground aria-pressed:bg-background aria-pressed:text-foreground aria-pressed:shadow-xs dark:aria-pressed:bg-input")}
                ><d.icon className="size-3.5" /></button>
              </TooltipTrigger>
              <TooltipContent>{d.label}</TooltipContent>
            </Tooltip>
          ))}
        </div>
        <Tooltip>
          <TooltipTrigger asChild><Button variant="ghost" size="icon-sm" aria-label="Reload" onClick={() => setReload((n) => n + 1)}><RefreshCw /></Button></TooltipTrigger>
          <TooltipContent>Reload</TooltipContent>
        </Tooltip>
        <Tooltip>
          <TooltipTrigger asChild><Button variant="ghost" size="icon-sm" aria-label="Open in new tab" asChild><a href={src} target="_blank" rel="noreferrer"><ExternalLink /></a></Button></TooltipTrigger>
          <TooltipContent>Open in new tab</TooltipContent>
        </Tooltip>
        {actions}
      </div>
      {banner}
      <div className="flex min-h-0 flex-1 justify-center overflow-auto bg-muted p-4">
        <iframe
          key={`${src}|${reload}`} src={src} title={a.title}
          className={cn("h-full min-h-[480px] rounded-lg border bg-white shadow-[0_1px_2px_rgb(0_0_0/0.04)]", width ? "shrink-0" : "w-full")}
          style={width ? { width } : undefined}
        />
      </div>
    </div>
  )
}
