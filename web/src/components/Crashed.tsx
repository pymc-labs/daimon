import { Component } from "react"

import { Button } from "@/components/ui/button"
import { cn } from "@/lib/utils"

type Props = { children: React.ReactNode; className?: string; resetKey?: string }

/** Catches a render error anywhere below so the app shows a way back instead of a blank page; a new `resetKey` tries again. */
export class Crashed extends Component<Props, { error: boolean; key?: string }> {
  state = { error: false, key: this.props.resetKey }
  static getDerivedStateFromError = () => ({ error: true })
  static getDerivedStateFromProps = (p: Props, s: { key?: string }) => (p.resetKey === s.key ? null : { error: false, key: p.resetKey })
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
