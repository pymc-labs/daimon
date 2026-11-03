import { Archive, Copy, MoreHorizontal } from "lucide-react"
import { useState } from "react"
import { useNavigate } from "react-router"
import { toast } from "sonner"

import { Button } from "@/components/ui/button"
import { Dialog, DialogContent, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog"
import { DropdownMenu, DropdownMenuContent, DropdownMenuItem, DropdownMenuTrigger } from "@/components/ui/dropdown-menu"
import { Input } from "@/components/ui/input"
import { useConfirm } from "@/lib/confirm"
import { useMe } from "@/lib/hooks"
import type { Agent } from "../types"
import { useAgentLifecycle } from "./api"

/** Copy or archive an agent. Admins only; archiving asks first. */
export function LifecycleMenu({ agent }: { agent: Agent }) {
  const { data: me } = useMe()
  const confirm = useConfirm()
  const navigate = useNavigate()
  const { archive, copy } = useAgentLifecycle(agent.id)
  const [naming, setNaming] = useState(false)
  const [name, setName] = useState("")
  if (me?.role !== "owner" && me?.role !== "admin") return null

  const retire = async () => {
    if (!(await confirm({ title: `Archive ${agent.name}?`, action: "Archive", destructive: true }))) return
    archive.mutate(undefined, { onSuccess: () => { navigate("/agents"); toast(`Archived ${agent.name}`) } })
  }
  const make = () => {
    if (!name.trim()) return
    copy.mutate(name.trim(), { onSuccess: (a) => { setNaming(false); navigate(`/agents/${a.id}`); toast(`Created ${a.name}`) } })
  }

  return (
    <>
      <DropdownMenu>
        <DropdownMenuTrigger asChild><Button variant="outline" size="icon-sm" aria-label="More"><MoreHorizontal /></Button></DropdownMenuTrigger>
        <DropdownMenuContent align="end" className="w-40">
          <DropdownMenuItem onSelect={() => { setName(`${agent.name} copy`); setNaming(true) }}><Copy />Copy</DropdownMenuItem>
          <DropdownMenuItem variant="destructive" onSelect={() => void retire()}><Archive />Archive</DropdownMenuItem>
        </DropdownMenuContent>
      </DropdownMenu>
      <Dialog open={naming} onOpenChange={setNaming}>
        <DialogContent className="sm:max-w-sm">
          <DialogHeader><DialogTitle>Copy {agent.name}</DialogTitle></DialogHeader>
          <form onSubmit={(e) => { e.preventDefault(); make() }}>
            <Input value={name} onChange={(e) => setName(e.target.value)} aria-label="Name" className="h-8" autoFocus />
            <DialogFooter className="mt-4">
              <Button type="button" variant="ghost" onClick={() => setNaming(false)}>Cancel</Button>
              <Button type="submit" disabled={!name.trim() || copy.isPending}>Copy</Button>
            </DialogFooter>
          </form>
        </DialogContent>
      </Dialog>
    </>
  )
}
