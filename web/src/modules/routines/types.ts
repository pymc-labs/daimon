export type Choice = { id: string; name: string }
/** Where a routine posts; a thread gets no "#". */
export type Destination = Choice & { kind?: "channel" | "thread" }
export type CatchUp = "skip" | "run-once"

export type Routine = {
  id: string
  /** The name it was given, if any. */
  name: string | null
  /** `name`, else the first line of the prompt. */
  title: string
  agent: Choice
  cron: string
  timezone: string
  /** The schedule in words, from the server. */
  schedule: string
  message: string
  enabled: boolean
  catch_up: CatchUp
  destination: Destination | null
  /** Delivered to the owner by direct message. */
  dm: boolean
  sandbox_tools: boolean
  next_run: string | null
  last_run: string | null
  failed: boolean
  last_output: string | null
  /** Made by the signed-in person; admins also see everyone else's. */
  mine: boolean
  owner: string | null
}

/** What the editor may pick from. */
export type Choices = { agents: Choice[]; destinations: Destination[]; can_create: boolean; can_dm: boolean; platform: string }

export type NewRoutine = { name?: string; agent_id: string; cron: string; timezone: string; message: string; catch_up: CatchUp; destination: string | null; dm?: boolean }
export type RoutineChange = Partial<Omit<NewRoutine, "destination">> & { destination?: string | null; clear_destination?: boolean }
