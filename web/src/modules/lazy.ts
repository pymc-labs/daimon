import { lazy } from "react"

const RELOADED_AT = "daimon.reloaded-at"

/** Reloads to pick up a new build; false when one just happened (or storage is blocked), so a missing file cannot loop. */
export function reloadForNewBuild(): boolean {
  try {
    if (Date.now() - Number(sessionStorage.getItem(RELOADED_AT)) < 10_000) return false
    sessionStorage.setItem(RELOADED_AT, String(Date.now()))
  } catch { return false }
  location.reload()
  return true
}

/** A page loaded when its route is first opened, from a module with a named export. */
export const lazyPage = <K extends string>(load: () => Promise<Record<K, React.ComponentType>>, name: K) =>
  lazy(() => load().then(
    (m) => ({ default: m[name] }),
    // A tab open across a deploy asks for chunks that are gone: keep the loading state while the new build loads.
    (e: unknown) => reloadForNewBuild() ? new Promise<never>(() => {}) : Promise.reject(e),
  ))
