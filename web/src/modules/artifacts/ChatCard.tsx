import { AppWindow, ExternalLink, Eye, X } from "lucide-react"
import { Link } from "react-router"

import { Tag } from "@/components/panel"
import { Button } from "@/components/ui/button"
import { useParam } from "@/lib/filters"
import type { Msg } from "../chat/types"
import { ACCESS, useArtifact } from "./bits"
import { Preview } from "./Preview"

/** The card under a reply that published a page. */
export function ArtifactCard({ a }: { a: NonNullable<Msg["artifacts"]>[number] }) {
  const [open, setOpen] = useParam("preview")
  return (
    <div className="mt-4 flex w-full max-w-md items-center gap-3 rounded-lg border p-3">
      <span className="grid size-9 shrink-0 place-items-center rounded-md bg-muted text-muted-foreground"><AppWindow className="size-4" /></span>
      <span className="min-w-0 flex-1">
        <span className="block truncate text-sm font-medium">{a.title}</span>
        <span className="mt-0.5 flex gap-1"><Tag>Version {a.version}</Tag><Tag>{ACCESS[a.access].label}</Tag></span>
      </span>
      <Button variant="outline" size="sm" onClick={() => setOpen(open === a.id ? null : a.id)}><Eye />Preview</Button>
      <Button variant="ghost" size="icon-sm" aria-label="Open in new tab" asChild><a href={a.url} target="_blank" rel="noreferrer"><ExternalLink /></a></Button>
    </div>
  )
}

/** The chat view's right half while a page is previewed. */
export function ArtifactSplit() {
  const [id, setId] = useParam("preview")
  const { data: a } = useArtifact(id)
  if (!id || !a) return null
  return (
    <aside className="absolute inset-0 z-10 flex min-w-0 border-l bg-background lg:static lg:w-[48%] lg:max-w-[780px] lg:min-w-[420px] lg:shrink-0">
      <Preview
        artifact={a} title
        actions={
          <>
            <Button variant="outline" size="sm" asChild><Link to={`/artifacts/${a.id}`}>Manage</Link></Button>
            <Button variant="ghost" size="icon-sm" aria-label="Close preview" onClick={() => setId(null)}><X /></Button>
          </>
        }
      />
    </aside>
  )
}
