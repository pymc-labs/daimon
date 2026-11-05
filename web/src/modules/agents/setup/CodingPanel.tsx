import { Trash2 } from "lucide-react"
import { useEffect, useRef, useState } from "react"

import { Ago, CopyButton, Mono, StatusDot } from "@/components/common"
import { Button } from "@/components/ui/button"
import { ToggleGroup, ToggleGroupItem } from "@/components/ui/toggle-group"
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip"
import { useConfirm } from "@/lib/confirm"
import { Pill } from "../bits"
import { Field, Section, type Props } from "../inspector/parts"
import type { ConnectBatch, MintedCodingKey } from "../types"
import { useCodingKeys, useCodingKeyWrites, useConnectBatch, useConnectStatus } from "./api"
import { clientName, CODING_CLIENTS } from "./clients"

/** A code lives 10 minutes; a new one replaces it a minute before. */
const REMINT_MS = 9 * 60_000

function CopyRow({ label, text }: { label: string; text: string }) {
  return (
    <Field label={label}>
      <span className="flex min-w-0 items-center gap-1">
        <Mono className="min-w-0 truncate">{text}</Mono>
        <CopyButton text={text} label={`Copy ${label}`} className="ml-auto size-6 shrink-0" />
      </span>
    </Field>
  )
}

/** One tool's prompt in a copy box, and whether the tool has used it yet. */
function Connect({ agentId, tab }: { agentId: string; tab: string }) {
  const [batch, setBatch] = useState<ConnectBatch | null>(null)
  const current = useRef<string | undefined>(undefined)
  const mint = useConnectBatch(agentId)
  const status = useConnectStatus(agentId, batch?.batch)
  const connected = status.data?.connected === true

  const refresh = () => mint.mutate(current.current, { onSuccess: (b) => { current.current = b.batch; setBatch(b) } })
  const started = useRef(false)
  useEffect(() => {
    if (started.current) return
    started.current = true
    refresh()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])
  useEffect(() => {
    if (!batch || connected) return
    const t = setInterval(refresh, REMINT_MS)
    return () => clearInterval(t)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [batch?.batch, connected])

  const prompt = batch?.prompts.find((p) => p.id === tab)

  return (
    <Section title={clientName(tab) ?? undefined}>
      <div className="space-y-3">
        {mint.isError && !batch ? (
          <div className="text-sm text-muted-foreground">{mint.error.message}</div>
        ) : connected ? null : !prompt ? (
          <div className="text-sm text-muted-foreground">Loading</div>
        ) : (
          <div className="relative">
            <pre aria-label="Prompt" className="max-h-44 overflow-y-auto rounded-md border bg-muted/50 p-2 pr-9 font-mono text-[11.5px] leading-[17px] whitespace-pre-wrap break-all">{prompt.prompt}</pre>
            <CopyButton text={prompt.prompt} label="Copy prompt" className="absolute top-1 right-1 size-6 bg-background/80" />
          </div>
        )}
        <Field label="Status">
          {connected ? <StatusDot status="healthy" label="Connected" /> : <StatusDot status="setup" label="Waiting" />}
        </Field>
        {connected ? (
          <Field label="Client"><span>{clientName(status.data?.client ?? null) ?? "Unknown"}</span></Field>
        ) : batch && (
          <Field label="Prompt ends"><Ago iso={batch.expires_at} /></Field>
        )}
        {connected && <Button variant="outline" size="sm" onClick={() => { current.current = undefined; setBatch(null); refresh() }}>Connect another</Button>}
      </div>
    </Section>
  )
}

/** Keys that let coding tools use this agent: connected by prompt or made by hand, listed and revoked here. */
export function CodingPanel({ agent }: Props) {
  const confirm = useConfirm()
  const keys = useCodingKeys(agent.id, agent.can_view_details)
  const writes = useCodingKeyWrites(agent.id)
  const [tab, setTab] = useState<string>(CODING_CLIENTS[0].id)
  const [made, setMade] = useState<MintedCodingKey | null>(null)

  const make = async () => {
    if (await confirm({ title: "Make a coding key?", body: "It acts as you.", action: "Make key" })) writes.make.mutate(undefined, { onSuccess: setMade })
  }
  const revoke = async (id: string) => {
    if (!(await confirm({ title: "Revoke this key?", body: "It stops working now.", action: "Revoke", destructive: true }))) return
    writes.revoke.mutate(id, { onSuccess: () => { if (made?.id === id) setMade(null) } })
  }

  return (
    <>
      <Section title="Tool">
        <ToggleGroup type="single" value={tab} onValueChange={(v) => v && setTab(v)} variant="segmented" size="sm" className="w-full">
          {CODING_CLIENTS.map(({ id, name, icon: Icon }) => (
            <Tooltip key={id}>
              <TooltipTrigger asChild>
                <ToggleGroupItem value={id} aria-label={name} className="flex-1"><Icon /></ToggleGroupItem>
              </TooltipTrigger>
              <TooltipContent>{name}</TooltipContent>
            </Tooltip>
          ))}
        </ToggleGroup>
      </Section>
      {tab === "token" ? (
        <Section title="Token" action={<Button variant="outline" size="xs" disabled={writes.make.isPending} onClick={() => void make()}>Make key</Button>}>
          {made ? (
            <div className="space-y-3">
              <CopyRow label="URL" text={made.url} />
              <CopyRow label="Token" text={made.token} />
              <Field label="Ends"><Ago iso={made.expires_at} /></Field>
              <Button variant="outline" size="sm" onClick={() => setMade(null)}>Done</Button>
            </div>
          ) : <div className="text-sm text-muted-foreground">No new key</div>}
        </Section>
      ) : <Connect agentId={agent.id} tab={tab} />}
      <Section title="Keys">
        {keys.isError ? <div className="text-sm text-muted-foreground">{keys.error.message}</div> : !keys.data?.length ? (
          <div className="text-sm text-muted-foreground">{keys.isPending ? "Loading" : "None"}</div>
        ) : (
          <div className="-my-1">
            {keys.data.map((k) => (
              <div key={k.id} className="flex items-center gap-2 py-1.5 text-sm">
                <div className="min-w-0">
                  <div className="flex items-center gap-1.5"><span className="truncate">{k.creator}</span>{k.mine && <Pill>You</Pill>}</div>
                  <div className="text-xs text-muted-foreground">{clientName(k.client) ?? "Unknown"}</div>
                </div>
                <div className="ml-auto text-right text-xs whitespace-nowrap text-muted-foreground">
                  <div>Made <Ago iso={k.created_at} /></div>
                  {k.expires_at && <div>Ends <Ago iso={k.expires_at} /></div>}
                </div>
                <Button variant="ghost" size="icon-sm" aria-label="Revoke" disabled={writes.revoke.isPending} onClick={() => void revoke(k.id)}><Trash2 /></Button>
              </div>
            ))}
          </div>
        )}
      </Section>
    </>
  )
}
