export const safeNext = (raw: string | null) => (raw && raw.startsWith("/") && !/^\/[/\\]/.test(raw) ? raw : "/chat")
/** Artifact pages (`/a/...`) are served by the server, outside the app's router. */
export const isServerPage = (path: string) => path.startsWith("/a/")
