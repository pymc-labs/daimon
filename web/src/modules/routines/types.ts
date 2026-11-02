export type Choice = { id: string; name: string }
export type CatchUp = "skip" | "run-once"

export type Routine = {
  id: string
  agent: Choice
  cron: string
  timezone: string
  /** The schedule in words, from the server. */
  schedule: string
  message: string
  enabled: boolean
  catch_up: CatchUp
  destination: Choice | null
  next_run: string | null
  last_run: string | null
  failed: boolean
  last_output: string | null
  /** Made by the signed-in person; admins also see everyone else's. */
  mine: boolean
  owner: string | null
}

/** What the editor may pick from. */
export type Choices = { agents: Choice[]; destinations: Choice[]; can_create: boolean; platform: string }

export type NewRoutine = { agent_id: string; cron: string; timezone: string; message: string; catch_up: CatchUp; destination: string | null }
export type RoutineChange = Partial<Omit<NewRoutine, "destination">> & { destination?: string | null; clear_destination?: boolean }
