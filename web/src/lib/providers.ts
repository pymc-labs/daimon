export const PROVIDER = { anthropic: "Anthropic", openai: "OpenAI", gemini: "Google", selfhosted: "Self-hosted" } as const

/** Models under their provider, providers in the order the server lists them (it also orders each provider's models). */
export function byProvider<T>(models: readonly T[], provider: (m: T) => string): { provider: string; name: string; models: T[] }[] {
  const groups = new Map<string, T[]>()
  for (const m of models) {
    const p = provider(m)
    const list = groups.get(p)
    if (list) list.push(m)
    else groups.set(p, [m])
  }
  return [...groups].map(([p, list]) => ({ provider: p, name: Object.hasOwn(PROVIDER, p) ? PROVIDER[p as keyof typeof PROVIDER] : p, models: list }))
}
