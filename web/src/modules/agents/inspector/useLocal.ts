import { useState } from "react"

/** Text that writes to the draft as you type, debounced. */
export function useLocal(value: string, write: (v: string) => void) {
  const [v, setV] = useState(value)
  const [seen, setSeen] = useState(value)
  if (seen !== value) { setSeen(value); setV(value) }
  return [v, (x: string) => { setV(x); write(x) }] as const
}
