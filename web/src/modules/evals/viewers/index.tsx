import { lazy, Suspense } from "react"

import { Rows } from "@/components/QueryState"
import type { Output, Pin } from "../types"
import { useText } from "./blob"
import { kindOf } from "./blob"
import { FileDiff } from "./FileDiff"
import { ImageView } from "./Image"
import { Json } from "./Json"
import { Slides } from "./Slides"
import { Table } from "./Table"
import { TableDiff } from "./TableDiff"
import { Text } from "./Text"

// pdf.js is the heaviest piece of the module: loaded when a PDF is first opened.
const Pdf = lazy(() => import("./Pdf").then((m) => ({ default: m.Pdf })))

function TextDiff({ a, b }: { a: Output; b: Output }) {
  const x = useText(a.blob.sha256), y = useText(b.blob.sha256)
  return x.data === undefined || y.data === undefined ? <Rows n={3} /> : <FileDiff before={x.data} after={y.data} name={a.name} />
}

/** Shows an output in the viewer for its kind; with `other`, the difference between the two. */
export function Artifact({ o, other, pin }: { o: Output; other?: Output; pin?: Pin }) {
  const kind = kindOf(o)
  const body = (() => {
    switch (kind) {
      case "image": return <ImageView a={o.blob} b={other?.blob} pin={pin} />
      case "pdf": return <Suspense fallback={<Rows n={3} />}><Pdf o={o} other={other} pin={pin} /></Suspense>
      case "slides": return <Slides o={o} other={other} pin={pin} />
      case "table": return other ? <TableDiff a={o} b={other} /> : <Table o={o} pin={pin} />
      case "json": return <Json o={o} other={other} />
      case "patch": return <PatchView o={o} />
      default: return other ? <TextDiff a={o} b={other} /> : <Text o={o} pin={pin} />
    }
  })()
  return <div data-viewer={kind}>{body}</div>
}

function PatchView({ o }: { o: Output }) {
  const t = useText(o.blob.sha256)
  return t.data === undefined ? <Rows n={3} /> : <FileDiff patch={t.data} />
}

export { FileDiff }
