import { Code2, KeyRound, MousePointer2, Pi, Plug, SquareTerminal } from "lucide-react"

/** The ways to reach an agent from a coding tool, in the order the panel lists them. `token` is a key made by hand. */
export const CODING_CLIENTS = [
  { id: "claude-code", name: "Claude Code", icon: SquareTerminal },
  { id: "codex", name: "Codex", icon: Code2 },
  { id: "cursor", name: "Cursor", icon: MousePointer2 },
  { id: "pi", name: "Pi", icon: Pi },
  { id: "mcp", name: "Other MCP", icon: Plug },
  { id: "token", name: "Token", icon: KeyRound },
] as const

/** A key's tool as a person reads it; empty for a key from before tools were recorded. */
export const clientName = (id: string | null) => CODING_CLIENTS.find((c) => c.id === id)?.name ?? null
