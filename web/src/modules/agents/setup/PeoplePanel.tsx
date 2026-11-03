import { X } from "lucide-react"
import { useState } from "react"

import { PersonLink } from "@/components/links"
import { Avatar } from "@/components/common"
import { Button } from "@/components/ui/button"
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select"
import { useConfirm } from "@/lib/confirm"
import { useMe } from "@/lib/hooks"
import { Section, type Props } from "../inspector/parts"
import type { PersonRef } from "../types"
import { useOwnerOptions, useSetOwners } from "./api"

const NONE = "none"

/** Who owns the agent. Everyone reads it; an admin changes it. */
export function PeoplePanel({ agent }: Props) {
  const { data: me } = useMe()
  const admin = me?.role === "owner" || me?.role === "admin"
  if (!admin) {
    return (
      <Section title="People">
        <div className="space-y-1">
          {[{ ...agent.owner, role: "Owner" }, ...agent.co_owners.map((p) => ({ ...p, role: "Co-owner" }))].map((p) => (
            <div key={p.id} className="flex h-8 items-center gap-2 text-sm">
              <Avatar name={p.name} /><PersonLink id={p.id} name={p.name} /><span className="ml-auto text-xs text-muted-foreground">{p.role}</span>
            </div>
          ))}
        </div>
      </Section>
    )
  }
  return <OwnersForm key={`${agent.recorded_owner.id}|${agent.co_owners.map((p) => p.id).join(",")}`} {...{ agent }} />
}

function OwnersForm({ agent }: { agent: Props["agent"] }) {
  const confirm = useConfirm()
  const options = useOwnerOptions(agent.id, true)
  const save = useSetOwners(agent.id)
  const [owner, setOwner] = useState<string>(agent.recorded_owner.id || NONE)
  const [co, setCo] = useState<string[]>(agent.co_owners.map((p) => p.id))
  const known = new Map<string, PersonRef>([...(options.data ?? []), agent.owner, ...agent.co_owners].filter((p) => p.id).map((p) => [p.id, p]))
  const name = (id: string) => known.get(id)?.name ?? "A member"
  const same = owner === (agent.recorded_owner.id || NONE) && co.length === agent.co_owners.length && co.every((id, i) => id === agent.co_owners[i]?.id)
  const free = (options.data ?? []).filter((p) => p.id !== owner && !co.includes(p.id))

  const apply = async () => {
    if (!(await confirm({ title: "Change owners?", action: "Change" }))) return
    save.mutate({ owner: owner === NONE ? null : owner, co_owners: co.filter((id) => id !== owner) })
  }

  return (
    <>
      <Section title="Owner">
        <Select value={owner} onValueChange={setOwner}>
          <SelectTrigger size="sm" className="h-8 w-full" aria-label="Owner"><SelectValue>{owner === NONE ? "No owner" : name(owner)}</SelectValue></SelectTrigger>
          <SelectContent>
            <SelectItem value={NONE}>No owner</SelectItem>
            {[...known.values()].map((p) => <SelectItem key={p.id} value={p.id}>{p.name}</SelectItem>)}
          </SelectContent>
        </Select>
      </Section>
      <Section
        title="Co-owners"
        action={free.length > 0 && (
          <Select value="" onValueChange={(id) => setCo((c) => [...c, id])}>
            <SelectTrigger size="sm" className="h-7 text-xs" aria-label="Add co-owner"><SelectValue placeholder="Add co-owner" /></SelectTrigger>
            <SelectContent>{free.map((p) => <SelectItem key={p.id} value={p.id}>{p.name}</SelectItem>)}</SelectContent>
          </Select>
        )}
      >
        {!co.length ? <div className="text-sm text-muted-foreground">None</div> : (
          <div className="-my-1">
            {co.map((id) => (
              <div key={id} className="flex h-9 items-center gap-2 text-sm">
                <Avatar name={name(id)} />{name(id)}
                <Button variant="ghost" size="icon-sm" className="ml-auto" aria-label={`Remove ${name(id)}`} onClick={() => setCo((c) => c.filter((x) => x !== id))}><X /></Button>
              </div>
            ))}
          </div>
        )}
      </Section>
      <Section>
        <Button size="sm" disabled={same || save.isPending} onClick={() => void apply()}>Save</Button>
      </Section>
    </>
  )
}
