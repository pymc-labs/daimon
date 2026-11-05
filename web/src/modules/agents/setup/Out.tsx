import { cn } from "@/lib/utils"

// Links come from repo metadata and release tags, so only https ones become links.
const https = (url: string | null): url is string => !!url && url.startsWith("https:")

/** An outside address: a link when https, plain text otherwise. */
export const Out = ({ href, children, className }: { href: string | null; children: React.ReactNode; className?: string }) =>
  https(href)
    ? <a href={href} target="_blank" rel="noreferrer" className={cn("text-link hover:underline", className)}>{children}</a>
    : <span className={className}>{children}</span>
