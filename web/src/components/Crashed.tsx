import { Component } from "react"

import { Button } from "@/components/ui/button"
import { cn } from "@/lib/utils"

/** Catches a render error anywhere below so the app shows a way back instead of a blank page. */
export class Crashed extends Component<{ children: React.ReactNode; className?: string }, { error: boolean }> {
  state = { error: false }
  static getDerivedStateFromError = () => ({ error: true })
  render() {
    if (!this.state.error) return this.props.children
    return (
      <div role="alert" className={cn("flex h-dvh flex-col items-center justify-center gap-3 text-center", this.props.className)}>
        <div className="text-sm font-medium">Something went wrong</div>
        <Button variant="outline" size="sm" onClick={() => location.reload()}>Reload</Button>
      </div>
    )
  }
}
