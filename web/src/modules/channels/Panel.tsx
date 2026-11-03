import { ExternalLink } from "lucide-react"

import { Ago, Money } from "@/components/common"
import { QueryState } from "@/components/QueryState"
import { shortDate } from "@/lib/format"
import { Facts, Tag } from "@/modules/admin/shared"
import { Access } from "./Access"
import { Answers } from "./Answers"
import { useChannel, useChannels, useChannelSettings } from "./api"
import { Budget } from "./Budget"
import { History } from "./History"
import { Model } from "./Model"
import { nameOf, platformLabel, SUBSCRIPTION, TAGS } from "./lib"
import { RoutinesHere } from "./RoutinesHere"
import { Sandbox } from "./Sandbox"
import { PlatformIcon } from "./shared"
import { Skills } from "./Skills"
import { Switches } from "./Switches"
import { Threads } from "./Threads"
import { ToolRules } from "./ToolRules"
import type { ChannelDetail, ChannelSettings } from "./types"

function subscriptionText(s: NonNullable<ChannelSettings["teams_subscription"]>) {
  return s.status === "active" && s.expires_at ? `Active until ${shortDate(s.expires_at)}` : SUBSCRIPTION[s.status]
}

function Header({ c, s }: { c: ChannelDetail; s?: ChannelSettings }) {
  const sub = s?.teams_subscription
  return (
    <>
      <div className="flex items-center gap-3 px-5 py-5">
        <span className="flex size-10 shrink-0 items-center justify-center rounded-full bg-accent"><PlatformIcon platform={c.platform} /></span>
        <div className="min-w-0 flex-1">
          <div className="truncate text-base font-semibold">{nameOf(c)}</div>
          <div className="flex flex-wrap items-center gap-1.5 text-sm text-muted-foreground">
            <span>{[platformLabel(c.platform), c.parent?.name].filter(Boolean).join(", ")}</span>
            {c.tags.map((t) => <Tag key={t}>{TAGS[t]}</Tag>)}
          </div>
        </div>
        {s?.link && (
          <a
            href={s.link} target="_blank" rel="noreferrer"
            className="inline-flex h-7 shrink-0 items-center gap-1.5 rounded-md border px-2 text-xs hover:bg-accent"
          >Open in {platformLabel(c.platform)}<ExternalLink className="size-3" /></a>
        )}
      </div>
      <section className="border-t px-5 py-4">
        <Facts rows={[
          ["Last active", <Ago key="a" iso={c.last_active_at} />],
          ["This month", <Money key="m" usd={c.spend.month_usd} />],
          ["Subscription", sub && (
            <span key="s" className="flex flex-wrap items-center gap-1.5">
              <span className={sub.status === "active" ? undefined : "text-destructive"}>{subscriptionText(sub)}</span>
            </span>
          )],
        ]} />
      </section>
    </>
  )
}

export function ChannelPanel({ id }: { id: string }) {
  const q = useChannel(id)
  const settings = useChannelSettings(id)
  const { data: list } = useChannels()
  const s = settings.data
  return (
    <QueryState query={q} className="px-5 py-6">{(c: ChannelDetail) => (
      <div>
        <Header c={c} s={s} />
        <Answers c={c} workspace={list?.default_agent} settings={s} />
        {c.platform !== "web" && <Access c={c} />}
        <Budget c={c} spend={s?.spend} />
        {c.platform !== "web" && s && (
          <>
            <Switches c={c} s={s} />
            <Sandbox key={`${c.environment.source}:${c.environment.value}`} c={c} />
            <Skills c={c} s={s} />
            <ToolRules c={c} s={s} />
            <RoutinesHere s={s} />
            <Model c={c} s={s} />
            <Threads c={c} s={s} />
          </>
        )}
        {c.can_manage && <History c={c} />}
      </div>
    )}</QueryState>
  )
}
