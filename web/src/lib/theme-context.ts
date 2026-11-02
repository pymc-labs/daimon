import { createContext, useContext } from "react"

export type Theme = "system" | "light" | "dark"
export const Ctx = createContext<{ theme: Theme; resolved: "light" | "dark"; setTheme: (t: Theme) => void }>({
  theme: "system", resolved: "light", setTheme: () => {},
})

export const useTheme = () => useContext(Ctx)
