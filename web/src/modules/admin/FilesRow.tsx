import { useMutation, useQueryClient } from "@tanstack/react-query"

import { SettingRow } from "@/components/panel"
import { Switch } from "@/components/ui/switch"
import { api } from "@/lib/api"

/** Whether the visitor behind an invite may attach files in chat. */
export function FilesRow({ invite, on }: { invite: string; on: boolean }) {
  const qc = useQueryClient()
  const set = useMutation({
    mutationFn: (allow: boolean) => api.patch(`/api/admin/invites/${invite}`, { allow_files: allow }),
    onSuccess: () => { void qc.invalidateQueries({ queryKey: ["admin"] }); void qc.invalidateQueries({ queryKey: ["me"] }) },
  })
  return (
    <div className="mt-1.5">
      <SettingRow label="Files"><Switch aria-label="Files" checked={on} disabled={set.isPending} onCheckedChange={(v) => set.mutate(v)} /></SettingRow>
    </div>
  )
}
