import { useMutation, useQueryClient } from "@tanstack/react-query"
import { Briefcase, ChartLine, GitPullRequest, Inbox, SquareDashed } from "lucide-react"
import { useState } from "react"
import { useNavigate } from "react-router"
import { toast } from "sonner"

import { Page } from "@/components/shell/Page"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select"
import { Skeleton } from "@/components/ui/skeleton"
import { api } from "@/lib/api"
import { useCan } from "@/lib/hooks"
import { cn } from "@/lib/utils"
import { NotFound } from "@/pages/Status"
import { modelValue, useCatalog, useTemplates } from "./api"
import { PROVIDER } from "./lib"
import type { Agent } from "./types"

const ICON: Record<string, React.ReactNode> = {
  "square-dashed": <SquareDashed />, "chart-line": <ChartLine />, "git-pull-request": <GitPullRequest />, briefcase: <Briefcase />, inbox: <Inbox />,
}

export function NewAgent() {
  const can = useCan()
  const qc = useQueryClient()
  const navigate = useNavigate()
  const { data: templates } = useTemplates()
  const { data: catalog } = useCatalog()
  const [tpl, setTpl] = useState("analyst")
  const [name, setName] = useState("")
  const [model, setModel] = useState<string>("")
  const t = templates?.find((x) => x.id === tpl)
  const create = useMutation({
    mutationFn: () => api.post<Agent>("/api/agents", { name: name.trim(), template: tpl, model: sent }),
    onSuccess: (a) => {
      void qc.invalidateQueries({ queryKey: ["agents"], exact: true })
      void qc.invalidateQueries({ queryKey: ["chat", "agents"] })
      navigate(`/agents/${a.id}${tpl === "blank" ? "?node=instructions" : ""}`)
      toast(`Created ${a.name}`)
    },
    onError: (e) => toast.error(e.message),
  })
  if (!can("agents.create")) return <NotFound />
  const defaultModel = catalog?.models.find((m) => m.label === t?.model)
  const picked = catalog?.models.find((m) => modelValue(m) === model) ?? defaultModel
  // Claude models go by their plain id, the others as `provider:id`.
  const sent = model ? (picked?.backend === "anthropic" ? picked.id : model) : undefined
  return (
    <Page crumbs={[{ label: "Agents", to: "/agents" }, { label: "New agent" }]}>
      <form className="mx-auto max-w-3xl space-y-8 pb-10" onSubmit={(e) => { e.preventDefault(); if (name.trim()) create.mutate() }}>
        <section className="space-y-3">
          <h2 className="text-sm font-medium">Start from</h2>
          <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
            {!templates ? Array.from({ length: 5 }, (_, i) => <Skeleton key={i} className="h-28 rounded-xl" />) : templates.map((x) => (
              <button
                type="button"
                key={x.id}
                onClick={() => { setTpl(x.id); setModel("") }}
                className={cn(
                  "flex flex-col items-start gap-3 rounded-xl border bg-card p-4 text-left transition-[border-color,box-shadow] hover:border-border-strong",
                  tpl === x.id && "border-ring ring-3 ring-ring/15 hover:border-ring",
                )}
              >
                <span className="flex size-8 items-center justify-center rounded-lg border bg-muted text-muted-foreground [&_svg]:size-4">{ICON[x.icon]}</span>
                <span className="min-w-0">
                  <span className="block text-sm font-medium">{x.name}</span>
                  <span className="block truncate text-xs text-muted-foreground">{x.purpose || "Empty agent"}</span>
                </span>
                <span className="text-[11px] text-faint tabular">
                  {x.id === "blank" ? x.model : `${x.skills} ${x.skills === 1 ? "skill" : "skills"}, ${x.servers} ${x.servers === 1 ? "server" : "servers"}, ${x.model}`}
                </span>
              </button>
            ))}
          </div>
        </section>
        <section className="max-w-md space-y-4">
          <div className="space-y-1.5">
            <Label htmlFor="ag-name">Name</Label>
            <Input id="ag-name" value={name} onChange={(e) => setName(e.target.value)} placeholder={t?.name === "Blank" ? "" : t?.name} autoFocus className="h-8" />
          </div>
          <div className="space-y-1.5">
            <Label>Model</Label>
            <Select value={picked ? modelValue(picked) : ""} onValueChange={setModel}>
              <SelectTrigger className="h-8 w-full"><SelectValue>{picked?.label}</SelectValue></SelectTrigger>
              <SelectContent>
                {catalog?.models.map((m) => (
                  <SelectItem key={modelValue(m)} value={modelValue(m)}>
                    <span className="flex w-full items-center gap-2">
                      {m.label}
                      <span className="inline-flex h-5 items-center rounded bg-muted px-1.5 text-[11px] font-medium text-muted-foreground">{PROVIDER[m.backend]}</span>
                      <span className="ml-auto pl-4 text-xs text-muted-foreground tabular">${m.in_usd} / ${m.out_usd} per M</span>
                    </span>
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>
          <div className="flex gap-2 pt-2">
            <Button type="submit" disabled={!name.trim() || create.isPending}>Create</Button>
            <Button type="button" variant="ghost" onClick={() => navigate("/agents")}>Cancel</Button>
          </div>
        </section>
      </form>
    </Page>
  )
}
