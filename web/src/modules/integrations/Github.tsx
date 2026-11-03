import { useQuery } from "@tanstack/react-query"
import { Star } from "lucide-react"
import { Ago, StatStrip, StatusDot } from "@/components/common"
import { Empty } from "@/components/filters"
import { QueryState } from "@/components/QueryState"
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table"
import { api } from "@/lib/api"
import { plural } from "@/lib/format"
import { cn } from "@/lib/utils"
import { Pill } from "../agents/bits"
import { Section } from "./shared"
import type { Github as Data } from "./types"

const STATE = { active: "Connected", suspended: "Paused", removed: "Removed" } as const
const SELECTION: Record<string, string> = { all: "All repositories", selected: "Chosen repositories" }

export function Github() {
  const q = useQuery({ queryKey: ["integrations", "github"], queryFn: () => api.get<Data>("/api/integrations/github") })
  return <QueryState query={q}>{(d) => (d.configured ? <Body d={d} /> : <Empty>GitHub isn't set up on this server</Empty>)}</QueryState>
}

function Body({ d }: { d: Data }) {
  return (
    <>
      <StatStrip stats={[
        { label: "Installations", value: d.installations.length },
        { label: "Agents with repos", value: d.agents.length },
        { label: "Waiting for approval", value: d.requests.length },
        { label: "Sync problems", value: <span className={cn(d.trouble.length > 0 && "text-destructive")}>{d.trouble.length}</span> },
      ]} />
      {d.trouble.length > 0 && (
        <Section title="Sync problems" className="mt-8">
          <div className="divide-y rounded-lg border">
            {d.trouble.map((t) => (
              <div key={`${t.kind}:${t.subject}`} className="flex flex-wrap items-baseline gap-x-3 px-4 py-2.5 text-sm">
                <span className="font-medium">{t.subject}</span>
                <span className="flex-1 text-muted-foreground">{t.error}</span>
                <span className="text-xs text-muted-foreground">{plural(t.attempts, "try", "tries")}, <Ago iso={t.at} /></span>
              </div>
            ))}
          </div>
        </Section>
      )}
      <Section title="Installations">
        {d.installations.length ? (
          <div className="divide-y rounded-lg border">
            {d.installations.map((i) => (
              <div key={i.id} className="flex min-h-12 items-center gap-3 px-4 py-2 text-sm">
                <span className="min-w-0 flex-1">
                  <span className="font-medium">{i.account}</span>
                  <span className="ml-2 text-xs text-muted-foreground">{i.selection ? SELECTION[i.selection] ?? i.selection : ""}</span>
                </span>
                <span className="text-xs text-muted-foreground max-sm:hidden">{plural(i.repos, "repo")}</span>
                <span className="text-xs text-muted-foreground max-md:hidden">Checked <Ago iso={i.refreshed_at} /></span>
                <StatusDot status={i.state === "active" ? "healthy" : "failing"} label={STATE[i.state]} />
              </div>
            ))}
          </div>
        ) : <Empty>No repos are shared with agents</Empty>}
      </Section>
      {d.requests.length > 0 && (
        <Section title="Waiting for approval" aside="Approved in chat">
          <Table>
            <TableHeader>
              <TableRow className="hover:bg-transparent">
                <TableHead>Agent</TableHead><TableHead>Repos</TableHead><TableHead className="max-sm:hidden">Asked by</TableHead>
                <TableHead className="max-md:hidden">Expires</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {d.requests.map((r) => (
                <TableRow key={r.id}>
                  <TableCell className="font-medium">{r.agent}</TableCell>
                  <TableCell>
                    <span className="flex flex-wrap gap-1">
                      {r.repos.map((n) => <Pill key={n}>{n}</Pill>)}
                      <Pill>{r.access === "write" ? "Can change" : "Read only"}</Pill>
                      {r.waiting_github && <Pill>Waiting on GitHub</Pill>}
                    </span>
                  </TableCell>
                  <TableCell className="text-muted-foreground max-sm:hidden">{r.requester}</TableCell>
                  <TableCell className="text-muted-foreground max-md:hidden"><Ago iso={r.expires_at} /></TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </Section>
      )}
      <Section title="Repos per agent">
        {d.agents.length ? (
          <Table>
            <TableHeader><TableRow className="hover:bg-transparent"><TableHead className="w-56">Agent</TableHead><TableHead>Repos</TableHead></TableRow></TableHeader>
            <TableBody>
              {d.agents.map((a) => (
                <TableRow key={a.id}>
                  <TableCell className="align-top font-medium">{a.name}</TableCell>
                  <TableCell>
                    <span className="flex flex-wrap gap-1.5">
                      {a.repos.map((r) => (
                        <Pill key={r.name} className={cn("gap-1.5", r.status !== "active" && "text-destructive")}>
                          {r.working && <Star className="size-3 fill-current" aria-label="Working repo" />}
                          {r.name}
                          <span className="text-faint">{r.access === "write" ? "write" : "read"}</span>
                        </Pill>
                      ))}
                    </span>
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        ) : <Empty>No agent has a repo</Empty>}
      </Section>
    </>
  )
}
