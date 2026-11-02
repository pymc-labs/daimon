import type { Invite } from "./Invite"

/** Where an accepted invite lands: the first agent it names, else chat. */
export const landingOf = (i: Pick<Invite, "agents">) => (i.agents[0] ? `/chat?agent=${i.agents[0].id}` : "/chat")
