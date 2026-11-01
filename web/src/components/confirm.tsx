import { createContext, useCallback, useContext, useRef, useState } from "react"

import {
  AlertDialog, AlertDialogAction, AlertDialogCancel, AlertDialogContent, AlertDialogDescription, AlertDialogFooter,
  AlertDialogHeader, AlertDialogTitle,
} from "@/components/ui/alert-dialog"

type Opts = { title: string; body?: React.ReactNode; action: string; destructive?: boolean }
const Ctx = createContext<(o: Opts) => Promise<boolean>>(async () => false)

/** `await confirm({title, action})` resolves true when the user confirms. */
export const useConfirm = () => useContext(Ctx)

export function ConfirmProvider({ children }: { children: React.ReactNode }) {
  const [opts, setOpts] = useState<Opts | null>(null)
  const resolve = useRef<(v: boolean) => void>(() => {})
  const confirm = useCallback((o: Opts) => new Promise<boolean>((r) => { resolve.current = r; setOpts(o) }), [])
  const close = (v: boolean) => { resolve.current(v); setOpts(null) }
  return (
    <Ctx.Provider value={confirm}>
      {children}
      <AlertDialog open={!!opts} onOpenChange={(o) => !o && close(false)}>
        <AlertDialogContent className="sm:max-w-sm">
          <AlertDialogHeader>
            <AlertDialogTitle>{opts?.title}</AlertDialogTitle>
            {opts?.body && <AlertDialogDescription>{opts.body}</AlertDialogDescription>}
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel onClick={() => close(false)}>Cancel</AlertDialogCancel>
            <AlertDialogAction variant={opts?.destructive ? "destructive" : "default"} onClick={() => close(true)}>{opts?.action}</AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </Ctx.Provider>
  )
}

