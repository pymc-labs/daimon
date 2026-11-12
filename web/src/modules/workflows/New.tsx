import { FileCode, LayoutTemplate, MessageSquareText, SquareDashed, type LucideIcon } from "lucide-react"
import { useState } from "react"
import { useNavigate } from "react-router"

import { Page } from "@/components/shell/Page"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import { Skeleton } from "@/components/ui/skeleton"
import { Textarea } from "@/components/ui/textarea"
import { useCan } from "@/lib/hooks"
import { cn } from "@/lib/utils"
import { NotFound } from "@/pages/Status"
import { useCreate, useTemplates } from "./api"
import type { AskMessage, NewWorkflow as Body } from "./types"
import { KIND, TRIGGER } from "./kinds"

type From = Body["from"]
const FROM: { id: From; icon: LucideIcon; label: string; fact: string }[] = [
  { id: "describe", icon: MessageSquareText, label: "Describe it", fact: "Daimon drafts the steps" },
  { id: "template", icon: LayoutTemplate, label: "Template", fact: "6 starting points" },
  { id: "blank", icon: SquareDashed, label: "Blank", fact: "Trigger and finish" },
  { id: "import", icon: FileCode, label: "Import", fact: "YAML spec" },
]
const card = (on: boolean) => cn(
  "flex flex-col items-start gap-3 rounded-xl border bg-card p-4 text-left transition-[border-color,box-shadow] hover:border-border-strong",
  on && "border-ring ring-3 ring-ring/15 hover:border-ring",
)

export function NewWorkflow() {
  const can = useCan()
  const navigate = useNavigate()
  const create = useCreate()
  const { data: templates } = useTemplates()
  const [from, setFrom] = useState<From>("describe")
  const [text, setText] = useState("")
  const [tpl, setTpl] = useState("daily_digest")
  const [name, setName] = useState("")
  const [yaml, setYaml] = useState("")
  if (!can("routines.manage")) return <NotFound />
  const body: Body | null = from === "describe" ? (text.trim() ? { from, text: text.trim() } : null)
    : from === "template" ? { from, template: tpl } : from === "blank" ? (name.trim() ? { from, name: name.trim() } : null) : (yaml.trim() ? { from, yaml } : null)
  const submit = () => body && create.mutate(body, {
    onSuccess: (w) => {
      const ask: AskMessage[] | undefined = w.reply && body.from === "describe" ? [{ role: "user", text: body.text }, { role: "assistant", text: w.reply }] : undefined
      navigate(`/workflows/${w.id}${ask ? "?ask=1" : ""}`, { state: ask ? { ask } : undefined })
    },
  })
  return (
    <Page crumbs={[{ label: "Workflows", to: "/workflows" }, { label: "New workflow" }]}>
      <form className="mx-auto max-w-3xl space-y-8 pb-10" onSubmit={(e) => { e.preventDefault(); submit() }}>
        <section className="space-y-3">
          <h2 className="text-sm font-medium">Start from</h2>
          <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
            {FROM.map((f) => (
              <button type="button" key={f.id} onClick={() => setFrom(f.id)} className={card(from === f.id)} aria-pressed={from === f.id}>
                <span className="flex size-8 items-center justify-center rounded-lg border bg-muted text-muted-foreground"><f.icon className="size-4" /></span>
                <span>
                  <span className="block text-sm font-medium">{f.label}</span>
                  <span className="block text-xs text-muted-foreground">{f.fact}</span>
                </span>
              </button>
            ))}
          </div>
        </section>

        {from === "describe" && (
          <section className="space-y-1.5">
            <Label htmlFor="wf-text">What should it do?</Label>
            <Textarea id="wf-text" autoFocus value={text} onChange={(e) => setText(e.target.value)} className="min-h-32 text-sm"
              placeholder="Every Monday, find deals over $50k with no activity in 14 days and post a digest to #sales" />
          </section>
        )}
        {from === "template" && (
          <section className="space-y-3">
            <h2 className="text-sm font-medium">Template</h2>
            <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
              {!templates ? Array.from({ length: 6 }, (_, i) => <Skeleton key={i} className="h-28 rounded-xl" />) : templates.map((t) => {
                const Trig = TRIGGER[t.trigger].icon
                return (
                  <button type="button" key={t.id} onClick={() => setTpl(t.id)} className={card(tpl === t.id)} aria-pressed={tpl === t.id}>
                    <span className="flex w-full items-center gap-2">
                      <span className="text-sm font-medium">{t.name}</span>
                      <span className="ml-auto flex items-center gap-1 text-[11px] text-muted-foreground"><Trig className="size-3.5" />{TRIGGER[t.trigger].label}</span>
                    </span>
                    <span className="flex flex-wrap items-center gap-1">
                      {t.kinds.map((k, i) => {
                        const K = KIND[k]
                        return (
                          <span key={i} className="flex items-center gap-1">
                            {i > 0 && <span className="h-px w-2 bg-border-strong" />}
                            <span className={cn("flex size-6 items-center justify-center rounded-md border bg-background", K.text)} title={K.label}><K.icon className="size-3.5" /></span>
                          </span>
                        )
                      })}
                    </span>
                    <span className="text-[11px] text-faint tabular">{t.kinds.length} steps</span>
                  </button>
                )
              })}
            </div>
          </section>
        )}
        {from === "blank" && (
          <section className="max-w-md space-y-1.5">
            <Label htmlFor="wf-name">Name</Label>
            <Input id="wf-name" autoFocus value={name} onChange={(e) => setName(e.target.value)} className="h-8" />
          </section>
        )}
        {from === "import" && (
          <section className="space-y-1.5">
            <Label htmlFor="wf-yaml">Spec</Label>
            <Textarea id="wf-yaml" autoFocus value={yaml} onChange={(e) => setYaml(e.target.value)} spellCheck={false}
              className="min-h-64 font-mono text-[12.5px] leading-[1.7]" placeholder={"schema: daimon.workflow/v1\nname: \nstart: \nnodes:"} />
          </section>
        )}

        <div className="space-y-2">
          <div className="flex justify-end gap-2 max-sm:[&>*]:flex-1">
            <Button type="button" variant="ghost" onClick={() => navigate("/workflows")}>Cancel</Button>
            <Button type="submit" disabled={!body || create.isPending}>{from === "describe" ? "Draft it" : "Create"}</Button>
          </div>
          {create.error && <p className="text-right text-sm text-destructive">{create.error.message}</p>}
        </div>
      </form>
    </Page>
  )
}
