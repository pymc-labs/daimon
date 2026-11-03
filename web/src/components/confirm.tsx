import { useCallback, useRef, useState } from "react"

import {
  AlertDialog, AlertDialogAction, AlertDialogCancel, AlertDialogContent, AlertDialogDescription, AlertDialogFooter,
  AlertDialogHeader, AlertDialogTitle,
} from "@/components/ui/alert-dialog"
import { type Choice, Ctx, type Opts } from "@/lib/confirm"

export function ConfirmProvider({ children }: { children: React.ReactNode }) {
  const [opts, setOpts] = useState<Opts | null>(null)
  const resolve = useRef<(v: Choice) => void>(() => {})
  const confirm = useCallback((o: Opts) => new Promise<Choice>((r) => { resolve.current = r; setOpts(o) }), [])
  const close = (v: Choice) => { resolve.current(v); setOpts(null) }
  return (
    <Ctx.Provider value={confirm}>
      {children}
      <AlertDialog open={!!opts} onOpenChange={(o) => !o && close(null)}>
        <AlertDialogContent className="sm:max-w-sm">
          <AlertDialogHeader>
            <AlertDialogTitle>{opts?.title}</AlertDialogTitle>
            {opts?.body && <AlertDialogDescription>{opts.body}</AlertDialogDescription>}
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel onClick={() => close(null)}>Cancel</AlertDialogCancel>
            {opts?.alternate && <AlertDialogAction variant="outline" onClick={() => close("alternate")}>{opts.alternate}</AlertDialogAction>}
            <AlertDialogAction variant={opts?.destructive ? "destructive" : "default"} onClick={() => close("action")}>{opts?.action}</AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </Ctx.Provider>
  )
}

