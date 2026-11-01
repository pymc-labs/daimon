import { lazy } from "react"

/** A page loaded when its route is first opened, from a module with a named export. */
export const lazyPage = <K extends string>(load: () => Promise<Record<K, React.ComponentType>>, name: K) =>
  lazy(() => load().then((m) => ({ default: m[name] })))
