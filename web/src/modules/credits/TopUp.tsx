import { useMutation, useQueryClient } from "@tanstack/react-query"
import { useRef, useState } from "react"
import { toast } from "sonner"
import { Button } from "@/components/ui/button"
import { Dialog, DialogContent, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import { api } from "@/lib/api"
import { usd } from "@/lib/format"
import { cn } from "@/lib/utils"

const AMOUNTS = { checkout: ["10", "25", "50", "100"], manual: ["50", "100", "250", "500"] }

export function TopUp({ mode, open, onOpenChange }: { mode: "checkout" | "manual"; open: boolean; onOpenChange: (o: boolean) => void }) {
  const [amount, setAmount] = useState("50")
  const qc = useQueryClient()
  // One id per attempt at an amount, so a retry of the same click credits once.
  const attempt = useRef({ amount: "", id: "" })
  const go = useMutation({
    mutationFn: () => {
      if (attempt.current.amount !== amount) attempt.current = { amount, id: crypto.randomUUID() }
      return api.post<{ checkout_url?: string; balance_usd?: number }>("/api/credits/topup", { amount_usd: Number(amount), request_id: attempt.current.id })
    },
    onSuccess: (r) => {
      if (r.checkout_url) { window.location.assign(r.checkout_url); return }
      attempt.current = { amount: "", id: "" }
      void qc.invalidateQueries({ queryKey: ["credits"] }); void qc.invalidateQueries({ queryKey: ["me"] }); onOpenChange(false); toast(`Added ${usd(Number(amount))}`)
    },
  })
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="sm:max-w-sm">
        <DialogHeader><DialogTitle>Top up</DialogTitle></DialogHeader>
        <div className="grid grid-cols-4 gap-2">
          {AMOUNTS[mode].map((a) => (
            <Button key={a} variant="outline" onClick={() => setAmount(a)}
              className={cn("tabular", amount === a && "border-foreground bg-accent")}>${a}</Button>
          ))}
        </div>
        {mode === "manual" && (
          <div className="space-y-1.5">
            <Label htmlFor="amt">Amount</Label>
            <Input id="amt" inputMode="decimal" value={amount} onChange={(e) => setAmount(e.target.value.replace(/[^\d.]/g, ""))} />
          </div>
        )}
        <DialogFooter>
          <Button variant="outline" onClick={() => onOpenChange(false)}>Cancel</Button>
          <Button onClick={() => go.mutate()} disabled={!Number(amount) || go.isPending}>{mode === "checkout" ? "Continue to checkout" : "Add credit"}</Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
