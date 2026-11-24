export const PROVIDER: Record<string, string> = { anthropic: "Anthropic", openai: "OpenAI", gemini: "Google", selfhosted: "Self-hosted" }

/** Models under their provider, providers in the order the server lists them (it also orders each provider's models). */
export function byProvider<T>(models: readonly T[], provider: (m: T) => string): { provider: string; name: string; models: T[] }[] {
  const groups = new Map<string, T[]>()
  for (const m of models) groups.set(provider(m), [...(groups.get(provider(m)) ?? []), m])
  return [...groups].map(([p, list]) => ({ provider: p, name: PROVIDER[p] ?? p, models: list }))
}
