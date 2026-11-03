import { createContext, useCallback, useContext } from "react"

export type Opts = { title: string; body?: React.ReactNode; action: string; destructive?: boolean; alternate?: string }
export type Choice = "action" | "alternate" | null
export const Ctx = createContext<(o: Opts) => Promise<Choice>>(async () => null)

/** `await choose({title, action, alternate})` resolves which button was pressed, or null when cancelled. */
export const useChoose = () => useContext(Ctx)

/** `await confirm({title, action})` resolves true when the user confirms. */
export function useConfirm() {
  const choose = useContext(Ctx)
  return useCallback(async (o: Opts) => (await choose(o)) === "action", [choose])
}
