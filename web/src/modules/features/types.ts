// `/api/features*` shapes: the schema describes every key, a place's features say what is set where.
export type Tier = "deployment" | "workspace" | "agent" | "channel" | "thread"

export type FeatureSchema = {
  key: string
  name: string
  category: string
  categoryName: string
  values: { value: string; label: string }[]
  default: string
  tiers: Tier[]
  lockable: Tier[]
  platforms: string[]
  threadAnyone: boolean
  native: boolean
  doc: string
}

/** One tier's own value; `text` is `default`, `<value>` or `<value>:locked`, the value `expected` carries back. */
export type TierValue = { tier: Tier; value: string; locked: boolean; text: string }

export type FeatureView = {
  key: string
  tiers: TierValue[]
  value: string
  source: Tier | "default"
  locked: boolean
  unavailable: string | null
  summary: string
}

export type FeaturesDoc = { platform: string; features: FeatureView[] }

/** Where to read or change: nothing is the workspace; an agent, a channel or a thread of a channel. */
export type FeaturePlace = { agent?: string; channel?: string; thread?: string }
