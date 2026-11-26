import { toast } from "sonner"

import { api } from "@/lib/api"

/** Leave only once the server ended the session: on a shared computer, a failed sign-out must not look done. */
export async function signOut() {
  try {
    await api.post("/auth/logout")
  } catch {
    toast.error("Could not sign out. Try again.")
    return
  }
  location.assign("/signin")
}
