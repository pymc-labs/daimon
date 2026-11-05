import type { LibrarySkill } from "../types"

export const sourceLabel = (s: Pick<LibrarySkill, "source" | "default" | "server">) =>
  s.source === "anthropic" ? "Anthropic" : s.default ? "Built in" : s.server ? "Skills server" : "Custom"

// No YAML package is installed, so only the single-line `description:` of the front matter block is
// read or written; anything else in the block, and the body, is left as it is.
const FRONT = /^---[ \t]*\r?\n([\s\S]*?)\r?\n---[ \t]*(?:\r?\n|$)/
const KEY = /^description:[ \t]*(.*?)[ \t]*$/

type Located = { start: number; end: number; lines: string[]; at: number }

function frontMatter(md: string): Located | null {
  const m = FRONT.exec(md)
  if (!m) return null
  const start = m[0].indexOf(m[1]!)
  const lines = m[1]!.split(/\r?\n/)
  return { start, end: start + m[1]!.length, lines, at: lines.findIndex((l) => KEY.test(l)) }
}

/** The text of a one-line YAML scalar, or null when it is folded, literal, spans lines or is not valid. */
function scalar(raw: string): string | null {
  if (raw === "") return ""
  if (/^[>|]/.test(raw)) return null
  if (raw.startsWith('"')) {
    try { const v: unknown = JSON.parse(raw); return typeof v === "string" ? v : null } catch { return null }
  }
  if (raw.startsWith("'")) return /^'(?:[^']|'')*'(?:[ \t]+#.*)?$/.test(raw) ? raw.replace(/^'((?:[^']|'')*)'.*$/, "$1").replace(/''/g, "'") : null
  if (/^[[{&*!%@`]/.test(raw)) return null
  return raw.replace(/[ \t]+#.*$/, "")
}

/** The description of a SKILL.md, and whether it can be edited here: not when it is folded, literal or over several lines. */
export function descriptionField(md: string): { value: string; editable: boolean } {
  const f = frontMatter(md)
  if (!f) return { value: "", editable: false }
  if (f.at < 0) return { value: "", editable: true }
  const value = scalar(KEY.exec(f.lines[f.at]!)![1]!)
  const continued = /^[ \t]+\S/.test(f.lines[f.at + 1] ?? "")
  return value === null || continued ? { value: "", editable: false } : { value, editable: true }
}

/** `md` with its description set, always as a quoted string. Unchanged when the field is not editable. */
export function withDescription(md: string, text: string) {
  const f = frontMatter(md)
  if (!f || !descriptionField(md).editable) return md
  const line = `description: ${JSON.stringify(text)}`
  const lines = [...f.lines]
  if (f.at < 0) lines.unshift(line)
  else lines[f.at] = line
  const eol = md.includes("\r\n") ? "\r\n" : "\n"
  return md.slice(0, f.start) + lines.join(eol) + md.slice(f.end)
}

/** The server's `slug_of`: the name a skill is saved under. */
export const slugOf = (text: string) => text.trim().toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-+|-+$/g, "")
