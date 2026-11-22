import { Building2, Check, User } from "lucide-react"

import { BrandIcon } from "@/components/brand"
import { Button } from "@/components/ui/button"
import { cn } from "@/lib/utils"
import type { Back, ConnectInstallation, Platform } from "./types"

const TONE = { ok: "text-success", warn: "text-warning", bad: "text-destructive", plain: "text-muted-foreground" } as const

export const GithubMark = ({ className }: { className?: string }) => <BrandIcon brand="github" className={className} />

/** The icon badge that tops a centered status page. */
export function Mark({ icon: Icon, tone = "plain" }: { icon: React.ElementType<{ className?: string }>; tone?: keyof typeof TONE }) {
  return <span className="mx-auto flex size-12 items-center justify-center rounded-full bg-accent"><Icon className={cn("size-5", TONE[tone])} /></span>
}

export function Title({ title, sub }: { title: string; sub?: React.ReactNode }) {
  return (
    <>
      <h1 className="mt-5 text-lg font-semibold">{title}</h1>
      {sub && <p className="mt-1 text-sm text-muted-foreground">{sub}</p>}
    </>
  )
}

/** Errors sit below the submit button. */
export function Err({ children }: { children?: React.ReactNode }) {
  return children ? <p role="alert" className="mt-4 text-sm text-destructive">{children}</p> : null
}

/** Buttons at the bottom right, the filled one rightmost; full width, side by side, on a phone. */
export function Actions({ children, className }: { children: React.ReactNode; className?: string }) {
  return <div className={cn("mt-6 flex justify-end gap-2 max-sm:[&>*]:flex-1", className)}>{children}</div>
}

const PLACE: Record<Platform, string> = { web: "Daimon", discord: "Discord", slack: "Slack", teams: "Teams" }

const NEXT: Partial<Record<Platform, string>> = { slack: "In Slack, run /github", teams: "In Teams, ask Daimon" }

/** The way back to where the person came from; Slack and Teams have no link, so a line names the next step. */
export function BackButton({ back }: { back: Back }) {
  if (back && !back.url && NEXT[back.platform]) return <p className="mt-6 text-sm text-muted-foreground">{NEXT[back.platform]}</p>
  if (!back?.url) return null
  return (
    <Button asChild size="lg" className="mt-6 w-full">
      <a href={back.url}>{back.platform !== "web" && <BrandIcon brand={back.platform} mono className="size-4" />}Back to {PLACE[back.platform]}</a>
    </Button>
  )
}

/** A GitHub owner's icon: round for a person, square for an organization. The web CSP blocks GitHub's avatar images. */
export function OwnerAvatar({ item, className }: { item?: ConnectInstallation; className?: string }) {
  const person = item?.owner_type.toLowerCase() === "user"
  const shape = person ? "rounded-full" : "rounded-md"
  const Icon = person ? User : Building2
  return <span className={cn("flex size-5 shrink-0 items-center justify-center bg-accent text-muted-foreground", shape, className)}><Icon className="size-3" /></span>
}

/** A whole-row choice. `radio` picks one; the default ticks any number. */
export function ChoiceRow({ checked, disabled, onChange, radio, children }: {
  checked: boolean; disabled?: boolean; onChange: () => void; radio?: boolean; children: React.ReactNode
}) {
  return (
    <button
      type="button" role={radio ? "radio" : "checkbox"} aria-checked={checked} disabled={disabled} onClick={onChange}
      className={cn(
        "flex min-h-11 w-full items-center gap-3 rounded-lg border px-3 py-2 text-left text-sm outline-none transition-colors hover:bg-muted focus-visible:border-ring focus-visible:ring-3 focus-visible:ring-ring/50 disabled:cursor-default disabled:opacity-60 disabled:hover:bg-background",
        checked ? "border-primary bg-accent" : "bg-background",
      )}
    >
      <span aria-hidden className={cn("flex size-4 shrink-0 items-center justify-center border", radio ? "rounded-full" : "rounded-[4px]", checked ? "border-primary bg-primary text-primary-foreground" : "border-input")}>
        {checked && <Check className="size-3" />}
      </span>
      <span className="flex min-w-0 flex-1 flex-wrap items-center gap-x-2 gap-y-1">{children}</span>
    </button>
  )
}

const PLATFORM_PLACE: Record<string, string> = { discord: "Server", teams: "Organisation" }

/** The page head of a wide page: the GitHub mark, a title, and where the request came from. */
export function WideHead({ title, platform, workspace }: { title: string; platform: string; workspace: string }) {
  return (
    <div className="flex items-center gap-3">
      <span className="flex size-10 shrink-0 items-center justify-center rounded-lg border bg-muted"><BrandIcon brand="github" className="size-5" /></span>
      <div className="min-w-0">
        <h1 className="text-lg font-semibold break-words">{title}</h1>
        <p className="flex items-center gap-1.5 text-sm text-muted-foreground"><BrandIcon brand={platform} className="size-3.5" /><span className="truncate">{PLATFORM_PLACE[platform] ?? "Workspace"}: {workspace}</span></p>
      </div>
    </div>
  )
}
