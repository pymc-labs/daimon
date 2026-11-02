import { Globe, Link2, Mail } from "lucide-react"
import { siDiscord, siGithub, siGoogle } from "simple-icons"

import type { Identity } from "@/lib/types"
import { cn } from "@/lib/utils"

/** Daimon, the product mark. */
export function Logo({ className }: { className?: string }) {
  return <img src="/daimon.png" alt="" className={cn("size-6 shrink-0 object-contain", className)} />
}

function Si({ path, color, className }: { path: string; color?: string; className?: string }) {
  return (
    <svg viewBox="0 0 24 24" className={cn("size-4 shrink-0", className)} aria-hidden fill={color ?? "currentColor"}>
      <path d={path} />
    </svg>
  )
}

function SlackMark({ className }: { className?: string }) {
  return (
    <svg viewBox="0 0 24 24" className={cn("size-4 shrink-0", className)} aria-hidden>
      <rect x="8" y="2" width="3.4" height="11" rx="1.7" fill="#36C5F0" />
      <rect x="12.6" y="11" width="3.4" height="11" rx="1.7" fill="#ECB22E" />
      <rect x="11" y="8" width="11" height="3.4" rx="1.7" fill="#2EB67D" />
      <rect x="2" y="12.6" width="11" height="3.4" rx="1.7" fill="#E01E5A" />
    </svg>
  )
}

function TeamsMark({ className }: { className?: string }) {
  return (
    <svg viewBox="0 0 24 24" className={cn("size-4 shrink-0", className)} aria-hidden>
      <rect x="2" y="2" width="9.5" height="9.5" fill="#F25022" />
      <rect x="12.5" y="2" width="9.5" height="9.5" fill="#7FBA00" />
      <rect x="2" y="12.5" width="9.5" height="9.5" fill="#00A4EF" />
      <rect x="12.5" y="12.5" width="9.5" height="9.5" fill="#FFB900" />
    </svg>
  )
}

type Brand = "discord" | "slack" | "teams" | "google" | "github" | "email" | "web"

/** A platform's mark. */
export function BrandIcon({ brand, className, mono }: { brand: Brand | string; className?: string; mono?: boolean }) {
  switch (brand) {
    case "discord": return <Si path={siDiscord.path} color={mono ? undefined : "#5865F2"} className={className} />
    case "google": return <Si path={siGoogle.path} color={mono ? undefined : "#4285F4"} className={className} />
    case "github": return <Si path={siGithub.path} className={className} />
    case "slack": return <SlackMark className={className} />
    case "teams": return <TeamsMark className={className} />
    case "email": return <Mail className={cn("size-4 shrink-0", className)} />
    default: return <Globe className={cn("size-4 shrink-0", className)} />
  }
}


/** A linked sign-in's mark; invite identities name the link icon. */
export function IdentityIcon({ identity }: { identity: Identity }) {
  return identity.icon === "link" ? <Link2 className="size-4 shrink-0" /> : <BrandIcon brand={identity.provider} />
}
