import { setupWorker } from "msw/browser"

import { adminHandlers } from "./admin"
import { agentsHandlers } from "./agents"
import { chatHandlers } from "./chat"
import { coreHandlers } from "./core"
import { creditsHandlers } from "./credits"

// Module handlers first, so a module can override a core route it owns.
export const worker = setupWorker(...chatHandlers, ...agentsHandlers, ...creditsHandlers, ...adminHandlers, ...coreHandlers)
