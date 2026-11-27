import { setupWorker } from "msw/browser"

import { adminHandlers } from "./admin"
import { agentGithubHandlers } from "./agent-github"
import { agentLibraryHandlers } from "./agent-library"
import { serverSkillsHandlers } from "./agent-server-skills"
import { agentSetupHandlers } from "./agent-setup"
import { agentsHandlers } from "./agents"
import { channelsHandlers } from "./channels"
import { featuresHandlers } from "./features"
import { chatHandlers } from "./chat"
import { coreHandlers } from "./core"
import { creditsHandlers } from "./credits"
import { githubConnectHandlers } from "./github-connect"
import { integrationsHandlers } from "./integrations"
import { opsHandlers } from "./ops"
import { profileHandlers } from "./profile"
import { routinesHandlers } from "./routines"
import { supportHandlers } from "./support"
import { workflowRunsHandlers, workflowsHandlers } from "./workflows"
import { workflowsHooksHandlers } from "./workflows-hooks"
import { workflowsInboxHandlers } from "./workflows-inbox"

// Module handlers first, so a module can override a core route it owns.
export const worker = setupWorker(
  ...githubConnectHandlers, ...chatHandlers, ...serverSkillsHandlers, ...agentLibraryHandlers, ...agentSetupHandlers, ...agentGithubHandlers, ...agentsHandlers, ...channelsHandlers, ...featuresHandlers, ...supportHandlers, ...routinesHandlers, ...workflowRunsHandlers, ...workflowsInboxHandlers, ...workflowsHooksHandlers, ...workflowsHandlers, ...creditsHandlers, ...integrationsHandlers, ...profileHandlers, ...opsHandlers, ...adminHandlers, ...coreHandlers,
)
