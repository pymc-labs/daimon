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

/** True once the clock passes `iso`; one timer, no polling. */
function usePassed(iso: string | undefined) {
  const [now, setNow] = useState(() => Date.now())
  useEffect(() => {
    const ms = iso ? Date.parse(iso) - Date.now() : 0
    if (!(ms > 0)) return
    const t = setTimeout(() => setNow(Date.now()), Math.min(ms, 2 ** 31 - 1) + 5)
    return () => clearTimeout(t)
  }, [iso])
  return !!iso && Date.parse(iso) <= now
}

type Mint = { batch: ConnectBatch | null; loading: boolean; error: string | null; make: (replaces?: string) => void }

/** The agent's connect batch, kept above the tool choice so switching tools never mints. One batch when the panel opens; a new one only on request. */
function useBatch(agentId: string): Mint {
  const [batch, setBatch] = useState<ConnectBatch | null>(null)
  const mint = useConnectBatch(agentId)
  const make = (replaces?: string) => mint.mutate(replaces, { onSuccess: setBatch, onError: () => setBatch(null) })
  const started = useRef(false)
  useEffect(() => {
    if (started.current) return
    started.current = true
    make()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])
  return { batch, loading: mint.isPending, error: mint.isError ? mint.error.message : null, make }
}

/** One tool's prompt in a copy box, and whether the tool has used it yet. */
function Connect({ agentId, tab, mint }: { agentId: string; tab: string; mint: Mint }) {
  const { batch } = mint
  const expired = usePassed(batch?.expires_at)
  const status = useConnectStatus(agentId, batch?.batch, !expired)
  const connected = status.data?.connected === true
  const prompt = batch?.prompts.find((p) => p.id === tab)
  const stale = expired && !connected

  return (
    <Section title={clientName(tab) ?? undefined}>
      <div className="space-y-3">
        {mint.error ? (
          <>
            <div className="text-sm text-muted-foreground">{mint.error}</div>
            <Button variant="outline" size="sm" onClick={() => mint.make()}>New prompt</Button>
          </>
        ) : !batch ? (
          <div className="text-sm text-muted-foreground">Loading</div>
        ) : (
          <>
            {!connected && !stale && prompt && (
              <div className="relative">
                <pre aria-label="Prompt" className="max-h-44 overflow-y-auto rounded-md border bg-muted/50 p-2 pr-9 font-mono text-[11.5px] leading-[17px] whitespace-pre-wrap break-all">{prompt.prompt}</pre>
                <CopyButton text={prompt.prompt} label="Copy prompt" className="absolute top-1 right-1 size-6 bg-background/80" />
              </div>
            )}
            <Field label="Status">
              {connected ? <StatusDot status="healthy" label="Connected" /> : stale ? <StatusDot status="failing" label="Expired" /> : <StatusDot status="setup" label="Waiting" />}
            </Field>
            {connected ? (
              <Field label="Client"><span>{clientName(status.data?.client ?? null) ?? "Unknown"}</span></Field>
            ) : !stale && <Field label="Prompt ends"><Ago iso={batch.expires_at} /></Field>}
            {connected && <Button variant="outline" size="sm" disabled={mint.loading} onClick={() => mint.make()}>Connect another</Button>}
            {stale && <Button variant="outline" size="sm" disabled={mint.loading} onClick={() => mint.make(batch.batch)}>New prompt</Button>}
          </>
        )}
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
  const mint = useBatch(agent.id)
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
      ) : <Connect agentId={agent.id} tab={tab} mint={mint} />}
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
