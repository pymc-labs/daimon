export const safeNext = (raw: string | null) => (raw && raw.startsWith("/") && !/^\/[/\\]/.test(raw) ? raw : "/chat")
