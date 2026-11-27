import { Logo } from "@/components/brand"
import { cn } from "@/lib/utils"

/** The standalone page shell. `wide` is for a page that holds a list: left-aligned, no logo. */
export function Card({ children, mark, wide }: { children: React.ReactNode; mark?: React.ReactNode; wide?: boolean }) {
  return (
    <div className={cn("flex min-h-dvh items-start justify-center bg-muted", wide ? "p-3 pt-6 sm:p-6 sm:pt-[6vh]" : "p-6 pt-[12vh]")}>
      <div className={cn("w-full rounded-xl border bg-background shadow-[0_1px_2px_rgb(0_0_0/0.04)]", wide ? "max-w-[600px] p-5 sm:p-8" : "max-w-[400px] p-8 text-center")}>
        {wide ? mark : (mark ?? <Logo className="mx-auto size-12" />)}
        {children}
      </div>
    </div>
  )
}
