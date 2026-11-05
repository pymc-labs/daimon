import { ExternalLink } from "lucide-react"

import { Ago, Money } from "@/components/common"
import { AgentLink } from "@/components/links"
import { DrawerTabs, Facts, PanelHead, PanelSection, Tag } from "@/components/panel"
import { QueryState } from "@/components/QueryState"
import { Button } from "@/components/ui/button"
import { shortDate, usd } from "@/lib/format"
import { Access } from "./Access"
import { Answers } from "./Answers"
import { useChannel, useChannels, useChannelSettings } from "./api"
import { Budget } from "./Budget"
import { History } from "./History"
import { agentOf, nameOf, platformLabel, SUBSCRIPTION, TAGS } from "./lib"
import { Model } from "./Model"
import { RoutinesHere } from "./RoutinesHere"
import { Sandbox } from "./Sandbox"
import { PlatformIcon } from "./shared"
import { Skills } from "./Skills"
import { SpendBreakdown } from "./Spend"
import { Switches } from "./Switches"
import { Threads } from "./Threads"
import { ToolRules } from "./ToolRules"
import type { ChannelDetail, ChannelSettings } from "./types"

function subscriptionText(s: NonNullable<ChannelSettings["teams_subscription"]>) {
  return s.status === "active" && s.expires_at ? `Active until ${shortDate(s.expires_at)}` : SUBSCRIPTION[s.status]
}

/** The path above a channel, as `Ops team / ops-room`; a top-level channel reads as its platform. */
const pathText = (c: ChannelDetail) => (c.path.length ? c.path.map((n) => n.name ?? "Unnamed").join(" / ") : c.platform === "web" ? undefined : platformLabel(c.platform))

function Head({ c, s }: { c: ChannelDetail; s?: ChannelSettings }) {
  const platform = platformLabel(c.platform)
  return (
    <PanelHead
      icon={<PlatformIcon platform={c.platform} />} title={nameOf(c)} subtitle={pathText(c)}
      tags={c.tags.map((t) => <Tag key={t}>{TAGS[t]}</Tag>)}
      actions={s?.link && (
        <Button asChild variant="ghost" size="icon-sm">
          <a href={s.link} target="_blank" rel="noreferrer" aria-label={`Open in ${platform}`} title={`Open in ${platform}`}><ExternalLink /></a>
        </Button>
      )}
    />
  )
}

function FactsBlock({ c, s }: { c: ChannelDetail; s?: ChannelSettings }) {
  const sub = s?.teams_subscription
  const who = agentOf(c.agents, c.agent.value)
  return (
    <PanelSection>
      <Facts rows={[
        ["Answers", who && <AgentLink key="w" id={who.id} name={who.name} avatar={who.avatar} />],
        ["Last active", <Ago key="a" iso={c.last_active_at} />],
        ["Last 30 days", c.spend && <Money key="m" usd={c.spend.last_30_days_usd} />],
        ["Subscription", sub && <span key="s" className={sub.status === "active" ? undefined : "text-destructive"}>{subscriptionText(sub)}</span>],
      ]} />
    </PanelSection>
  )
}

function SpendTab({ c, s }: { c: ChannelDetail; s?: ChannelSettings }) {
  const spent = s?.spend?.by_day.some((d) => d.usd > 0)
  return (
    <PanelSection className="border-t-0">
      {c.spend && <Facts rows={[["Last 30 days", usd(c.spend.last_30_days_usd)]]} />}
      {s?.spend && <div className="mt-4"><SpendBreakdown spend={s.spend} /></div>}
      {s && !spent && <div className="mt-3 text-sm text-muted-foreground">No spend yet</div>}
    </PanelSection>
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
        <Head c={c} s={s} />
        <FactsBlock c={c} s={s} />
        <DrawerTabs tabs={[
          {
            value: "settings", label: "Settings",
            children: (
              <>
                <Answers c={c} workspace={list?.default_agent} settings={s} />
                {c.platform !== "web" && <Access c={c} />}
                <Budget c={c} />
                {c.platform !== "web" && s && (
                  <>
                    <Switches c={c} s={s} />
                    <Sandbox c={c} workspace={list?.default_environment} />
                    <Model c={c} s={s} />
                    <Skills c={c} s={s} />
                    <ToolRules c={c} s={s} />
                    <RoutinesHere s={s} />
                    <Threads c={c} s={s} />
                  </>
                )}
              </>
            ),
          },
          { value: "spend", label: "Spend", hidden: !c.spend, children: <SpendTab c={c} s={s} /> },
          { value: "history", label: "History", hidden: !c.can_manage, children: <History c={c} /> },
        ]} />
      </div>
    )}</QueryState>
  )
}
