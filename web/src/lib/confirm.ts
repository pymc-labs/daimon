import { createContext, useContext } from "react"

export type Opts = { title: string; body?: React.ReactNode; action: string; destructive?: boolean }
export const Ctx = createContext<(o: Opts) => Promise<boolean>>(async () => false)

/** `await confirm({title, action})` resolves true when the user confirms. */
export const useConfirm = () => useContext(Ctx)
