import { Link } from "react-router"

import { Button } from "@/components/ui/button"
import { Card } from "./Card"

export function SignedOut() {
  return (
    <Card>
      <h1 className="mt-5 text-lg font-semibold">Signed out</h1>
      <Button asChild variant="outline" size="lg" className="mt-6 w-full"><Link to="/signin">Sign in again</Link></Button>
    </Card>
  )
}

export function NotFound() {
  return (
    <div className="flex flex-1 flex-col items-center justify-center gap-3 p-6">
      <div className="text-sm font-medium">Page not found</div>
      <Button asChild variant="outline"><Link to="/chat">Go to chat</Link></Button>
    </div>
  )
}
