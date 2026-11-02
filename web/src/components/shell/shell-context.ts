import { createContext, useContext } from "react"

export const ShellCtx = createContext<{ openNav: () => void; openPalette: () => void; openShortcuts: () => void }>({
  openNav: () => {}, openPalette: () => {}, openShortcuts: () => {},
})
export const useShell = () => useContext(ShellCtx)
