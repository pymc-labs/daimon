import { Avatar } from "@/components/common"
import { cn } from "@/lib/utils"
import { Body, Card, Files, nameOf, PinButton, Reactions, type Skin } from "./parts"
import type { Msg } from "./timeline"

const time = (iso?: string) => (iso ? new Date(iso).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }) : "")

/** Flat rows with a round avatar; cards carry the accent bar of an embed. */
export function Discord({ messages, skin }: { messages: Msg[]; skin: Skin }) {
  return (
    <div className="divide-y divide-transparent">
      {messages.map((m) => (
        <div key={m.id} className="group/msg flex gap-3 px-3 py-1.5 hover:bg-muted/50">
          <Avatar name={nameOf(skin, m.author)} className="mt-0.5 size-8 text-[11px]" />
          <div className="min-w-0 flex-1">
            <div className="flex items-baseline gap-2">
              <span className="text-sm font-medium">{nameOf(skin, m.author)}</span>
              <span className="text-[11px] text-faint">{time(m.at)}</span>
              <span className="ml-auto"><PinButton pin={skin.pin} id={m.id} quote={m.text?.slice(0, 80)} /></span>
            </div>
            <div className={cn(m.notice && "italic text-muted-foreground")}><Body m={m} /></div>
            {m.cards.map((c) => <Card key={c.id} card={c} skin={skin} className="rounded-md border border-l-4 border-l-discord bg-muted" />)}
            <Files m={m} />
            <Reactions m={m} />
          </div>
        </div>
      ))}
    </div>
  )
}
