import type { Answer, Sent } from "../types"
import { FileList } from "./parts"

const PLATFORM = { discord: "Discord", slack: "Slack", teams: "Teams" }
type Row = [string, React.ReactNode]

/** Where an ask went: one row per post, with a link to it when the platform gave one. */
export const sentRows = (sent: Sent[]): Row[] => sent.map((x, i) => [
  "Sent",
  <span key={i}>{x.where}{x.link && <> <a href={x.link} target="_blank" rel="noreferrer" className="text-link hover:underline">Open in {PLATFORM[x.platform]}</a></>}</span>,
])

/** What came back: who answered and how, the text and any files. */
export const answerRows = (a: Answer | null): Row[] => !a ? [] : [
  ["Answered by", `${a.by.name} via ${a.via === "web" ? "the web" : PLATFORM[a.via]}`],
  ["Answer", a.text && <span key="t" className="whitespace-pre-wrap">{a.text}</span>],
  ...Object.entries(a.labels ?? {}).map(([k, label]): Row => [label, a.values[k] != null && <span key={k} className="whitespace-pre-wrap">{String(a.values[k])}</span>]),
  ["Files", a.files.length > 0 && <div key="f" className="-my-1"><FileList files={a.files} /></div>],
]
