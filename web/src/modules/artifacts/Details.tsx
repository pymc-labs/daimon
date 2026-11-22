import { CopyButton, Mono } from "@/components/common"
import { AgentLink } from "@/components/links"
import { Facts, PanelSection, SettingRow } from "@/components/panel"
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select"
import { Switch } from "@/components/ui/switch"
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip"
import { bytes } from "../chat/format"
import { absolute, plural } from "@/lib/format"
import { useChange } from "./bits"
import { refused } from "./rules"
import type { ArtifactDetail, DataAccess } from "./types"

/** A control that explains, on hover, why it is off. */
function Why({ reason, children }: { reason: string | null; children: React.ReactNode }) {
  if (!reason) return <>{children}</>
  return (
    <Tooltip>
      <TooltipTrigger asChild><span className="inline-flex">{children}</span></TooltipTrigger>
      <TooltipContent>{reason}</TooltipContent>
    </Tooltip>
  )
}

export function Details({ a }: { a: ArtifactDetail }) {
  const change = useChange(a.id)
  const no = refused(a.access, a)
  const edit = a.can.edit && !change.isPending
  return (
    <>
      <PanelSection>
        <Facts rows={[
          ["Agent", <AgentLink key="agent" id={a.agent.id} name={a.agent.name} avatar={a.agent.avatar} />],
          ["Owner", a.owner.name],
          ["Address", <span key="addr" className="flex min-w-0 items-center gap-1"><Mono className="min-w-0 truncate">{a.url}</Mono><CopyButton text={`${location.origin}${a.url}`} label="Copy address" className="size-6" /></span>],
          ["Created", absolute(a.created_at)],
          ["Updated", absolute(a.updated_at)],
          ["Size", bytes(a.size_bytes)],
          ["Expires", a.expires_at ? absolute(a.expires_at) : "Never"],
        ]} />
      </PanelSection>
      <PanelSection title="Page can">
        <div className="space-y-3">
          <SettingRow label="Shared data">
            <Select value={a.data} disabled={!edit} onValueChange={(v) => change.mutate({ data: v as DataAccess })}>
              <SelectTrigger size="sm" className="w-40" aria-label="Shared data"><SelectValue /></SelectTrigger>
              <SelectContent>
                <SelectItem value="off">Off</SelectItem>
                <SelectItem value="read" disabled={!!no.read}><Why reason={no.read}><span className="pointer-events-auto">Read</span></Why></SelectItem>
                <SelectItem value="write" disabled={!!no.write}><Why reason={no.write}><span className="pointer-events-auto">Read and write</span></Why></SelectItem>
              </SelectContent>
            </Select>
          </SettingRow>
          <SettingRow label="Send events">
            <div className="flex h-7 items-center gap-2.5">
              <Why reason={no.events}>
                <Switch checked={a.events} disabled={!edit || !!no.events} onCheckedChange={(v) => change.mutate({ events: v })} aria-label="Send events" />
              </Why>
              {a.events && <span className="text-xs text-muted-foreground">{plural(a.watches.length, "watch", "watches")}</span>}
            </div>
          </SettingRow>
          <SettingRow label="Ask the agent">
            <div className="flex h-7 items-center">
              <Why reason={no.ask}>
                <Switch checked={a.ask} disabled={!edit || !!no.ask} onCheckedChange={(v) => change.mutate({ ask: v })} aria-label="Ask the agent" />
              </Why>
            </div>
          </SettingRow>
          {a.data !== "off" && (
            <SettingRow label="Data space">
              <Select value={String(a.data_scale)} disabled={!edit} onValueChange={(v) => change.mutate({ data_scale: Number(v) })}>
                <SelectTrigger size="sm" className="w-24" aria-label="Data space"><SelectValue /></SelectTrigger>
                <SelectContent>{Array.from({ length: 10 }, (_, i) => <SelectItem key={i} value={String(i + 1)}>{i + 1}x</SelectItem>)}</SelectContent>
              </Select>
            </SettingRow>
          )}
        </div>
      </PanelSection>
    </>
  )
}
