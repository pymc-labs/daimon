import { setupWorker } from "msw/browser"

import { adminHandlers } from "./admin"
import { agentsHandlers } from "./agents"
import { channelsHandlers } from "./channels"
import { chatHandlers } from "./chat"
import { coreHandlers } from "./core"
import { creditsHandlers } from "./credits"
import { routinesHandlers } from "./routines"

// Module handlers first, so a module can override a core route it owns.
export const worker = setupWorker(
  ...chatHandlers, ...agentsHandlers, ...channelsHandlers, ...routinesHandlers, ...creditsHandlers, ...adminHandlers, ...coreHandlers,
)
