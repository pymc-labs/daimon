import { Avatar } from "@/components/common"
import { cn } from "@/lib/utils"
import { Body, Card, Files, nameOf, PinButton, Reactions, type Skin } from "./parts"
import type { Msg } from "./timeline"

const time = (iso?: string) => (iso ? new Date(iso).toLocaleString([], { hour: "numeric", minute: "2-digit" }) : "")

/** Message bubbles with the author above, adaptive-style cards inside. */
export function Teams({ messages, skin }: { messages: Msg[]; skin: Skin }) {
  return (
    <div className="space-y-2 px-3 py-2">
      {messages.map((m) => {
        const mine = m.author === skin.me
        return (
          <div key={m.id} className={cn("group/msg flex gap-2", mine && "flex-row-reverse")}>
            <Avatar name={nameOf(skin, m.author)} className="mt-1 size-7 text-[10px]" />
            <div className={cn("min-w-0 max-w-[85%] rounded-lg border px-3 py-2", mine ? "bg-teams/15" : "bg-card")}>
              <div className="flex items-baseline gap-2 text-xs"><span className="font-semibold">{nameOf(skin, m.author)}</span><span className="text-faint">{time(m.at)}</span><span className="ml-auto"><PinButton pin={skin.pin} id={m.id} quote={m.text?.slice(0, 80)} /></span></div>
              <div className={cn("mt-0.5", m.notice && "italic text-muted-foreground")}><Body m={m} /></div>
              {m.cards.map((c) => <Card key={c.id} card={c} skin={skin} className="rounded-md border-t-4 border-t-teams bg-background shadow-sm" />)}
              <Files m={m} />
              <Reactions m={m} round />
            </div>
          </div>
        )
      })}
    </div>
  )
}
