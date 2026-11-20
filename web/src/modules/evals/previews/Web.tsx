import { cn } from "@/lib/utils"
import { Body, Card, Files, PinButton, Reactions, type Skin } from "./parts"
import type { Msg } from "./timeline"

/** The web chat: the requester in a muted bubble on the right, the agent as plain text. */
export function Web({ messages, skin }: { messages: Msg[]; skin: Skin }) {
  return (
    <div className="space-y-3 px-3 py-2">
      {messages.map((m) => {
        const mine = m.author === skin.me
        return (
          <div key={m.id} className={cn("group/msg flex", mine && "justify-end")}>
            <div className={cn("relative min-w-0 max-w-[85%]", mine && "rounded-2xl bg-muted px-3 py-2")}>
              <div className={cn(m.notice && "italic text-muted-foreground")}><Body m={m} /></div>
              {m.cards.map((c) => <Card key={c.id} card={c} skin={skin} className="rounded-xl border bg-card" />)}
              <Files m={m} />
              <Reactions m={m} round />
              <span className="absolute -top-3 right-0"><PinButton pin={skin.pin} id={m.id} quote={m.text?.slice(0, 80)} /></span>
            </div>
          </div>
        )
      })}
    </div>
  )
}
