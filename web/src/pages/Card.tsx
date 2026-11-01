import { Logo } from "@/components/brand"

export function Card({ children, mark }: { children: React.ReactNode; mark?: React.ReactNode }) {
  return (
    <div className="flex min-h-dvh items-start justify-center bg-muted p-6 pt-[12vh]">
      <div className="w-full max-w-[400px] rounded-xl border bg-background p-8 text-center shadow-[0_1px_2px_rgb(0_0_0/0.04)]">
        {mark ?? <Logo className="mx-auto size-12" />}
        {children}
      </div>
    </div>
  )
}

export const safeNext = (raw: string | null) => (raw && raw.startsWith("/") && !/^\/[/\\]/.test(raw) ? raw : "/chat")
