import { createContext, useContext, useEffect, useState } from "react"

type Theme = "system" | "light" | "dark"
const Ctx = createContext<{ theme: Theme; resolved: "light" | "dark"; setTheme: (t: Theme) => void }>({
  theme: "system", resolved: "light", setTheme: () => {},
})

export function ThemeProvider({ children }: { children: React.ReactNode }) {
  // stored only when picked; the old key held "system" for everyone
  const [theme, set] = useState<Theme>(() => (localStorage.getItem("daimon:theme") as Theme) || "light")
  const setTheme = (t: Theme) => { localStorage.setItem("daimon:theme", t); set(t) }
  const [system, setSystem] = useState(() => matchMedia("(prefers-color-scheme: dark)").matches)
  useEffect(() => {
    const m = matchMedia("(prefers-color-scheme: dark)")
    const on = () => setSystem(m.matches)
    m.addEventListener("change", on)
    return () => m.removeEventListener("change", on)
  }, [])
  const resolved = theme === "system" ? (system ? "dark" : "light") : theme
  useEffect(() => {
    document.documentElement.classList.toggle("dark", resolved === "dark")
  }, [resolved])
  return <Ctx.Provider value={{ theme, resolved, setTheme }}>{children}</Ctx.Provider>
}

export const useTheme = () => useContext(Ctx)
