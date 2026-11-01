// Mock API for the agents module: roster, drafts, versions. The future `/api/agents` contract.
import { http, HttpResponse } from "msw"

import type {
  Agent, AgentApp, AgentConfig, AgentSummary, AgentVersion, Backend, BuiltinTool, Catalog, CatalogModel, Change, Draft,
  Effect, McpServer, ModelChoice, PersonRef, Place, Skill, Template,
} from "@/modules/agents/types"
import { notFound, unauthorized, wait } from "./util"
import { me, PEOPLE } from "./world"
import { face } from "./face"

const ago = (h: number) => new Date(Date.now() - h * 3_600_000).toISOString()
const ref = (id: string): PersonRef => ({ id, name: PEOPLE.find((p) => p.id === id)?.name ?? "Daimon" })
const clone = <T,>(x: T): T => structuredClone(x)

// ---------- Catalog ----------

const MODELS: CatalogModel[] = [
  { id: "claude-opus-5-5", label: "Opus 5.5", backend: "anthropic", in_usd: 5, out_usd: 25, fast: true },
  { id: "claude-sonnet-5-5", label: "Sonnet 5.5", backend: "anthropic", in_usd: 3, out_usd: 15 },
  { id: "claude-haiku-5-5", label: "Haiku 5.5", backend: "anthropic", in_usd: 1, out_usd: 5 },
  { id: "gpt-5.5", label: "GPT-5.5", backend: "openai", in_usd: 2.5, out_usd: 15 },
  { id: "gpt-5.5-mini", label: "GPT-5.5 mini", backend: "openai", in_usd: 0.4, out_usd: 2 },
  { id: "gemini-3-pro", label: "Gemini 3 Pro", backend: "gemini", in_usd: 2, out_usd: 12 },
]
const modelLabel = (m: ModelChoice) => {
  const c = MODELS.find((x) => x.id === m.id)
  return `${c?.label ?? m.id}${m.speed === "fast" ? " Fast" : ""}, ${m.effort === "xhigh" ? "x-high" : m.effort} effort`
}

const TOOL_LABELS = [
  { name: "bash", label: "Shell" }, { name: "read", label: "Read files" }, { name: "write", label: "Write files" },
  { name: "edit", label: "Edit files" }, { name: "grep", label: "Search files" }, { name: "glob", label: "Find files" },
  { name: "web_fetch", label: "Fetch pages" }, { name: "web_search", label: "Web search" },
]
const toolset = (...on: string[]): BuiltinTool[] => TOOL_LABELS.map((t) => ({ name: t.name, enabled: on.includes(t.name) }))
const CODE_TOOLS = ["bash", "read", "write", "edit", "grep", "glob"]

const md = (id: string, title: string, description: string, body: string) =>
  `---\nname: ${id}\ndescription: ${description}\n---\n\n# ${title}\n\n${body}\n`

/** Files of each published skill version, keyed `<skill id>@<version>`. */
const FILES = new Map<string, Record<string, string>>()
const latestVersion = (id: string) => Math.max(0, ...[...FILES.keys()].filter((k) => k.startsWith(`${id}@`)).map((k) => Number(k.split("@")[1])))

const SKILL = (id: string, title: string, description: string, body: string, extra: Partial<Skill> = {}): Skill => {
  FILES.set(`${id}@3`, { "SKILL.md": md(id, title, description, body) })
  return { type: "custom", skill_id: id, title, description, version: "3", ...extra }
}

const SKILLS: Skill[] = [
  SKILL("data-validation", "Data validation", "Check a dataset's grain, types, ranges and joins before any analysis.",
    "Validate before you trust a number.\n\n## Checks\n\n1. Grain: one row per what? Count duplicates on the key.\n2. Types: dates parse, numbers are numbers.\n3. Ranges: spend is never negative; dates fall in the brief's window.\n4. Joins: row counts before and after every join.\n\nWrite each failed check to `run/validation.md` with the query that found it."),
  SKILL("data-cleaning", "Data cleaning", "Dedup, coerce types and normalise units without editing the source; log every operation.",
    "Cleaned data with no record of what changed is worse than the dirty data it came from.\n\n## Never edit the source\n\n- Write cleaned data to a new parquet file.\n- Append a row to `run/changelog.jsonl` for every operation.\n\n## Order\n\n1. Dedup on the validated key.\n2. Coerce types.\n3. Normalise categories and units.\n4. Flag outliers; never drop them silently."),
  SKILL("exploratory-data-analysis", "Exploratory analysis", "Profile a cleaned dataset: distributions, trends, seasonality, correlations.",
    "## Steps\n\n1. Summary table per column.\n2. Weekly trend per channel.\n3. Seasonality and holidays.\n4. Correlation between spend channels; flag pairs above 0.8."),
  SKILL("eda-storytelling", "Findings write-up", "Turn analysis into a short narrative with one chart per claim.",
    "Lead with the answer. One chart per claim, each with a one-line takeaway as its title."),
  SKILL("pymc-artifact-style", "Chart style", "House style for charts and notebooks: palette, fonts, HDI bands.",
    "## Charts\n\n- Palette: navy, teal, peach.\n- Credible intervals as shaded 94% HDI bands.\n- Axis labels in sentence case with units."),
  SKILL("marimo_notebooks", "Notebooks", "Build reactive marimo notebooks and publish them as reports.",
    "Use marimo for any analysis a person will rerun. One cell per step; no hidden state."),
  SKILL("report-publish", "Publish reports", "Publish a notebook or HTML report and post the link.",
    "Publish to the reports host, then post the link with a one-line summary."),
  SKILL("file-handling", "Files", "Read and send files people share in chat.",
    "Attachments land in `/mnt/session/uploads`. Send outputs from `/mnt/session/outputs`."),
  SKILL("warehouse-pulls", "Warehouse pulls", "Pull spend and outcomes from BigQuery with saved, reviewed queries.",
    "## Rules\n\n- Use `run_query` with a saved query from `queries/`.\n- Never select `*` from a raw events table.\n- Record the query and row count in `run/pulls.md`.", { requires: ["bigquery"] }),
  SKILL("pymc-marketing", "PyMC-Marketing", "Specify, fit and check media mix models with PyMC-Marketing.",
    "## Defaults\n\n- Weekly data, geometric adstock, logistic saturation.\n- Prior predictive check before fitting.\n- Report ROAS with 94% HDI.\n\n## Diagnostics\n\n- R-hat under 1.01, no divergences.\n- Posterior predictive against holdout weeks."),
  SKILL("pr-review", "Pull request review", "Review a pull request for correctness, tests and style.",
    "Read the diff and the touched files. Comment on bugs first, then tests, then style. Approve only when CI passes.", { requires: ["github"] }),
  SKILL("account-brief", "Account brief", "One-page brief on a client account from the CRM and Almanac.",
    "Sections: who they are, open deals, last three touches, risks. Pull deals from HubSpot.", { requires: ["hubspot"] }),
  SKILL("triage", "Ticket triage", "Label, prioritise and route support tickets.",
    "Label by product area. P1 if a customer is blocked. Route to the owning team's Linear project.", { requires: ["linear"] }),
  { type: "anthropic", skill_id: "xlsx", title: "Excel", description: "Edit Excel files, keeping formulas and formatting.", version: "latest" },
  { type: "anthropic", skill_id: "pptx", title: "PowerPoint", description: "Edit PowerPoint decks, keeping layouts.", version: "latest" },
  { type: "anthropic", skill_id: "docx", title: "Word", description: "Edit Word documents with tracked changes.", version: "latest" },
]
const skill = (id: string) => clone(SKILLS.find((s) => s.skill_id === id)!)

const SERVERS: Record<string, McpServer> = {
  almanac: {
    key: "almanac", name: "Almanac", url: "https://almanac.example.com/mcp", auth: "oauth", credential: "connected", app: "almanac",
    tools: [
      { name: "search", description: "Search documents, tables and briefs", effect: "read", rule: "allow" },
      { name: "ask", description: "Ask the librarian a question", effect: "read", rule: "allow" },
      { name: "get_item", description: "Read one item with its sources", effect: "read", rule: "allow" },
      { name: "query_table", description: "Run SQL on a Almanac table", effect: "read", rule: "allow" },
      { name: "save_brief", description: "Save a brief to the client's space", effect: "write", rule: "ask" },
    ],
  },
  bigquery: {
    key: "bigquery", name: "BigQuery", url: "https://mcp.fernhill.example/bigquery", auth: "token", credential: "connected",
    tools: [
      { name: "list_datasets", description: "List datasets and tables", effect: "read", rule: "allow" },
      { name: "run_query", description: "Run a read-only query", effect: "read", rule: "allow" },
      { name: "write_table", description: "Create or replace a table", effect: "write", rule: "deny" },
    ],
  },
  linear: {
    key: "linear", name: "Linear", url: "https://mcp.linear.app/sse", auth: "oauth", credential: "connected",
    tools: [
      { name: "list_issues", description: "List issues in a project", effect: "read", rule: "allow" },
      { name: "create_issue", description: "Create an issue", effect: "write", rule: "allow" },
      { name: "update_issue", description: "Change status, assignee or labels", effect: "write", rule: "ask" },
      { name: "add_comment", description: "Comment on an issue", effect: "write", rule: "ask" },
    ],
  },
  github: {
    key: "github", name: "GitHub", url: "https://api.githubcopilot.com/mcp/", auth: "token", credential: "connected",
    tools: [
      { name: "get_pull_request", description: "Read a pull request and its diff", effect: "read", rule: "allow" },
      { name: "list_files", description: "List changed files", effect: "read", rule: "allow" },
      { name: "create_review", description: "Post a review with comments", effect: "write", rule: "ask" },
      { name: "merge_pull_request", description: "Merge a pull request", effect: "write", rule: "deny" },
    ],
  },
  hubspot: {
    key: "hubspot", name: "HubSpot", url: "https://mcp.hubspot.com/", auth: "oauth", credential: "expired",
    tools: [
      { name: "search_crm", description: "Search companies, contacts and deals", effect: "read", rule: "allow" },
      { name: "get_object", description: "Read one CRM record", effect: "read", rule: "allow" },
      { name: "create_note", description: "Log a note on a record", effect: "write", rule: "ask" },
    ],
  },
}
const server = (k: string) => clone(SERVERS[k]!)

const CH = (name: string, extra: Partial<Place> = {}): Place => ({ id: `ch_${name}`, kind: "channel", platform: "discord", name, ...extra })
const CHANNELS: Place[] = [
  CH("general"), CH("analytics"), CH("mmm-larkspur"), CH("pricing"), CH("eng-reviews"), CH("sales"), CH("support"),
  CH("client-cobalt", { confidential: true }),
]
const DM: Place = { id: "dm", kind: "dm", platform: "discord", name: "Direct messages" }
const WEB: Place = { id: "web", kind: "web", platform: "web", name: "Web chat" }
const place = (name: string, extra: Partial<Place> = {}) => ({ ...clone(CHANNELS.find((c) => c.name === name)!), ...extra })

const CATALOG: Catalog = { models: MODELS, skills: SKILLS, channels: CHANNELS, tools: TOOL_LABELS }

// ---------- Effects and changes ----------

/** Which axis a field sits on (`session_snapshot`): identity needs a new session, mutable reaches it live. */
type Axis = "identity" | "mutable" | "route"
function effectOf(axis: Axis, backend: Backend): Effect {
  if (axis === "route") return "live"
  if (backend !== "anthropic") return "threads"
  return axis === "identity" ? "session" : "live"
}

const RULE = { allow: "Allow", ask: "Ask", deny: "Deny" }
const AUTH = { oauth: "OAuth", token: "Token", none: "None" }

/** Every difference from `a` to `b`, tagged with when it reaches conversations. */
export function diffConfig(a: AgentConfig, b: AgentConfig): Change[] {
  const out: Change[] = []
  const be = a.model.backend
  const add = (c: Omit<Change, "effect">, axis: Axis | Effect) =>
    out.push({ ...c, effect: axis === "identity" || axis === "mutable" || axis === "route" ? effectOf(axis, be) : axis })
  if (a.name !== b.name) add({ key: "name", node: "agent", label: "Name", kind: "edit", before: a.name, after: b.name }, "route")
  if (a.purpose !== b.purpose) add({ key: "purpose", node: "agent", label: "Description", kind: "edit", before: a.purpose, after: b.purpose }, "route")
  if (a.avatar !== b.avatar) add({ key: "avatar", node: "agent", label: "Avatar", kind: "edit" }, "route")
  if ((a.system ?? "") !== (b.system ?? "")) add({ key: "system", node: "instructions", label: "Instructions", kind: "edit", before: a.system ?? "", after: b.system ?? "", text: true }, "identity")
  if (JSON.stringify(a.model) !== JSON.stringify(b.model)) {
    add({ key: "model", node: "model", label: "Model", kind: "edit", before: modelLabel(a.model), after: modelLabel(b.model) },
      a.model.backend !== b.model.backend ? "threads" : "identity")
  }
  const byId = <T,>(xs: T[], k: (x: T) => string) => new Map(xs.map((x) => [k(x), x]))
  const sa = byId(a.skills, (s) => s.skill_id), sb = byId(b.skills, (s) => s.skill_id)
  for (const [id, s] of sb) {
    const o = sa.get(id)
    if (!o) add({ key: `skill:${id}`, node: "skills", label: `${s.title} skill`, kind: "add" }, "identity")
    else if (o.version !== s.version) add({ key: `skill:${id}`, node: "skills", label: `${s.title} skill`, kind: "edit", before: `v${o.version}`, after: `v${s.version}` }, "identity")
  }
  for (const [id, s] of sa) if (!sb.has(id)) add({ key: `skill:${id}`, node: "skills", label: `${s.title} skill`, kind: "remove" }, "identity")
  for (const t of b.tools) {
    const o = a.tools.find((x) => x.name === t.name)
    if (o && o.enabled !== t.enabled) {
      const label = TOOL_LABELS.find((x) => x.name === t.name)?.label ?? t.name
      add({ key: `tool:${t.name}`, node: "tools", label: `${label} tool`, kind: t.enabled ? "add" : "remove" }, "mutable")
    }
  }
  const ma = byId(a.mcp_servers, (m) => m.key), mb = byId(b.mcp_servers, (m) => m.key)
  for (const [k, m] of mb) {
    const o = ma.get(k)
    if (!o) { add({ key: `mcp:${k}`, node: `mcp:${k}`, label: `${m.name} server`, kind: "add", after: m.url }, "mutable"); continue }
    if (o.url !== m.url) add({ key: `mcp:${k}/url`, node: `mcp:${k}`, label: `${m.name} URL`, kind: "edit", before: o.url, after: m.url }, "mutable")
    if (o.auth !== m.auth) add({ key: `mcp:${k}/auth`, node: `mcp:${k}`, label: `${m.name} sign-in`, kind: "edit", before: AUTH[o.auth], after: AUTH[m.auth] }, "mutable")
    for (const t of m.tools) {
      const ot = o.tools.find((x) => x.name === t.name)
      if (ot && ot.rule !== t.rule) add({ key: `mcp:${k}/${t.name}`, node: `mcp:${k}`, label: `${m.name} ${t.name}`, kind: "edit", before: RULE[ot.rule], after: RULE[t.rule] }, "mutable")
    }
  }
  for (const [k, m] of ma) if (!mb.has(k)) add({ key: `mcp:${k}`, node: `mcp:${k}`, label: `${m.name} server`, kind: "remove", before: m.url }, "mutable")
  const pa = byId(a.places ?? [], (p) => p.id), pb = byId(b.places ?? [], (p) => p.id)
  const pname = (p: Place) => (p.kind === "channel" ? `#${p.name}` : p.name)
  for (const [id, p] of pb) {
    const o = pa.get(id)
    if (!o) add({ key: `place:${id}`, node: "places", label: pname(p), kind: "add" }, "route")
    else if (!!o.default !== !!p.default) add({ key: `place:${id}`, node: "places", label: `${pname(p)} default`, kind: "edit", before: o.default ? "On" : "Off", after: p.default ? "On" : "Off" }, "route")
  }
  for (const [id, p] of pa) if (!pb.has(id)) add({ key: `place:${id}`, node: "places", label: pname(p), kind: "remove" }, "route")
  return out
}

// ---------- Seed world ----------

type VersionRec = { version: number; at: string; by: string; note: string | null; config: AgentConfig }
type Rec = {
  id: string; owner: string; co_owners: string[]; last_used_at: string | null; month_usd: number
  versions: VersionRec[]; drafts: Record<string, { base_version: number; config: AgentConfig }>
}

const m = (id: string, effort: ModelChoice["effort"] = "high", speed: ModelChoice["speed"] = "standard"): ModelChoice =>
  ({ id, backend: MODELS.find((x) => x.id === id)!.backend, speed, effort })

/** Build a history: a first config, then dated steps that each change it. */
function history(first: AgentConfig, by: string, at: number, steps: [number, string, string | null, (c: AgentConfig) => void][]): VersionRec[] {
  const out: VersionRec[] = [{ version: 1, at: ago(at), by, note: "Created", config: first }]
  for (const [h, who, note, fn] of steps) {
    const c = clone(out.at(-1)!.config)
    fn(c)
    out.push({ version: out.length + 1, at: ago(h), by: who, note, config: c })
  }
  return out
}

const MMM_SYSTEM = `# MMM Analyst

You build and read media mix models for Fernhill's clients with PyMC-Marketing.

## How you work

- Start from the client's brief in Almanac. When the brief is silent on channels or dates, ask before you pull data.
- Pull spend and outcomes with the warehouse-pulls skill. Never invent spend.
- Fit weekly data with geometric adstock and logistic saturation unless the brief says otherwise.
- Report ROAS with a 94% HDI, and say when an interval crosses 1.

## Output

- Lead with the decision the client can make.
- Charts follow the chart style skill.
- Log open questions as Linear issues in the client's project.`

const DAIMON_SYSTEM = `You are Daimon, the agent this workspace starts with, available in the channel you were mentioned in, able to write and run code, fit models, and produce notebooks and charts. Prefer concise answers; keep actions and follow-ups within the requested scope; cite file paths and line numbers when referencing code.

Speak directly to the person. Public text contains the answer, an action's result, a needed question, or an actionable blocker.

Align before you build. Before producing anything substantial, make sure you're building the right thing. If you lack the data the task needs, or the scope is materially ambiguous, your first reply is a short clarifying question with concrete options and a recommendation.

Never fabricate data and present it as real analysis. If you don't have the data, fetch it or ask for it.

You run across more than one chat platform. The \`<channel>\` element in your context says which. Format your output for that platform.`

function seed(): Rec[] {
  const mmm = history({
    name: "MMM Analyst", purpose: "Media mix models with PyMC-Marketing", avatar: face(4), model: m("claude-sonnet-5-5"),
    system: MMM_SYSTEM.replace("\n- Log open questions as Linear issues in the client's project.", "").replace("Pull spend and outcomes with the warehouse-pulls skill. Never invent spend.", "Never invent spend."),
    skills: ["data-validation", "exploratory-data-analysis", "pymc-artifact-style"].map(skill),
    tools: toolset(...CODE_TOOLS), mcp_servers: [server("almanac")], places: [place("mmm-larkspur", { default: true })],
  }, "p_maya", 960, [
    [720, "p_priya", "Warehouse access", (c) => {
      c.mcp_servers.push(server("bigquery")); c.skills.push(skill("warehouse-pulls"), skill("pymc-marketing"))
      c.system = c.system!.replace("Never invent spend.", "Pull spend and outcomes with the warehouse-pulls skill. Never invent spend.")
    }],
    [600, "p_maya", null, (c) => { c.places!.push(place("analytics"), clone(DM)) }],
    [504, "p_maya", "Opus for model fits", (c) => { c.model = m("claude-opus-5-5") }],
    [300, "p_priya", null, (c) => { c.skills.push(skill("eda-storytelling")); c.tools = toolset(...CODE_TOOLS, "web_fetch") }],
    [216, "p_jonas", "Linear for open questions", (c) => {
      const l = server("linear")
      for (const t of l.tools) t.rule = "allow"
      c.mcp_servers.push(l)
      c.system += "\n- Log open questions as Linear issues in the client's project."
    }],
    [74, "p_maya", null, (c) => { c.places!.push(clone(WEB)) }],
    [5, "p_priya", "Ask before updating issues", (c) => {
      const l = c.mcp_servers.find((s) => s.key === "linear")!
      l.tools.find((t) => t.name === "update_issue")!.rule = "ask"
      l.tools.find((t) => t.name === "add_comment")!.rule = "ask"
    }],
  ])
  const daimon = history({
    name: "Daimon", purpose: "General assistant", avatar: face(11), model: m("claude-sonnet-5-5"), system: DAIMON_SYSTEM,
    skills: ["file-handling", "marimo_notebooks", "pymc-artifact-style", "data-validation", "data-cleaning", "exploratory-data-analysis", "eda-storytelling"].map(skill),
    tools: toolset(...CODE_TOOLS), mcp_servers: [server("almanac")], places: [place("general", { default: true }), clone(DM), clone(WEB)],
  }, "p_maya", 2400, [
    [1200, "p_jonas", null, (c) => { c.skills.push(skill("report-publish")) }],
    [400, "p_maya", "Web search", (c) => { c.tools = toolset(...CODE_TOOLS, "web_fetch", "web_search") }],
  ])
  const pricing = history({
    name: "Pricing Analyst", purpose: "Price elasticity and promo lift", avatar: face(24), model: m("claude-sonnet-5-5"),
    system: "# Pricing Analyst\n\nYou estimate price elasticity and promotion lift with hierarchical Bayesian models.\n\n- Use the client's transaction tables in Almanac.\n- Report elasticities with 94% HDI per segment.",
    skills: ["data-validation", "data-cleaning", "pymc-artifact-style"].map(skill), tools: toolset(...CODE_TOOLS),
    mcp_servers: [server("almanac")], places: [place("pricing", { default: true }), clone(WEB)],
  }, "p_priya", 1300, [[160, "p_priya", null, (c) => { c.skills.push(skill("eda-storytelling")) }]])
  const review = history({
    name: "Code Reviewer", purpose: "Reviews pull requests", avatar: face(33), model: m("claude-sonnet-5-5", "medium"),
    system: "# Code Reviewer\n\nReview pull requests in Fernhill's repositories.\n\n- Bugs first, then missing tests, then style.\n- Never approve with failing CI.\n- Never merge.",
    skills: [skill("pr-review")], tools: toolset("bash", "read", "grep", "glob"), mcp_servers: [server("github")],
    places: [place("eng-reviews", { default: true }), clone(WEB)],
  }, "p_jonas", 1700, [[52, "p_jonas", null, (c) => { c.tools = toolset(...CODE_TOOLS) }]])
  const sales = history({
    name: "Sales Desk", purpose: "Account briefs from the CRM", avatar: face(14), model: m("claude-haiku-5-5", "low"),
    system: "# Sales Desk\n\nWrite account briefs for Fernhill's sales team from HubSpot and Almanac. One page, no speculation.",
    skills: [skill("account-brief"), skill("pptx")], tools: toolset("read", "write"), mcp_servers: [server("almanac"), server("hubspot")],
    places: [place("sales", { default: true }), clone(DM), clone(WEB)],
  }, "p_maya", 900, [[120, "p_maya", null, (c) => { c.skills.push(skill("docx")) }]])
  const support = history({
    name: "Support Triage", purpose: "Labels and routes support tickets", avatar: face(39), model: m("gpt-5.5", "medium"),
    system: "# Support Triage\n\nLabel every new ticket in #support, set a priority, and file it in the owning team's Linear project.",
    skills: [skill("triage")], tools: toolset("read"), mcp_servers: [server("linear")], places: [place("support", { default: true })],
  }, "p_jonas", 500, [[30, "p_jonas", "Comments need approval", (c) => { c.mcp_servers[0]!.tools[3]!.rule = "ask" }]])
  const rec = (id: string, owner: string, co: string[], versions: VersionRec[], used: number | null, usd: number): Rec => ({
    id, owner, co_owners: co, versions, last_used_at: used == null ? null : ago(used), month_usd: usd, drafts: {},
  })
  return [
    rec("ag_daimon", "p_maya", ["p_jonas"], daimon, 0.2, 412.6),
    rec("ag_mmm", "p_maya", ["p_priya"], mmm, 0.3, 286.15),
    rec("ag_pricing", "p_priya", [], pricing, 1, 64.9),
    rec("ag_review", "p_jonas", [], review, 26, 38.4),
    rec("ag_sales", "p_maya", [], sales, 80, 11.2),
    rec("ag_support", "p_jonas", [], support, 3, 22.75),
  ]
}

// Persisted so reloads keep edits; bump the key when the seed changes.
const KEY = "daimon:mock:agents:v1"
let DB: Rec[] = (() => {
  try { const s = localStorage.getItem(KEY); if (s) return JSON.parse(s) as Rec[] } catch { /* reseed */ }
  return seed()
})()
const save = () => { try { localStorage.setItem(KEY, JSON.stringify(DB)) } catch { /* quota */ } }

// ---------- Views ----------

type Viewer = { id: string; admin: boolean; edit: boolean; create: boolean }
function viewer(): Viewer | null {
  const u = me()
  if (!u || !u.modules.includes("agents")) return null
  return { id: u.id, admin: u.role === "owner" || u.role === "admin", edit: u.capabilities.includes("agents.edit"), create: u.capabilities.includes("agents.create") }
}
const details = (r: Rec, v: Viewer) => v.admin || r.owner === v.id || r.co_owners.includes(v.id)
const current = (r: Rec) => r.versions.at(-1)!

/** Strip what this viewer may not read: prompt, credentials, reach detail. */
function redact(c: AgentConfig, full: boolean): AgentConfig {
  if (full) return c
  return { ...c, system: null, places: null, mcp_servers: c.mcp_servers.map((s) => ({ ...s, url: "", credential: null })) }
}

/** The agent's views in apps it has an MCP server at, as the server fills them in. */
function appsOf(c: AgentConfig): AgentApp[] {
  const id = "5b1c2f56-6a39-5c1e-9a0e-3f5a2f6f2d11"
  return c.mcp_servers.some((s) => s.app === "almanac") ? [{ key: "almanac", embed: `/embed/agents/external/${id}`, page: `/agents/external/${id}` }] : []
}

function view(r: Rec, v: Viewer): Agent {
  const cur = current(r)
  const full = details(r, v)
  return {
    id: r.id, version: cur.version, ...redact(cur.config, full), owner: ref(r.owner), co_owners: r.co_owners.map(ref),
    updated_at: cur.at, updated_by: ref(cur.by), last_used_at: r.last_used_at, month_usd: r.month_usd,
    can_edit: full && v.edit, can_view_details: full, apps: full ? appsOf(cur.config) : [],
  }
}

function summary(r: Rec, v: Viewer): AgentSummary {
  const a = view(r, v)
  const c = current(r).config
  const runs = new Set<AgentSummary["runs_in"][number]>()
  for (const p of c.places ?? []) runs.add(p.kind === "channel" ? (p.platform as "discord") : p.kind)
  return {
    id: a.id, name: a.name, purpose: a.purpose, avatar: a.avatar, version: a.version, owner: a.owner, updated_at: a.updated_at,
    updated_by: a.updated_by, last_used_at: a.last_used_at, month_usd: a.month_usd, can_edit: a.can_edit, co_owners: a.co_owners,
    draft_changes: draftView(r, v.id)?.changes.length ?? 0,
    model_label: MODELS.find((x) => x.id === c.model.id)?.label ?? c.model.id, backend: c.model.backend,
    runs_in: (["discord", "slack", "teams", "dm", "web"] as const).filter((k) => runs.has(k)),
  }
}

function draftView(r: Rec, uid: string): Draft | null {
  const d = r.drafts[uid]
  if (!d) return null
  const base = r.versions.find((x) => x.version === d.base_version)!.config
  return { base_version: d.base_version, config: d.config, changes: diffConfig(base, d.config), stale: d.base_version !== current(r).version }
}

function versionsView(r: Rec): AgentVersion[] {
  return r.versions.map((x, i) => ({
    version: x.version, at: x.at, by: ref(x.by), note: x.note, changes: i ? diffConfig(r.versions[i - 1]!.config, x.config) : [],
  })).reverse()
}

// ---------- Templates ----------

const TEMPLATES: (Template & { config: () => Omit<AgentConfig, "name" | "avatar"> })[] = [
  { id: "blank", name: "Blank", purpose: "", icon: "square-dashed", skills: 0, servers: 0, model: "Sonnet 5.5",
    config: () => ({ purpose: "", model: m("claude-sonnet-5-5"), system: "", skills: [], tools: toolset(...CODE_TOOLS), mcp_servers: [], places: [] }) },
  { id: "analyst", name: "Data analyst", purpose: "Cleans, explores and explains data", icon: "chart-line", skills: 5, servers: 1, model: "Sonnet 5.5",
    config: () => ({ purpose: "Cleans, explores and explains data", model: m("claude-sonnet-5-5"),
      system: "# Data analyst\n\nAnswer questions with data from Almanac. Validate before you analyse, and show the query behind every number.",
      skills: ["data-validation", "data-cleaning", "exploratory-data-analysis", "eda-storytelling", "pymc-artifact-style"].map(skill),
      tools: toolset(...CODE_TOOLS), mcp_servers: [server("almanac")], places: [] }) },
  { id: "reviewer", name: "Code reviewer", purpose: "Reviews pull requests", icon: "git-pull-request", skills: 1, servers: 1, model: "Sonnet 5.5",
    config: () => ({ purpose: "Reviews pull requests", model: m("claude-sonnet-5-5", "medium"),
      system: "# Code reviewer\n\nBugs first, then tests, then style. Never merge.", skills: [skill("pr-review")],
      tools: toolset("bash", "read", "grep", "glob"), mcp_servers: [server("github")], places: [] }) },
  { id: "sales", name: "Account briefs", purpose: "Client briefs from the CRM", icon: "briefcase", skills: 2, servers: 2, model: "Haiku 5.5",
    config: () => ({ purpose: "Client briefs from the CRM", model: m("claude-haiku-5-5", "low"),
      system: "# Account briefs\n\nOne-page briefs from HubSpot and Almanac. No speculation.", skills: [skill("account-brief"), skill("docx")],
      tools: toolset("read", "write"), mcp_servers: [server("almanac"), server("hubspot")], places: [] }) },
  { id: "triage", name: "Support triage", purpose: "Labels and routes tickets", icon: "inbox", skills: 1, servers: 1, model: "GPT-5.5",
    config: () => ({ purpose: "Labels and routes tickets", model: m("gpt-5.5", "medium"),
      system: "# Support triage\n\nLabel, prioritise and route every new ticket.", skills: [skill("triage")], tools: toolset("read"),
      mcp_servers: [server("linear")], places: [] }) },
]
const FACES = [1, 3, 8, 23, 31, 44]

// ---------- Handlers ----------

const find = (id: string) => DB.find((r) => r.id === id)

async function setCredential(id: string, key: string, credential: McpServer["credential"]) {
  await wait(250)
  const r = find(id)
  const server = r && current(r).config.mcp_servers.find((x) => x.key === key)
  if (!r || !server) return notFound()
  server.credential = credential
  server.auth = "token"
  save()
  return HttpResponse.json({ ok: true })
}

export const agentsHandlers = [
  http.get("/api/agents", async () => {
    await wait()
    const v = viewer()
    return v ? HttpResponse.json(DB.map((r) => summary(r, v))) : unauthorized()
  }),
  http.get("/api/agents/catalog", async () => {
    await wait(120)
    return HttpResponse.json(CATALOG)
  }),
  http.get("/api/agents/templates", async () => {
    await wait(120)
    return HttpResponse.json(TEMPLATES.map(({ config: _c, ...t }) => t))
  }),
  http.post("/api/agents", async ({ request }) => {
    await wait(400)
    const v = viewer()
    if (!v?.create) return HttpResponse.json({ detail: "Ask an admin" }, { status: 403 })
    const { name, template, model } = (await request.json()) as { name: string; template: string; model?: string }
    const t = TEMPLATES.find((x) => x.id === template) ?? TEMPLATES[0]!
    const config: AgentConfig = { ...t.config(), name, avatar: face(FACES[DB.length % FACES.length]!) }
    if (model) config.model = m(model, config.model.effort)
    const id = `ag_${name.toLowerCase().replace(/[^a-z0-9]+/g, "_").replace(/^_|_$/g, "")}_${Date.now().toString(36).slice(-4)}`
    DB.push({
      id, owner: v.id, co_owners: [], last_used_at: null, month_usd: 0, drafts: {},
      versions: [{ version: 1, at: new Date().toISOString(), by: v.id, note: t.id === "blank" ? "Created" : `Created from ${t.name}`, config }],
    })
    save()
    return HttpResponse.json(view(DB.at(-1)!, v))
  }),
  http.get("/api/agents/:id", async ({ params }) => {
    await wait()
    const v = viewer(), r = find(String(params.id))
    if (!v) return unauthorized()
    if (!r) return notFound()
    return HttpResponse.json(view(r, v))
  }),
  http.get("/api/agents/:id/draft", async ({ params }) => {
    await wait(120)
    const v = viewer(), r = find(String(params.id))
    if (!v) return unauthorized()
    if (!r) return notFound()
    return HttpResponse.json(draftView(r, v.id))
  }),
  http.patch("/api/agents/:id/draft", async ({ params, request }) => {
    await wait(120)
    const v = viewer(), r = find(String(params.id))
    if (!v) return unauthorized()
    if (!r) return notFound()
    if (!(details(r, v) && v.edit)) return HttpResponse.json({ detail: `Ask ${ref(r.owner).name}` }, { status: 403 })
    const { base_version, config } = (await request.json()) as { base_version: number; config: AgentConfig }
    const base = r.versions.find((x) => x.version === base_version)
    if (!base) return notFound()
    if (!diffConfig(base.config, config).length) delete r.drafts[v.id]
    else r.drafts[v.id] = { base_version, config }
    save()
    return HttpResponse.json(draftView(r, v.id))
  }),
  http.delete("/api/agents/:id/draft", async ({ params }) => {
    const v = viewer(), r = find(String(params.id))
    if (!v) return unauthorized()
    if (r) { delete r.drafts[v.id]; save() }
    return HttpResponse.json(null)
  }),
  http.post("/api/agents/:id/apply", async ({ params, request }) => {
    await wait(500)
    const v = viewer(), r = find(String(params.id))
    if (!v) return unauthorized()
    if (!r) return notFound()
    const d = r.drafts[v.id]
    const { note } = (await request.json()) as { note?: string }
    if (!d) return HttpResponse.json({ detail: "Nothing to apply" }, { status: 409 })
    if (d.base_version !== current(r).version) {
      return HttpResponse.json({ detail: `Changed to v${current(r).version} since your draft began` }, { status: 409 })
    }
    r.versions.push({ version: current(r).version + 1, at: new Date().toISOString(), by: v.id, note: note?.trim() || null, config: d.config })
    delete r.drafts[v.id]
    save()
    return HttpResponse.json(view(r, v))
  }),
  http.get("/api/agents/:id/skills/:skill/files", async ({ params, request }) => {
    await wait(120)
    if (!viewer()) return unauthorized()
    const id = String(params.skill)
    const version = new URL(request.url).searchParams.get("version") ?? String(latestVersion(id))
    const files = FILES.get(`${id}@${version}`)
    if (!files) return notFound()
    return HttpResponse.json({
      skill_id: id, version,
      files: Object.entries(files).sort(([a], [b]) => (a === "SKILL.md" ? -1 : b === "SKILL.md" ? 1 : a.localeCompare(b)))
        .map(([path, content]) => ({ path, size: content.length, content })),
    })
  }),
  http.post("/api/agents/:id/skills", async ({ request }) => {
    await wait(400)
    if (!viewer()) return unauthorized()
    const { skill_id, files, version: held } = (await request.json()) as { skill_id: string | null; files: Record<string, string | null>; version?: string }
    const base = skill_id ? FILES.get(`${skill_id}@${held ?? latestVersion(skill_id)}`) ?? {} : {}
    const next: Record<string, string> = {}
    for (const [path, body] of Object.entries(files)) next[path] = body ?? base[path] ?? ""
    const doc = next["SKILL.md"]
    const name = doc?.match(/^name:\s*(\S+)/m)?.[1]
    const description = doc?.match(/^description:\s*(.+)$/m)?.[1]?.trim()
    if (!name || !description) return HttpResponse.json({ detail: "SKILL.md needs a name and a description." }, { status: 422 })
    const id = skill_id ?? name
    const version = String(latestVersion(id) + 1)
    FILES.set(`${id}@${version}`, next)
    const title = SKILLS.find((x) => x.skill_id === id)?.title ?? name.replace(/-/g, " ").replace(/^./, (c) => c.toUpperCase())
    return HttpResponse.json({ type: "custom", skill_id: id, title, description, version } satisfies Skill)
  }),
  http.put("/api/agents/:id/mcp/:key/token", async ({ params }) => setCredential(String(params.id), String(params.key), "connected")),
  http.delete("/api/agents/:id/mcp/:key/token", async ({ params }) => setCredential(String(params.id), String(params.key), "missing")),
  http.get("/api/agents/:id/versions", async ({ params }) => {
    await wait()
    const v = viewer(), r = find(String(params.id))
    if (!v) return unauthorized()
    if (!r) return notFound()
    const full = details(r, v)
    return HttpResponse.json(versionsView(r).map((x) => full ? x : { ...x, changes: x.changes.map(({ before: _b, after: _a, ...c }) => c) }))
  }),
  http.post("/api/agents/:id/revert", async ({ params, request }) => {
    await wait(400)
    const v = viewer(), r = find(String(params.id))
    if (!v) return unauthorized()
    if (!r) return notFound()
    if (!(details(r, v) && v.edit)) return HttpResponse.json({ detail: `Ask ${ref(r.owner).name}` }, { status: 403 })
    const { version } = (await request.json()) as { version: number }
    const to = r.versions.find((x) => x.version === version)
    if (!to) return notFound()
    r.versions.push({ version: current(r).version + 1, at: new Date().toISOString(), by: v.id, note: `Reverted to v${version}`, config: clone(to.config) })
    save()
    return HttpResponse.json(view(r, v))
  }),
]

/** Dev only: forget edits (`window.__resetAgents()`). */
;(globalThis as { __resetAgents?: () => void }).__resetAgents = () => { DB = seed(); save() }
