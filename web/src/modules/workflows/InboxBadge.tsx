import { useInbox } from "./api"

/** Sidebar count of what waits on you. */
export function InboxBadge() {
  const n = useInbox().data?.filter((i) => !i.decision).length ?? 0
  if (!n) return null
  return <span className="ml-auto rounded-full bg-link/10 px-1.5 text-xs font-medium text-link tabular" aria-label={`${n} waiting`}>{n}</span>
}
