import { useMutation, useQueryClient } from "@tanstack/react-query"
import { useMemo, useState } from "react"
import { toast } from "sonner"

import { PanelSection, SettingRow } from "@/components/panel"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select"
import { Textarea } from "@/components/ui/textarea"
import { api } from "@/lib/api"
import { channelLabel } from "@/lib/format"
import { DEFAULT_SPEC, fromCron, KINDS, toCron, WEEKDAYS, type Kind, type Spec } from "./cron"
import type { CatchUp, Choices, Routine, RoutineChange } from "./types"

const NONE = "none"
const DM = "dm"

const zones = (current: string) => {
  const all = typeof Intl.supportedValuesOf === "function" ? Intl.supportedValuesOf("timeZone") : []
  return [...new Set(["UTC", current, ...all])]
}
const localZone = () => Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC"

/** The schedule picker: how often, when, and in which time zone. */
function SchedulePicker({ spec, zone, onSpec, onZone }: { spec: Spec; zone: string; onSpec: (s: Spec) => void; onZone: (z: string) => void }) {
  const list = useMemo(() => zones(zone), [zone])
  const set = (over: Partial<Spec>) => onSpec({ ...spec, ...over })
  return (
    <div className="space-y-2.5">
      <SettingRow label="Repeats">
        <Select value={spec.kind} onValueChange={(k) => set({ kind: k as Kind, cron: k === "custom" ? toCron(spec) : spec.cron })}>
          <SelectTrigger aria-label="Repeats" className="h-8 w-full"><SelectValue /></SelectTrigger>
          <SelectContent>{KINDS.map((k) => <SelectItem key={k.value} value={k.value}>{k.label}</SelectItem>)}</SelectContent>
        </Select>
      </SettingRow>
      {spec.kind === "weekly" && (
        <SettingRow label="On">
          <Select value={String(spec.weekday)} onValueChange={(d) => set({ weekday: Number(d) })}>
            <SelectTrigger aria-label="On" className="h-8 w-full"><SelectValue /></SelectTrigger>
            <SelectContent>{WEEKDAYS.map((d, i) => <SelectItem key={d} value={String(i)}>{d}</SelectItem>)}</SelectContent>
          </Select>
        </SettingRow>
      )}
      {spec.kind === "monthly" && (
        <SettingRow label="On day">
          <Select value={String(spec.day)} onValueChange={(d) => set({ day: Number(d) })}>
            <SelectTrigger aria-label="On day" className="h-8 w-full"><SelectValue /></SelectTrigger>
            <SelectContent>{Array.from({ length: 28 }, (_, i) => <SelectItem key={i} value={String(i + 1)}>{i + 1}</SelectItem>)}</SelectContent>
          </Select>
        </SettingRow>
      )}
      {spec.kind === "hourly" && (
        <SettingRow label="At minute">
          <Input aria-label="At minute" type="number" min={0} max={59} className="h-8" value={Number(spec.time.split(":")[1])}
            onChange={(e) => set({ time: `00:${String(Math.min(59, Math.max(0, Number(e.target.value) || 0))).padStart(2, "0")}` })} />
        </SettingRow>
      )}
      {["daily", "weekdays", "weekly", "monthly"].includes(spec.kind) && (
        <SettingRow label="At"><Input aria-label="At" type="time" className="h-8" value={spec.time} onChange={(e) => set({ time: e.target.value || "09:00" })} /></SettingRow>
      )}
      {spec.kind === "custom" && (
        <SettingRow label="Cron"><Input aria-label="Cron expression" className="h-8 font-mono" value={spec.cron} onChange={(e) => set({ cron: e.target.value })} /></SettingRow>
      )}
      <SettingRow label="Time zone">
        <Select value={zone} onValueChange={onZone}>
          <SelectTrigger aria-label="Time zone" className="h-8 w-full"><SelectValue /></SelectTrigger>
          <SelectContent>{list.map((z) => <SelectItem key={z} value={z}>{z}</SelectItem>)}</SelectContent>
        </Select>
      </SettingRow>
    </div>
  )
}

/**
 * Creates a routine, or changes one: only what differs is sent.
 * The Save footer shows only when something changed.
 */
export function Editor({ routine, choices, onDone }: { routine?: Routine; choices: Choices; onDone: (id?: string) => void }) {
  const qc = useQueryClient()
  const first = {
    name: routine?.name ?? "",
    agent: routine?.agent.id ?? choices.agents[0]?.id ?? "",
    cron: routine?.cron ?? toCron(DEFAULT_SPEC),
    zone: routine?.timezone ?? localZone(),
    dest: routine ? (routine.dm ? DM : routine.destination?.id ?? NONE) : NONE,
    message: routine?.message ?? "",
    catchUp: routine?.catch_up ?? ("skip" as CatchUp),
  }
  const [name, setName] = useState(first.name)
  const [agent, setAgent] = useState(first.agent)
  const [spec, setSpec] = useState<Spec>(routine ? fromCron(routine.cron) : DEFAULT_SPEC)
  const [zone, setZone] = useState(first.zone)
  const [dest, setDest] = useState(first.dest)
  const [message, setMessage] = useState(first.message)
  const [catchUp, setCatchUp] = useState<CatchUp>(first.catchUp)
  const cron = toCron(spec)
  const dirty = !routine || name.trim() !== first.name || agent !== first.agent || cron !== first.cron || zone !== first.zone
    || dest !== first.dest || message.trim() !== first.message || catchUp !== first.catchUp
  const discard = () => {
    setName(first.name); setAgent(first.agent); setSpec(routine ? fromCron(routine.cron) : DEFAULT_SPEC); setZone(first.zone)
    setDest(first.dest); setMessage(first.message); setCatchUp(first.catchUp)
  }

  // The agent and destination a routine already has stay pickable even if the lists no longer hold them.
  const agents = routine && !choices.agents.some((a) => a.id === routine.agent.id) ? [routine.agent, ...choices.agents] : choices.agents
  const dests = routine?.destination && !choices.destinations.some((d) => d.id === routine.destination!.id) ? [routine.destination, ...choices.destinations] : choices.destinations

  const save = useMutation({
    mutationFn: () => {
      if (!routine) return api.post<Routine>("/api/routines", { name: name.trim() || undefined, agent_id: agent, cron, timezone: zone, message: message.trim(), catch_up: catchUp, destination: dest === NONE || dest === DM ? null : dest, dm: dest === DM })
      const change: RoutineChange = {}
      if (name.trim() !== first.name) change.name = name.trim()
      if (agent !== first.agent) change.agent_id = agent
      if (cron !== first.cron) change.cron = cron
      if (zone !== first.zone) change.timezone = zone
      if (message.trim() !== first.message) change.message = message.trim()
      if (catchUp !== first.catchUp) change.catch_up = catchUp
      if (dest !== first.dest) { if (dest === NONE) change.clear_destination = true; else if (dest === DM) change.dm = true; else change.destination = dest }
      return api.patch<Routine>(`/api/routines/${routine.id}`, change)
    },
    onSuccess: (saved) => { void qc.invalidateQueries({ queryKey: ["routines"] }); toast(routine ? "Saved" : "Routine added"); onDone(saved.id) },
  })
  const ready = !!agent && !!message.trim() && !!cron && !!zone

  return (
    <form className="flex flex-1 flex-col" onSubmit={(e) => { e.preventDefault(); if (ready && dirty) save.mutate() }}>
      <PanelSection title="Routine">
        <div className="space-y-2.5">
          <SettingRow label="Name"><Input aria-label="Name" className="h-8" placeholder="Optional" value={name} maxLength={80} onChange={(e) => setName(e.target.value)} /></SettingRow>
          <SettingRow label="Agent">
            <Select value={agent} onValueChange={setAgent}>
              <SelectTrigger aria-label="Agent" className="h-8 w-full"><SelectValue placeholder="Choose" /></SelectTrigger>
              <SelectContent>{agents.map((a) => <SelectItem key={a.id} value={a.id}>{a.name}</SelectItem>)}</SelectContent>
            </Select>
          </SettingRow>
          <SettingRow label="Post to">
            <Select value={dest} onValueChange={setDest}>
              <SelectTrigger aria-label="Post to" className="h-8 w-full"><SelectValue /></SelectTrigger>
              <SelectContent>
                <SelectItem value={NONE}>No channel</SelectItem>
                {(choices.can_dm || routine?.dm) && <SelectItem value={DM}>Direct message</SelectItem>}
                {dests.map((d) => <SelectItem key={d.id} value={d.id}>{channelLabel(d.name, d.kind)}</SelectItem>)}
              </SelectContent>
            </Select>
          </SettingRow>
          <SettingRow label="Prompt" top><Textarea aria-label="Prompt" rows={5} value={message} onChange={(e) => setMessage(e.target.value)} /></SettingRow>
        </div>
      </PanelSection>
      <PanelSection title="Schedule">
        <SchedulePicker spec={spec} zone={zone} onSpec={setSpec} onZone={setZone} />
        <div className="mt-2.5">
          <SettingRow label="If missed">
            <Select value={catchUp} onValueChange={(v) => setCatchUp(v as CatchUp)}>
              <SelectTrigger aria-label="If a run is missed" className="h-8 w-full"><SelectValue /></SelectTrigger>
              <SelectContent>
                <SelectItem value="skip">Skip it</SelectItem>
                <SelectItem value="run-once">Run once when back</SelectItem>
              </SelectContent>
            </Select>
          </SettingRow>
        </div>
      </PanelSection>
      {dirty && (
        <div className="sticky bottom-0 mt-auto flex justify-end gap-2 border-t bg-popover px-5 py-3">
          <Button type="button" variant="ghost" disabled={save.isPending} onClick={() => (routine ? discard() : onDone())}>{routine ? "Discard" : "Cancel"}</Button>
          <Button type="submit" disabled={!ready || save.isPending}>{routine ? "Save" : "Add routine"}</Button>
        </div>
      )}
    </form>
  )
}
