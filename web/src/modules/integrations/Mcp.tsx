import { useQuery } from "@tanstack/react-query"
import { QueryState } from "@/components/QueryState"
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table"
import { api } from "@/lib/api"
import { Pill } from "../agents/bits"
import type { Mcp as Data, McpExpiry, McpServer } from "./types"

export function Mcp() {
  const q = useQuery({ queryKey: ["integrations", "mcp"], queryFn: () => api.get<Data>("/api/integrations/mcp") })
  // Slower than the list, so it loads after it and never blocks it.
  const expiry = useQuery({
    queryKey: ["integrations", "mcp", "expiry"], queryFn: () => api.get<McpExpiry>("/api/integrations/mcp/expiry"),
    enabled: !!q.data?.servers.length, retry: false, staleTime: 60_000,
  })
  return (
    <QueryState query={q} empty="No agent uses a tool server" isEmpty={(d) => !d.servers.length}>
      {(d) => (
        <Table>
          <TableHeader>
            <TableRow className="hover:bg-transparent">
              <TableHead>Server</TableHead><TableHead className="max-md:hidden">Agents</TableHead><TableHead className="text-right">Signed in</TableHead>
              <TableHead className="w-40 max-sm:hidden">Expired</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>{d.servers.map((s) => <Server key={s.id} s={s} expiry={expiry.data} loading={expiry.isPending && expiry.fetchStatus !== "idle"} />)}</TableBody>
        </Table>
      )}
    </QueryState>
  )
}

function Server({ s, expiry, loading }: { s: McpServer; expiry: McpExpiry | undefined; loading: boolean }) {
  const lapsed = expiry?.servers.find((e) => e.id === s.id)
  return (
    <TableRow>
      <TableCell className="align-top">
        <div className="font-medium">{s.name}</div>
        <div className="max-w-44 truncate sm:max-w-[320px] font-mono text-[12px] text-muted-foreground">{s.url}</div>
      </TableCell>
      <TableCell className="max-md:hidden">
        <span className="flex flex-wrap gap-1.5">
          {s.agents.map((a) => <Pill key={a.id} className="gap-1.5">{a.name}{a.shared && <span className="text-faint">shared sign-in</span>}</Pill>)}
        </span>
      </TableCell>
      <TableCell className="text-right tabular">{s.signed_in}</TableCell>
      <TableCell className="max-sm:hidden">
        {lapsed && lapsed.expired > 0 ? (
          <span className="text-destructive" title={lapsed.people.join(", ")}>{lapsed.expired} expired</span>
        ) : loading ? <span className="text-faint">Checking</span>
          : expiry ? <span className="text-muted-foreground">{expiry.complete ? "None" : "None found so far"}</span> : null}
      </TableCell>
    </TableRow>
  )
}
