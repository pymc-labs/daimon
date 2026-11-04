import type { Capability, ModuleKey } from "@/lib/types"
import table from "./deep-links.json"

/** A page the bots link to. The table is shared with the bots' link builder. */
export type DeepLink = { path: string; param: string | null; module: ModuleKey; cap: Capability }
export type LinkKey = keyof typeof table

export const DEEP_LINKS = table as Record<LinkKey, DeepLink>

const q = encodeURIComponent

/** The in-app address of a page: `segment` fills `{id}` in the path, `value` is the opened item or tab. */
export function deepLink(key: LinkKey, segment?: string | null, value?: string | null): string {
  const { path, param } = DEEP_LINKS[key]
  const to = segment != null ? path.replace("{id}", q(segment)) : path
  return param && value ? `${to}?${param}=${q(value)}` : to
}
