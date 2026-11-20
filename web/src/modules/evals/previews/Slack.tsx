import { cn } from "@/lib/utils"
import { Body, Card, Files, nameOf, PinButton, Reactions, type Skin } from "./parts"
import type { Msg } from "./timeline"

const time = (iso?: string) => (iso ? new Date(iso).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" }) : "")

/** Square avatars, bold names, block cards with outlined buttons and round reaction pills. */
export function Slack({ messages, skin }: { messages: Msg[]; skin: Skin }) {
  return (
    <div>
      {messages.map((m) => (
        <div key={m.id} className="group/msg flex gap-2.5 px-3 py-1.5 hover:bg-muted/50">
          <span className="mt-0.5 flex size-8 shrink-0 items-center justify-center rounded-md bg-slack text-xs font-semibold text-white">{nameOf(skin, m.author).slice(0, 1).toUpperCase()}</span>
          <div className="min-w-0 flex-1">
            <div className="flex items-baseline gap-2">
              <span className="text-sm font-bold">{nameOf(skin, m.author)}</span>
              <span className="text-[11px] text-faint">{time(m.at)}</span>
              <span className="ml-auto"><PinButton pin={skin.pin} id={m.id} quote={m.text?.slice(0, 80)} /></span>
            </div>
            <div className={cn(m.notice && "italic text-muted-foreground")}><Body m={m} /></div>
            {m.cards.map((c) => <Card key={c.id} card={c} skin={skin} className="rounded-lg border-l-[3px] border-y border-r border-l-border-strong bg-background" />)}
            <Files m={m} />
            <Reactions m={m} round />
          </div>
        </div>
      ))}
    </div>
  )
}
