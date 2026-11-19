import { Trash2 } from "lucide-react"
import { useState } from "react"

import { Ago, Mono } from "@/components/common"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { useConfirm } from "@/lib/confirm"
import { NotSupported } from "../bits"
import { Section, type Props } from "../inspector/parts"
import { NOTHING } from "../unsupported"
import { useAgentKeys, useAgentKeyWrites } from "./api"

/** The names of the agent's stored keys, and a form that only writes. */
export function KeysPanel({ agent, unsupported = NOTHING }: Props) {
  const lost = unsupported.capabilities.has("vaults")
  const confirm = useConfirm()
  const keys = useAgentKeys(agent.id, agent.can_view_details)
  const writes = useAgentKeyWrites(agent.id)
  const [name, setName] = useState("")
  const [value, setValue] = useState("")
  const held = (keys.data ?? []).some((k) => k.key === name.trim())
  const ready = name.trim() !== "" && value.trim() !== ""

  const submit = async () => {
    const key = name.trim()
    const ok = await confirm({ title: `${held ? "Replace" : "Add"} ${key}?`, action: held ? "Replace" : "Add", destructive: held })
    if (!ok) return
    writes.save.mutate({ key, value }, { onSuccess: () => { setName(""); setValue("") } })
  }
  const remove = async (key: string) => {
    if (await confirm({ title: `Remove ${key}?`, action: "Remove", destructive: true })) writes.remove.mutate(key)
  }

  return (
    <>
      <Section title="Keys">
        {keys.isError ? <div className="text-sm text-muted-foreground">{keys.error.message}</div> : !keys.data?.length ? (
          <div className="text-sm text-muted-foreground">{keys.isPending ? "Loading" : "None"}</div>
        ) : (
          <div className="-my-1">
            {keys.data.map((k) => (
              <div key={k.key} className="flex h-9 items-center gap-2 text-sm">
                <Mono className="truncate">{k.key}</Mono>
                <Ago iso={k.updated_at} className="ml-auto text-xs text-muted-foreground" />
                <Button variant="ghost" size="icon-sm" aria-label={`Remove ${k.key}`} disabled={writes.remove.isPending} onClick={() => void remove(k.key)}>
                  <Trash2 />
                </Button>
              </div>
            ))}
          </div>
        )}
      </Section>
      <Section title="Add a key" action={lost && <NotSupported />}>
        <form className="space-y-2" autoComplete="off" onSubmit={(e) => { e.preventDefault(); if (ready) void submit() }}>
          <Input aria-label="Name" placeholder="Name" value={name} onChange={(e) => setName(e.target.value)} spellCheck={false} autoCapitalize="characters" className="h-8 font-mono text-[12.5px]" />
          <Input aria-label="Value" placeholder="Value" type="password" value={value} onChange={(e) => setValue(e.target.value)} autoComplete="new-password" spellCheck={false} className="h-8 font-mono text-[12.5px]" />
          <Button type="submit" variant="outline" size="sm" disabled={!ready || lost || writes.save.isPending}>{held ? "Replace" : "Add"}</Button>
        </form>
      </Section>
    </>
  )
}
