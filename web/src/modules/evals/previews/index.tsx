import { Discord } from "./Discord"
import { Slack } from "./Slack"
import { Teams } from "./Teams"
import { Web } from "./Web"
import type { Skin } from "./parts"
import type { Msg } from "./timeline"
import type { Platform } from "../types"

export { fromEvents, fromMessages } from "./timeline"
export type { Msg, Pin } from "./timeline"

const SKINS = { discord: Discord, slack: Slack, teams: Teams, web: Web } satisfies Record<Platform, React.ComponentType<{ messages: Msg[]; skin: Skin }>>
export const PLATFORMS: Record<Platform, string> = { discord: "Discord", slack: "Slack", teams: "Teams", web: "Web" }

/** Messages and cards as the chosen platform would draw them. */
export function ChatThread({ platform, messages, skin }: { platform: Platform; messages: Msg[]; skin: Skin }) {
  const Skin = SKINS[platform]
  return messages.length ? <div className="rounded-lg border bg-background py-1" data-platform={platform}><Skin messages={messages} skin={skin} /></div> : <div className="py-6 text-center text-sm text-faint">No messages</div>
}
