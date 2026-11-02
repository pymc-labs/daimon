import { api } from "@/lib/api"

export async function signOut() {
  await api.post("/auth/logout")
  location.assign("/signin")
}
