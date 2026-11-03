import type { LibrarySkill } from "../types"

export const sourceLabel = (s: Pick<LibrarySkill, "source" | "default">) => (s.source === "anthropic" ? "Anthropic" : s.default ? "Built in" : "Custom")

const DESCRIPTION = /^description:[ \t]*(.*)$/m

/** The description line of a SKILL.md, unquoted. */
export function descriptionOf(md: string) {
  const raw = md.match(DESCRIPTION)?.[1]?.trim() ?? ""
  if (raw.startsWith('"')) { try { return String(JSON.parse(raw)) } catch { return raw } }
  return raw.replace(/^'(.*)'$/, "$1")
}

/** `md` with its description line set; quoted when the text would break the front matter. */
export function withDescription(md: string, text: string) {
  const line = `description: ${/^[A-Za-z0-9][^:#"'\n]*$/.test(text) || !text ? text : JSON.stringify(text)}`
  return DESCRIPTION.test(md) ? md.replace(DESCRIPTION, line) : md.replace(/^---\n/, `---\n${line}\n`)
}
