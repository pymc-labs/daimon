import { ChevronLeft } from "lucide-react"
import { Link, useParams } from "react-router"

import { Button } from "@/components/ui/button"
import { useChannels } from "@/modules/channels/api"
import { nameOf } from "@/modules/channels/lib"
import { FeatureList } from "./Settings"

/** One thread's own settings, headed by its name and the channel it is in. Who may change each is the server's rule; a refusal names who can help. */
export function ThreadSettings() {
  const { channel = "", thread = "" } = useParams()
  const { data } = useChannels()
  const find = (id: string) => data?.channels.find((c) => c.id === id)
  const parent = find(channel)
  const own = find(thread)
  return (
    <div className="mx-auto max-w-3xl">
      <div className="flex min-w-0 items-center gap-2 px-4 pt-4 lg:px-6">
        <Button asChild variant="ghost" size="sm" className="min-w-0">
          <Link to={`/channels?channel=${encodeURIComponent(channel)}`}><ChevronLeft /><span className="truncate">{parent ? nameOf(parent) : "Channel"}</span></Link>
        </Button>
        <span className="text-faint">/</span>
        <h1 className="min-w-0 truncate text-sm font-semibold">{own ? nameOf(own) : "Thread"} settings</h1>
      </div>
      <FeatureList place={{ channel, thread }} tier="thread" edit />
    </div>
  )
}
