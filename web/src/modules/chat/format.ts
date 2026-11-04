export const bytes = (n: number) => (n < 1024 ? `${n} B` : n < 1024 ** 2 ? `${(n / 1024).toFixed(1)} KB` : `${(n / 1024 ** 2).toFixed(1)} MB`)
export const secs = (ms: number) => (ms < 1000 ? `${ms} ms` : `${(ms / 1000).toFixed(1)} s`)

/** The server refuses a file above this size; checked before the upload starts. */
export const MAX_FILE_BYTES = 24_000_000

/** The in-app path of a link to a workspace page of this site (`/w/...`), else null.
 * Downloads and the rest of the site's paths stay ordinary links. */
export function inAppPath(href: string | undefined, origin: string): string | null {
  if (!href) return null
  try {
    const u = new URL(href, origin)
    return /^https?:$/.test(u.protocol) && u.origin === origin && u.pathname.startsWith("/w/") ? u.pathname + u.search + u.hash : null
  } catch { return null }
}
