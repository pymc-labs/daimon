import { ChevronLeft, ChevronRight } from "lucide-react"
import { useState } from "react"
import { Document, Page, pdfjs } from "react-pdf"
import "react-pdf/dist/Page/AnnotationLayer.css"
import "react-pdf/dist/Page/TextLayer.css"

import { Rows } from "@/components/QueryState"
import { Button } from "@/components/ui/button"
import { blobUrl } from "../api"
import type { Output, Pin } from "../types"
import { useText } from "./blob"
import { FileDiff } from "./FileDiff"

pdfjs.GlobalWorkerOptions.workerSrc = new URL("pdfjs-dist/build/pdf.worker.min.mjs", import.meta.url).toString()

/** Pages with a text layer. Two documents share one page counter; "Text" diffs what was extracted from them. */
export function Pdf({ o, other, pin }: { o: Output; other?: Output; pin?: Pin }) {
  const [page, setPage] = useState(1)
  const [pages, setPages] = useState(1)
  const [text, setText] = useState(false)
  const a = useText(text ? o.text?.sha256 : undefined), b = useText(text ? other?.text?.sha256 : undefined)
  const docs = [o, ...(other ? [other] : [])]
  return (
    <div className="space-y-2">
      <div className="flex items-center gap-1">
        <Button size="icon-xs" variant="ghost" aria-label="Previous page" disabled={page <= 1} onClick={() => setPage(page - 1)}><ChevronLeft /></Button>
        <span className="text-xs tabular text-muted-foreground">{page} / {pages}</span>
        <Button size="icon-xs" variant="ghost" aria-label="Next page" disabled={page >= pages} onClick={() => setPage(page + 1)}><ChevronRight /></Button>
        {other && o.text && other.text && <Button size="xs" variant={text ? "secondary" : "ghost"} className="ml-2" onClick={() => setText(!text)}>Text diff</Button>}
        {pin && <Button size="xs" variant="outline" className="ml-auto" onClick={() => pin({ target: "page", ref: o.id, page })}>Comment on page {page}</Button>}
      </div>
      {text
        ? (a.data === undefined || b.data === undefined ? <Rows n={3} /> : <FileDiff before={a.data} after={b.data} name={o.name} />)
        : (
          <div className={docs.length > 1 ? "grid grid-cols-2 gap-2" : ""}>
            {docs.map((d, i) => (
              <div key={d.id} className="overflow-hidden rounded-md border">
                <Document file={blobUrl(d.blob.sha256)} loading={<Rows n={2} className="p-2" />} onLoadSuccess={i === 0 ? (p) => { setPages(p.numPages); setPage((n) => Math.min(n, p.numPages)) } : undefined}>
                  <Page pageNumber={page} width={docs.length > 1 ? 280 : 520} />
                </Document>
              </div>
            ))}
          </div>
        )}
    </div>
  )
}
