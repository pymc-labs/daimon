import { useQuery } from "@tanstack/react-query"
import { Drawer } from "@/components/Drawer"
import { Empty, FilterBar, SearchInput } from "@/components/filters"
import { AgentLink } from "@/components/links"
import { Facts, PanelHead, PanelSection } from "@/components/panel"
import { QueryState } from "@/components/QueryState"
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table"
import { api } from "@/lib/api"
import { rowProps, useParam } from "@/lib/filters"
import { plural } from "@/lib/format"
import { Pill } from "../agents/bits"
import type { Mcp as Data, McpExpiry, McpServer } from "./types"

const expiredOf = (expiry: McpExpiry | undefined, s: McpServer) => expiry?.servers.find((e) => e.id === s.id)

export function Mcp() {
  const q = useQuery({ queryKey: ["integrations", "mcp"], queryFn: () => api.get<Data>("/api/integrations/mcp") })
  // Slower than the list, so it loads after it and never blocks it.
  const expiry = useQuery({
    queryKey: ["integrations", "mcp", "expiry"], queryFn: () => api.get<McpExpiry>("/api/integrations/mcp/expiry"),
    enabled: !!q.data?.servers.length, retry: false, staleTime: 60_000,
  })
  const loading = expiry.isPending && expiry.fetchStatus !== "idle"
  return (
    <QueryState query={q} empty="No agent uses a tool server" isEmpty={(d) => !d.servers.length}>
      {(d) => <Servers servers={d.servers} expiry={expiry.data} loading={loading} />}
    </QueryState>
  )
}

function Servers({ servers, expiry, loading }: { servers: McpServer[]; expiry: McpExpiry | undefined; loading: boolean }) {
  const [search, setSearch] = useParam("q")
  const [, setServer] = useParam("server")
  const needle = (search ?? "").toLowerCase()
  const shown = servers.filter((s) => !needle || `${s.name} ${s.url} ${s.agents.map((a) => a.name).join(" ")}`.toLowerCase().includes(needle))
  return (
    <>
      <FilterBar className="px-0 pt-0">
        <SearchInput value={search} onChange={(v) => setSearch(v || null)} className="w-48" />
        <span className="ml-auto text-xs text-muted-foreground tabular">{shown.length} of {servers.length}</span>
      </FilterBar>
      {!shown.length ? <Empty>Nothing matches</Empty> : (
        <Table>
          <TableHeader>
            <TableRow className="hover:bg-transparent">
              <TableHead>Server</TableHead><TableHead className="max-md:hidden">Agents</TableHead><TableHead className="text-right">Signed in</TableHead>
              <TableHead className="w-40 max-sm:hidden">Expired</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {shown.map((s) => (
              <TableRow key={s.id} {...rowProps(() => setServer(s.id))}>
                <TableCell className="align-top">
                  <div className="font-medium">{s.name}</div>
                  <div className="max-w-44 truncate sm:max-w-[320px] font-mono text-[12px] text-muted-foreground">{s.url}</div>
                </TableCell>
                <TableCell className="max-md:hidden">
                  <span className="flex flex-wrap gap-1.5">
                    {s.agents.map((a) => (
                      <Pill key={a.id} className="gap-1.5"><AgentLink id={a.id} name={a.name} />{a.shared && <span className="text-faint">shared sign-in</span>}</Pill>
                    ))}
                  </span>
                </TableCell>
                <TableCell className="text-right tabular">{s.signed_in}</TableCell>
                <TableCell className="max-sm:hidden"><Expired s={s} expiry={expiry} loading={loading} /></TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
      )}
      <Drawer param="server" title="Tool server" ids={servers.map((s) => s.id)}>
        {(id) => {
          const s = servers.find((x) => x.id === id)
          return s ? <Detail s={s} expiry={expiry} loading={loading} /> : null
        }}
      </Drawer>
    </>
  )
}

function Expired({ s, expiry, loading }: { s: McpServer; expiry: McpExpiry | undefined; loading: boolean }) {
  const lapsed = expiredOf(expiry, s)
  if (lapsed && lapsed.expired > 0) return <span className="text-destructive" title={lapsed.people.join(", ")}>{lapsed.expired} expired</span>
  if (loading) return <span className="text-faint">Checking</span>
  return expiry ? <span className="text-muted-foreground">{expiry.complete ? "None" : "None found so far"}</span> : null
}

function Detail({ s, expiry, loading }: { s: McpServer; expiry: McpExpiry | undefined; loading: boolean }) {
  const lapsed = expiredOf(expiry, s)
  return (
    <div>
      <PanelHead title={s.name} subtitle={<span className="font-mono text-xs">{s.url}</span>} />
      <PanelSection>
        <Facts rows={[
          ["Signed in", plural(s.signed_in, "person", "people")],
          ["Expired", <Expired key="expired" s={s} expiry={expiry} loading={loading} />],
          ["Who", lapsed && lapsed.expired > 0 ? lapsed.people.join(", ") : null],
        ]} />
      </PanelSection>
      <PanelSection title="Agents">
        <ul className="space-y-2 text-sm">
          {s.agents.map((a) => (
            <li key={a.id} className="flex items-center gap-2">
              <AgentLink id={a.id} name={a.name} />
              <span className="ml-auto text-xs text-muted-foreground">{a.shared ? "Shared sign-in" : `${a.signed_in} signed in`}</span>
            </li>
          ))}
        </ul>
      </PanelSection>
    </div>
  )
}
