import { useQuery } from "@tanstack/react-query"

import { api } from "@/lib/api"
import { blobText, E } from "../api"
import type { Output } from "../types"

export const useText = (sha: string | undefined) => useQuery({ queryKey: ["evals", "blob", sha], queryFn: () => blobText(sha!), enabled: !!sha, staleTime: Infinity })
export type Sheet = { name: string; rows: string[][] }
export const useSheets = (o: Output, enabled: boolean) =>
  useQuery({ queryKey: ["evals", "table", o.blob.sha256], queryFn: () => api.get<{ sheets: Sheet[] }>(`${E}/blobs/${o.blob.sha256}/table`).then((r) => r.sheets), enabled, staleTime: Infinity })

const has = (o: Output, ...needles: string[]) => needles.some((n) => o.media_type.includes(n) || o.name.toLowerCase().endsWith(n))
export type Kind = "image" | "pdf" | "slides" | "table" | "json" | "patch" | "text"
/** Which viewer shows an output. */
export function kindOf(o: Output): Kind {
  if (o.media_type.startsWith("image/")) return "image"
  if (has(o, "pdf")) return "pdf"
  if (has(o, "presentation", ".pptx")) return "slides"
  if (has(o, "spreadsheet", "csv", ".xlsx")) return "table"
  if (has(o, "json")) return "json"
  if (has(o, "x-diff", "x-patch", ".patch", ".diff")) return "patch"
  return "text"
}
