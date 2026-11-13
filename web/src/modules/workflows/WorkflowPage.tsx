import { ReactFlowProvider } from "@xyflow/react"
import { Copy, Download, Pause, Play, Trash2 } from "lucide-react"
import { useState } from "react"
import { Route, Routes, useNavigate, useParams } from "react-router"
import { toast } from "sonner"

import { PanelMenu } from "@/components/panel"
import { ErrorState } from "@/components/QueryState"
import { Page } from "@/components/shell/Page"
import { Button } from "@/components/ui/button"
import { ApiError } from "@/lib/api"
import { useConfirm } from "@/lib/confirm"
import { NotFound } from "@/pages/Status"
import { useCreate, useDelete, useDraft, usePatch, useWorkflow } from "./api"
import { Design } from "./Design"
import { MigrationStrip, PublishStrip } from "./PublishStrip"
import { RunDialog } from "./RunDialog"
import { RunsTab } from "./run/RunsTable"
import { AccessTab } from "./Access"
import { TriggersTab } from "./Triggers"
import { StatusBadge } from "./ui"
import { VersionsTab, VersionView } from "./Versions"
import { toYaml } from "./yaml"

const ICON_ON_PHONE = "max-sm:w-7 max-sm:px-0"

export function WorkflowPage() {
  const { id = "", "*": rest = "" } = useParams()
  const query = useWorkflow(id)
  const { data: wf, error } = query
  const { data: draft } = useDraft(id, wf?.can_edit === true)
  const patch = usePatch(id)
  const create = useCreate()
  const del = useDelete()
  const confirm = useConfirm()
  const navigate = useNavigate()
  const [run, setRun] = useState(false)
  if (error instanceof ApiError && error.status === 404) return <NotFound />
  const crumbs = [{ label: "Workflows", to: "/workflows" }, { label: draft?.spec.name ?? wf?.name ?? "" }]
  if (!wf) return <Page crumbs={crumbs}>{query.isError && <ErrorState error={error} onRetry={() => void query.refetch()} />}</Page>
  const b = `/workflows/${id}`
  const design = rest === "" || /^versions\/\d+$/.test(rest)
  const exportYaml = () => {
    const a = document.createElement("a")
    a.href = URL.createObjectURL(new Blob([toYaml(wf.spec)], { type: "text/yaml" }))
    a.download = `${wf.name.toLowerCase().replace(/\W+/g, "-")}.yaml`
    a.click()
    URL.revokeObjectURL(a.href)
  }
  const remove = async () => {
    if (!(await confirm({ title: `Delete ${wf.name}?`, action: "Delete", destructive: true }))) return
    del.mutate(id, { onSuccess: () => { toast(`Deleted ${wf.name}`); navigate("/workflows") } })
  }
  const paused = wf.status === "paused"
  return (
    <Page
      crumbs={crumbs}
      flush
      className={design ? "relative flex flex-col overflow-hidden bg-muted/40" : undefined}
      tabs={[
        { label: "Design", to: b, end: true }, { label: "Runs", to: `${b}/runs` }, { label: "Triggers", to: `${b}/triggers` },
        { label: "Access", to: `${b}/access` }, { label: "Versions", to: `${b}/versions` },
      ]}
      actions={(
        <>
          <StatusBadge status={wf.status} className="max-sm:hidden" />
          {wf.live_version != null && <span className="inline-flex h-5 items-center rounded-md border px-1.5 text-xs text-muted-foreground tabular max-sm:hidden">v{wf.live_version}</span>}
          {wf.can_edit && wf.live_version != null && (
            <Button variant="outline" size="sm" className={ICON_ON_PHONE} disabled={patch.isPending} onClick={() => patch.mutate({ paused: !paused })}>
              {paused ? <Play /> : <Pause />}<span className="max-sm:sr-only">{paused ? "Resume" : "Pause"}</span>
            </Button>
          )}
          <PanelMenu items={[
            { label: "Export YAML", icon: <Download />, onSelect: exportYaml },
            ...(wf.can_edit ? [
              { label: "Duplicate", icon: <Copy />, onSelect: () => create.mutate({ from: "import", yaml: toYaml({ ...wf.spec, name: `${wf.name} copy` }) }, { onSuccess: (w) => navigate(`/workflows/${w.id}`) }) },
              { label: "Delete", icon: <Trash2 />, destructive: true, onSelect: () => void remove() },
            ] : []),
          ]} />
          {wf.can_run && <Button size="sm" disabled={wf.live_version == null || paused} onClick={() => setRun(true)} className={ICON_ON_PHONE}><Play /><span className="max-sm:sr-only">Run</span></Button>}
        </>
      )}
    >
      <PublishStrip wf={wf} />
      {design && rest === "" && <MigrationStrip wf={wf} />}
      <Routes>
        <Route index element={<ReactFlowProvider><Design wf={wf} /></ReactFlowProvider>} />
        <Route path="runs" element={<RunsTab wf={wf} />} />
        <Route path="triggers" element={<TriggersTab wf={wf} />} />
        <Route path="access" element={<AccessTab wf={wf} />} />
        <Route path="versions" element={<VersionsTab wf={wf} />} />
        <Route path="versions/:n" element={<ReactFlowProvider><VersionView wf={wf} /></ReactFlowProvider>} />
        <Route path="*" element={<NotFound />} />
      </Routes>
      <RunDialog id={id} spec={wf.spec} open={run} onOpenChange={setRun} />
    </Page>
  )
}
