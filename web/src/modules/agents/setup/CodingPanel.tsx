import { Trash2 } from "lucide-react"
import { useState } from "react"

import { Ago, CopyButton } from "@/components/common"
import { Button } from "@/components/ui/button"
import { useConfirm } from "@/lib/confirm"
import { Pill } from "../bits"
import { Section, type Props } from "../inspector/parts"
import type { MintedCodingKey } from "../types"
import { useCodingKeys, useCodingKeyWrites } from "./api"

function Block({ label, text }: { label: string; text: string }) {
  return (
    <div>
      <div className="mb-1 flex items-center text-xs text-muted-foreground">{label}<CopyButton text={text} label={`Copy ${label}`} className="ml-auto size-6" /></div>
      <pre className="overflow-x-auto rounded-md border bg-muted/50 p-2 font-mono text-[11.5px] leading-[17px] whitespace-pre-wrap break-all">{text}</pre>
    </div>
  )
}

/** Keys that let coding tools use this agent: made here, shown once, revoked here. */
export function CodingPanel({ agent }: Props) {
  const confirm = useConfirm()
  const keys = useCodingKeys(agent.id, agent.can_view_details)
  const writes = useCodingKeyWrites(agent.id)
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
      <Section title="Keys" action={<Button variant="outline" size="xs" disabled={writes.make.isPending} onClick={() => void make()}>Make key</Button>}>
        {keys.isError ? <div className="text-sm text-muted-foreground">{keys.error.message}</div> : !keys.data?.length ? (
          <div className="text-sm text-muted-foreground">{keys.isPending ? "Loading" : "None"}</div>
        ) : (
          <div className="-my-1">
            {keys.data.map((k) => (
              <div key={k.id} className="flex h-9 items-center gap-2 text-sm">
                <span className="truncate">{k.creator}</span>
                {k.mine && <Pill>You</Pill>}
                <span className="ml-auto text-xs whitespace-nowrap text-muted-foreground">
                  <Ago iso={k.created_at} />{k.expires_at && <>, ends <Ago iso={k.expires_at} /></>}
                </span>
                <Button variant="ghost" size="icon-sm" aria-label="Revoke" disabled={writes.revoke.isPending} onClick={() => void revoke(k.id)}><Trash2 /></Button>
              </div>
            ))}
          </div>
        )}
      </Section>
      {made && (
        <Section title="New key, shown once">
          <div className="space-y-3">
            <Block label="Command" text={made.command} />
            <Block label=".mcp.json" text={made.config} />
            <Button variant="outline" size="sm" onClick={() => setMade(null)}>Done</Button>
          </div>
        </Section>
      )}
    </>
  )
}
