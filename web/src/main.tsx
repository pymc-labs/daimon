import { MutationCache, QueryClient, QueryClientProvider } from "@tanstack/react-query"
import { StrictMode } from "react"
import { createRoot } from "react-dom/client"
import { BrowserRouter } from "react-router"
import { toast } from "sonner"

import { ConfirmProvider } from "@/components/confirm"
import { Toaster } from "@/components/ui/sonner"
import { TooltipProvider } from "@/components/ui/tooltip"
import { ApiError } from "@/lib/api"
import { ThemeProvider } from "@/lib/theme"
import App from "./App"
import "./index.css"

const client = new QueryClient({
  // Mutations without their own error handling still surface failures.
  mutationCache: new MutationCache({ onError: (e, _v, _c, m) => { if (!m.options.onError && !m.meta?.inline) toast.error(e.message) } }),
  defaultOptions: {
    queries: {
      staleTime: 15_000, refetchOnWindowFocus: false,
      retry: (n, e) => !(e instanceof ApiError && e.status < 500) && n < 2,
    },
  },
})

// `npm run dev:mock`: the API runs in a service worker with mock data; never in a production build.
if (import.meta.env.MODE === "mock") {
  const { worker } = await import("./mock/browser")
  await worker.start({ onUnhandledRequest: "bypass", quiet: true })
}

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <QueryClientProvider client={client}>
      <ThemeProvider>
        <TooltipProvider delayDuration={300}>
          <ConfirmProvider>
            <BrowserRouter>
              <App />
            </BrowserRouter>
          </ConfirmProvider>
          <Toaster position="bottom-right" />
        </TooltipProvider>
      </ThemeProvider>
    </QueryClientProvider>
  </StrictMode>,
)
