import { api } from "@/lib/api"

export async function signOut() {
  try {
    await api.post("/auth/logout")
  } finally {
    location.assign("/signin")
  }
}
