import { ChevronLeft } from "lucide-react"
import { Link, useParams } from "react-router"

import { Button } from "@/components/ui/button"
import { FeatureList } from "./Settings"

/** One thread's own settings. Who may change each is the server's rule; a refusal names who can help. */
export function ThreadSettings() {
  const { channel = "", thread = "" } = useParams()
  return (
    <div className="mx-auto max-w-3xl">
      <div className="flex items-center gap-2 px-4 pt-4 lg:px-6">
        <Button asChild variant="ghost" size="sm"><Link to={`/channels?channel=${encodeURIComponent(channel)}`}><ChevronLeft />Channel</Link></Button>
        <h1 className="text-sm font-semibold">Thread settings</h1>
      </div>
      <FeatureList place={{ channel, thread }} tier="thread" edit />
    </div>
  )
}
