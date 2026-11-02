import { useSearchParams } from "react-router"

/** URL-synced param helpers: lists are comma-separated. */
export function useParam(key: string) {
  const [params, setParams] = useSearchParams()
  const value = params.get(key) ?? ""
  const set = (v: string | null) => setParams((p) => {
    if (v) p.set(key, v)
    else p.delete(key)
    return p
  }, { replace: true })
  return [value, set] as const
}

/** Keys that press a button; only when the focus is on the element itself, not something inside it. */
const onPress = (open: () => void) => (e: React.KeyboardEvent) => {
  if (e.target === e.currentTarget && (e.key === "Enter" || e.key === " ")) { e.preventDefault(); open() }
}

/** Props for any element that opens something: clickable and focusable, Enter and Space open. */
export const pressProps = (open: () => void) => ({ role: "button", tabIndex: 0, onClick: open, onKeyDown: onPress(open) })

/** Props for a table row that opens something. */
export const rowProps = (open: () => void, selected = false) => ({
  tabIndex: 0, onClick: open, "data-state": selected ? "selected" : undefined, onKeyDown: onPress(open),
})

export function useListParam(key: string) {
  const [v, set] = useParam(key)
  const list = v ? v.split(",") : []
  return [list, (l: string[]) => set(l.length ? l.join(",") : null)] as const
}
