import { matchPath } from "react-router"

export type Crumb = { label: string; to?: string }
/** `active` overrides the route match, for a page that sits under a tab without being its route. */
export type Tab = { label: React.ReactNode; to: string; end?: boolean; active?: boolean }

/** A tabbed page reads `Module / Tab`: a lone crumb gains the active tab's label, unless it is the same word. */
export function crumbsWithTab(crumbs: Crumb[], tabs: Tab[] | undefined, pathname: string): Crumb[] {
  if (crumbs.length !== 1 || !tabs) return crumbs
  const active = tabs.find((t) => matchPath({ path: t.to.split("?")[0]!, end: !!t.end }, pathname))
  return typeof active?.label === "string" && active.label !== crumbs[0]!.label ? [crumbs[0]!, { label: active.label }] : crumbs
}
