import { Empty } from "@/components/filters"
import { Button } from "@/components/ui/button"
import { Skeleton } from "@/components/ui/skeleton"
import { ApiError } from "@/lib/api"
import { cn } from "@/lib/utils"

export const Rows = ({ n = 6, className }: { n?: number; className?: string }) => (
  <div className={cn("space-y-2", className)} role="status" aria-label="Loading">
    {Array.from({ length: n }, (_, i) => <Skeleton key={i} className="h-8" />)}
  </div>
)

/** What a failed query says: the server's refusal when it gave one, then a way to retry. */
export function ErrorState({ error, onRetry, className }: { error: unknown; onRetry: () => void; className?: string }) {
  const why = error instanceof ApiError && error.message ? error.message : null
  return (
    <div role="alert" className={cn("flex flex-col items-center gap-3 py-16 text-center", className)}>
      <div className="text-sm font-medium">Couldn't load</div>
      {why && <div className="text-sm text-muted-foreground">{why}</div>}
      <Button variant="outline" size="sm" onClick={onRetry}>Retry</Button>
    </div>
  )
}

/** One place for loading, error and empty: a skeleton, then "Couldn't load" with Retry, then the empty text. */
export function QueryState<T>({ query, empty, isEmpty, skeleton, className, children }: {
  query: { data: T | undefined; error: unknown; isError: boolean; refetch: () => unknown }
  empty?: React.ReactNode
  isEmpty?: (data: T) => boolean
  skeleton?: React.ReactNode
  className?: string
  children: (data: T) => React.ReactNode
}) {
  const { data } = query
  if (data === undefined) {
    return query.isError ? <ErrorState error={query.error} onRetry={() => void query.refetch()} className={className} /> : <>{skeleton ?? <Rows className={className} />}</>
  }
  if (empty !== undefined && (isEmpty ? isEmpty(data) : Array.isArray(data) && !data.length)) return <Empty className={className}>{empty}</Empty>
  return <>{children(data)}</>
}

/** The end of a paged list: a button for the next page while the server has more. */
export function LoadMore({ list, className }: { list: { hasMore: boolean; loadingMore: boolean; loadMore: () => unknown }; className?: string }) {
  if (!list.hasMore) return null
  return (
    <div className={cn("flex justify-center py-3", className)}>
      <Button variant="outline" size="sm" disabled={list.loadingMore} onClick={() => void list.loadMore()}>{list.loadingMore ? "Loading" : "Load more"}</Button>
    </div>
  )
}
