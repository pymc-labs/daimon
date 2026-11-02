import { Ago } from "@/components/common"
import { QueryState } from "@/components/QueryState"
import { Facts, Tag } from "@/modules/admin/shared"
import { Access } from "./Access"
import { Answers } from "./Answers"
import { useChannel, useChannels } from "./api"
import { Budget } from "./Budget"
import { History } from "./History"
import { nameOf, platformLabel, TAGS } from "./lib"
import { PlatformIcon } from "./shared"
import type { ChannelDetail } from "./types"

export function ChannelPanel({ id }: { id: string }) {
  const q = useChannel(id)
  const { data: list } = useChannels()
  return (
    <QueryState query={q} className="px-5 py-6">{(c: ChannelDetail) => (
      <div>
        <div className="flex items-center gap-3 px-5 py-5">
          <span className="flex size-10 shrink-0 items-center justify-center rounded-full bg-accent"><PlatformIcon platform={c.platform} /></span>
          <div className="min-w-0">
            <div className="truncate text-base font-semibold">{nameOf(c)}</div>
            <div className="flex flex-wrap items-center gap-1.5 text-sm text-muted-foreground">
              <span>{[platformLabel(c.platform), c.parent?.name].filter(Boolean).join(", ")}</span>
              {c.tags.map((t) => <Tag key={t}>{TAGS[t]}</Tag>)}
            </div>
          </div>
        </div>
        <section className="border-t px-5 py-4">
          <Facts rows={[["Last active", <Ago key="a" iso={c.last_active_at} />]]} />
        </section>
        <Answers c={c} workspace={list?.default_agent} />
        {c.platform !== "web" && <Access c={c} />}
        <Budget c={c} />
        {c.can_manage && <History c={c} />}
      </div>
    )}</QueryState>
  )
}
