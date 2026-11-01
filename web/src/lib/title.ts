/** Browser tab title: the most specific part first, then the wider ones, then the workspace. */
export function pageTitle(parts: (string | null | undefined)[], workspace?: string | null): string {
  const out: string[] = []
  for (const p of [...parts, workspace]) if (p && out[out.length - 1] !== p) out.push(p)
  return out.length ? out.join(" · ") : "Daimon"
}
