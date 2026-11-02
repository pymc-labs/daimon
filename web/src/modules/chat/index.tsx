import { MessageSquare } from "lucide-react"
import { Route } from "react-router"

import type { ModuleDef } from "../types"
import { ChatView, NewChat } from "./Chat"

export const chat: ModuleDef = {
  key: "chat",
  nav: [{ to: "/chat", label: "Chat", icon: MessageSquare, g: "c" }],
  routes: (
    <>
      <Route path="/chat" element={<NewChat />} />
      <Route path="/chat/:id" element={<ChatView />} />
    </>
  ),
}
