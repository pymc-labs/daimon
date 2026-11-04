import { setupWorker } from "msw/browser"

import { adminHandlers } from "./admin"
import { agentLibraryHandlers } from "./agent-library"
import { agentSetupHandlers } from "./agent-setup"
import { agentsHandlers } from "./agents"
import { channelsHandlers } from "./channels"
import { chatHandlers } from "./chat"
import { coreHandlers } from "./core"
import { creditsHandlers } from "./credits"
import { integrationsHandlers } from "./integrations"
import { profileHandlers } from "./profile"
import { routinesHandlers } from "./routines"
import { serverSkillsHandlers } from "./agent-server-skills"

// Module handlers first, so a module can override a core route it owns.
export const worker = setupWorker(
  ...chatHandlers, ...serverSkillsHandlers, ...agentLibraryHandlers, ...agentSetupHandlers, ...agentsHandlers, ...channelsHandlers, ...routinesHandlers, ...creditsHandlers, ...integrationsHandlers, ...profileHandlers, ...adminHandlers, ...coreHandlers,
)
