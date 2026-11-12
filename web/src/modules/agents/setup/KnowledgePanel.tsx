import { useState } from "react"

import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select"
import { cn } from "@/lib/utils"
import { NotSupported } from "../bits"
import { Section, type Props } from "../inspector/parts"
import { NOTHING } from "../unsupported"
import { useKnowledge, useMemoryPaths, useMemoryText } from "./api"

const SHARED = "shared"

/** What the agent remembers, to read: its shared memory, and for admins each channel's. */
export function KnowledgePanel({ agent, unsupported = NOTHING }: Props) {
  const lost = unsupported.capabilities.has("memory") || unsupported.capabilities.has("memory_stores")
  const stores = useKnowledge(agent.id, agent.can_view_details)
  const [picked, setPicked] = useState("")
  const [path, setPath] = useState("")
  const options = [
    ...(stores.data?.shared ? [{ store: SHARED, name: "Shared" }] : []),
    ...(stores.data?.channels ?? []).map((c) => ({ store: c.store, name: c.name })),
  ]
  const store = options.some((o) => o.store === picked) ? picked : options[0]?.store ?? ""
  const paths = useMemoryPaths(agent.id, store)
  const text = useMemoryText(agent.id, store, path)

  if (stores.isError) return <Section title="Memory"><div className="text-sm text-muted-foreground">{stores.error.message}</div></Section>
  if (!options.length) return <Section title="Memory"><div className="text-sm text-muted-foreground">{stores.isPending ? "Loading" : "None"}</div></Section>
  return (
    <>
      <Section title="Memory" action={lost && <NotSupported />}>
        <Select value={store} onValueChange={(v) => { setPicked(v); setPath("") }}>
          <SelectTrigger size="sm" className="w-full" aria-label="Memory"><SelectValue /></SelectTrigger>
          <SelectContent>{options.map((o) => <SelectItem key={o.store} value={o.store}>{o.name}</SelectItem>)}</SelectContent>
        </Select>
      </Section>
      <Section title="Files">
        {paths.isError ? <div className="text-sm text-muted-foreground">{paths.error.message}</div> : !paths.data?.length ? (
          <div className="text-sm text-muted-foreground">{paths.isPending ? "Loading" : "None"}</div>
        ) : (
          <div className="-mx-1">
            {paths.data.map((p) => (
              <button
                key={p} type="button" onClick={() => setPath(p === path ? "" : p)}
                className={cn("flex h-8 w-full items-center rounded px-1 text-left font-mono text-[12.5px] hover:bg-accent", p === path && "bg-accent")}
              ><span className="truncate">{p}</span></button>
            ))}
          </div>
        )}
      </Section>
      {path && (
        <Section title={path}>
          {text.isError ? <div className="text-sm text-muted-foreground">{text.error.message}</div> : (
            <pre className="max-h-96 overflow-auto rounded-md border bg-muted/50 p-2 font-mono text-[11.5px] leading-[17px] whitespace-pre-wrap break-words">{text.data?.content ?? "Loading"}</pre>
          )}
        </Section>
      )}
    </>
  )
}
