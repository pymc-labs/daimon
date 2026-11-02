import { useMutation, useQueryClient } from "@tanstack/react-query"
import { useMemo, useState } from "react"
import { toast } from "sonner"

import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select"
import { Textarea } from "@/components/ui/textarea"
import { api } from "@/lib/api"
import { channelLabel } from "@/lib/format"
import { DEFAULT_SPEC, fromCron, KINDS, toCron, WEEKDAYS, type Kind, type Spec } from "./cron"
import { Field } from "../credits/shared"
import type { CatchUp, Choices, Routine, RoutineChange } from "./types"

const NONE = "none"

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
    <div className="space-y-3">
      <div className="grid grid-cols-2 gap-3">
        <Field label="Repeats">
          <Select value={spec.kind} onValueChange={(k) => set({ kind: k as Kind, cron: k === "custom" ? toCron(spec) : spec.cron })}>
            <SelectTrigger className="w-full"><SelectValue /></SelectTrigger>
            <SelectContent>{KINDS.map((k) => <SelectItem key={k.value} value={k.value}>{k.label}</SelectItem>)}</SelectContent>
          </Select>
        </Field>
        {spec.kind === "weekly" && (
          <Field label="On">
            <Select value={String(spec.weekday)} onValueChange={(d) => set({ weekday: Number(d) })}>
              <SelectTrigger className="w-full"><SelectValue /></SelectTrigger>
              <SelectContent>{WEEKDAYS.map((d, i) => <SelectItem key={d} value={String(i)}>{d}</SelectItem>)}</SelectContent>
            </Select>
          </Field>
        )}
        {spec.kind === "monthly" && (
          <Field label="On day">
            <Select value={String(spec.day)} onValueChange={(d) => set({ day: Number(d) })}>
              <SelectTrigger className="w-full"><SelectValue /></SelectTrigger>
              <SelectContent>{Array.from({ length: 28 }, (_, i) => <SelectItem key={i} value={String(i + 1)}>{i + 1}</SelectItem>)}</SelectContent>
            </Select>
          </Field>
        )}
        {spec.kind === "hourly" && (
          <Field label="At minute">
            <Input type="number" min={0} max={59} value={Number(spec.time.split(":")[1])}
              onChange={(e) => set({ time: `00:${String(Math.min(59, Math.max(0, Number(e.target.value) || 0))).padStart(2, "0")}` })} />
          </Field>
        )}
        {["daily", "weekdays", "weekly", "monthly"].includes(spec.kind) && (
          <Field label="At"><Input type="time" value={spec.time} onChange={(e) => set({ time: e.target.value || "09:00" })} /></Field>
        )}
      </div>
      {spec.kind === "custom" && (
        <Field label="Cron expression"><Input className="font-mono" value={spec.cron} onChange={(e) => set({ cron: e.target.value })} /></Field>
      )}
      <Field label="Time zone">
        <Select value={zone} onValueChange={onZone}>
          <SelectTrigger className="w-full"><SelectValue /></SelectTrigger>
          <SelectContent>{list.map((z) => <SelectItem key={z} value={z}>{z}</SelectItem>)}</SelectContent>
        </Select>
      </Field>
    </div>
  )
}

/** Creates a routine, or changes one: only what differs is sent. */
export function Editor({ routine, choices, onDone }: { routine?: Routine; choices: Choices; onDone: (id?: string) => void }) {
  const qc = useQueryClient()
  const [agent, setAgent] = useState(routine?.agent.id ?? choices.agents[0]?.id ?? "")
  const [spec, setSpec] = useState<Spec>(routine ? fromCron(routine.cron) : DEFAULT_SPEC)
  const [zone, setZone] = useState(routine?.timezone ?? localZone())
  const [dest, setDest] = useState(routine?.destination?.id ?? NONE)
  const [message, setMessage] = useState(routine?.message ?? "")
  const [catchUp, setCatchUp] = useState<CatchUp>(routine?.catch_up ?? "skip")
  const cron = toCron(spec)

  // The agent and destination a routine already has stay pickable even if the lists no longer hold them.
  const agents = routine && !choices.agents.some((a) => a.id === routine.agent.id) ? [routine.agent, ...choices.agents] : choices.agents
  const dests = routine?.destination && !choices.destinations.some((d) => d.id === routine.destination!.id) ? [routine.destination, ...choices.destinations] : choices.destinations

  const save = useMutation({
    mutationFn: () => {
      if (!routine) return api.post<Routine>("/api/routines", { agent_id: agent, cron, timezone: zone, message: message.trim(), catch_up: catchUp, destination: dest === NONE ? null : dest })
      const change: RoutineChange = {}
      if (agent !== routine.agent.id) change.agent_id = agent
      if (cron !== routine.cron) change.cron = cron
      if (zone !== routine.timezone) change.timezone = zone
      if (message.trim() !== routine.message) change.message = message.trim()
      if (catchUp !== routine.catch_up) change.catch_up = catchUp
      if (dest !== (routine.destination?.id ?? NONE)) { if (dest === NONE) change.clear_destination = true; else change.destination = dest }
      return api.patch<Routine>(`/api/routines/${routine.id}`, change)
    },
    onSuccess: (saved) => { void qc.invalidateQueries({ queryKey: ["routines"] }); toast(routine ? "Saved" : "Routine added"); onDone(saved.id) },
  })
  const ready = !!agent && !!message.trim() && !!cron && !!zone

  return (
    <form className="space-y-4 px-5 py-5" onSubmit={(e) => { e.preventDefault(); if (ready) save.mutate() }}>
      <Field label="Agent">
        <Select value={agent} onValueChange={setAgent}>
          <SelectTrigger className="w-full"><SelectValue placeholder="Choose" /></SelectTrigger>
          <SelectContent>{agents.map((a) => <SelectItem key={a.id} value={a.id}>{a.name}</SelectItem>)}</SelectContent>
        </Select>
      </Field>
      <SchedulePicker spec={spec} zone={zone} onSpec={setSpec} onZone={setZone} />
      <Field label="Post to">
        <Select value={dest} onValueChange={setDest}>
          <SelectTrigger className="w-full"><SelectValue /></SelectTrigger>
          <SelectContent>
            <SelectItem value={NONE}>No channel</SelectItem>
            {dests.map((d) => <SelectItem key={d.id} value={d.id}>{channelLabel(d.name)}</SelectItem>)}
          </SelectContent>
        </Select>
      </Field>
      <Field label="Prompt"><Textarea rows={5} value={message} onChange={(e) => setMessage(e.target.value)} /></Field>
      <Field label="If a run is missed">
        <Select value={catchUp} onValueChange={(v) => setCatchUp(v as CatchUp)}>
          <SelectTrigger className="w-full"><SelectValue /></SelectTrigger>
          <SelectContent>
            <SelectItem value="skip">Skip it</SelectItem>
            <SelectItem value="run-once">Run once when back</SelectItem>
          </SelectContent>
        </Select>
      </Field>
      <div className="flex justify-end gap-2 pt-2">
        <Button type="button" variant="outline" onClick={() => onDone()}>Cancel</Button>
        <Button type="submit" disabled={!ready || save.isPending}>{routine ? "Save" : "Add routine"}</Button>
      </div>
    </form>
  )
}
