import { Ago, Avatar } from "@/components/common"
import { LoadMore, QueryState } from "@/components/QueryState"
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table"
import { usePaged } from "@/lib/hooks"
import type { AuditEntry } from "./types"

// Audit

export function Audit() {
  const q = usePaged<AuditEntry>(["admin", "audit"], "/api/admin/audit")
  return <QueryState query={q} empty="No activity yet">{(data) => <><AuditTable data={data} /><LoadMore list={q} /></>}</QueryState>
}

function AuditTable({ data }: { data: AuditEntry[] }) {
  return (
    <Table>
      <TableHeader>
        <TableRow className="hover:bg-transparent">
          <TableHead className="w-28">When</TableHead><TableHead>Who</TableHead><TableHead>Action</TableHead><TableHead>Target</TableHead><TableHead className="max-md:hidden">Detail</TableHead>
        </TableRow>
      </TableHeader>
      <TableBody>
        {data.map((e) => (
          <TableRow key={e.id}>
            <TableCell className="text-muted-foreground"><Ago iso={e.at} /></TableCell>
            <TableCell><span className="flex items-center gap-2"><Avatar name={e.who} className="size-5 text-[9px]" />{e.who}</span></TableCell>
            <TableCell>{e.action}</TableCell>
            <TableCell className="font-medium">{e.target}</TableCell>
            <TableCell className="text-muted-foreground max-md:hidden">{e.detail ?? ""}</TableCell>
          </TableRow>
        ))}
      </TableBody>
    </Table>
  )
}
