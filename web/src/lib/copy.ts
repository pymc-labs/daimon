import { toast } from "sonner"

/** Copy to the clipboard; the clipboard is missing over plain HTTP, so say when it fails. `done` toasts on success. */
export async function copy(text: string, done?: string): Promise<boolean> {
  try {
    await navigator.clipboard.writeText(text)
  } catch {
    toast.error("Could not copy")
    return false
  }
  if (done) toast(done)
  return true
}
