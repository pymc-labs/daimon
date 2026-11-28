// Choices a step offers, with the words the inspector shows; the publish review reads them back.
import type { AnswerShape, AskWhere, Catalog, Node } from "./types"

export type Choices<T extends string = string> = [T, string][]

export const WRITES: Choices<NonNullable<Node["writes"]>> = [["refuse", "Refuse"], ["propose", "Propose"], ["allow", "Allow"], ["policy", "As the server allows"]]
export const SESSION: Choices = [["main", "Main lane"], ["fresh", "Fresh"]]
export const BUILTINS: Choices<NonNullable<Node["builtins"]>> = [["none", "None"], ["read", "Read"], ["all", "All"]]
export const NETWORK: Choices<NonNullable<Node["network"]>> = [["off", "Off"], ["on", "On"]]
export const OUTCOME: Choices<NonNullable<Node["outcome"]>> = [["succeeded", "Succeed"], ["failed", "Fail"]]
export const WHERE: Choices<AskWhere> = [["inbox", "Inbox"], ["dm", "DM"], ["thread", "Thread"]]
export const ANSWER: Choices<AnswerShape> = [["approve", "Approve"], ["choice", "Choice"], ["text", "Text"], ["form", "Form"]]

export const NOWHERE = "none"
export const NOWHERE_LABEL = "Nowhere in particular"

export const labelOf = (choices: Choices, value: string) => choices.find(([v]) => v === value)?.[1] ?? value
export const asOptions = (choices: Choices) => choices.map(([value, label]) => ({ value, label }))

/** Where a step posts or works: its home, where the run started, a channel or thread, and (for a post) people. */
export function placeOptions(catalog: Catalog | undefined, current: string | undefined, people: boolean) {
  const places = [
    { value: "home", label: "Home channel" }, { value: "origin", label: "Where the run started" },
    ...(people ? [{ value: "owner", label: "Owner" }, { value: "run_as", label: "Person it runs as" }] : []),
    ...(catalog?.channels ?? []).map((c) => ({ value: c.id, label: `#${c.name}` })),
  ]
  return !current || places.some((o) => o.value === current) ? places : [...places, { value: current, label: current }]
}
