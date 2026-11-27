import { Pill } from "../agents/bits"
import type { BlindRun, Case, CaseView, Run, RunStatus, ScrubStatus, SuiteRole } from "./types"

const RUN: Record<RunStatus, { label: string; cls?: string }> = {
  draft: { label: "Draft" }, estimated: { label: "Awaiting operator approval", cls: "text-warning" }, approved: { label: "Queued" }, running: { label: "Running", cls: "text-link" },
  done: { label: "Done", cls: "text-success" }, partial: { label: "Partial", cls: "text-warning" }, cancelled: { label: "Cancelled" }, failed: { label: "Failed", cls: "text-destructive" },
}
export const RunPill = ({ status }: { status: RunStatus }) => <Pill className={RUN[status].cls}>{RUN[status].label}</Pill>

const SCRUB: Record<ScrubStatus, { label: string; cls?: string }> = { scrubbed: { label: "Scrubbed", cls: "text-success" }, partial: { label: "Partly scrubbed", cls: "text-warning" }, unscrubbed: { label: "Unscrubbed", cls: "text-destructive" } }
export const ScrubPill = ({ status }: { status: ScrubStatus }) => <Pill className={SCRUB[status].cls}>{SCRUB[status].label}</Pill>

export const ROLE: Record<SuiteRole, string> = { owner: "Owner", admin: "Admin", editor: "Editor", reviewer: "Reviewer", viewer: "Viewer" }
export const canEdit = (r: SuiteRole) => r === "owner" || r === "admin" || r === "editor"
export const canShare = (r: SuiteRole) => r === "owner" || r === "admin"
export const canReview = (r: SuiteRole) => canEdit(r) || r === "reviewer"

/** A rate as a percentage, "–" when there is none. */
export const pct = (v: number | null | undefined) => (v == null ? "–" : `${Math.round(v * 100)}%`)

/** A reviewer's view of a run names its arms by label only. */
export const isBlindRun = (r: Run | BlindRun): r is BlindRun => typeof r.arms[0] === "string"

/** The full case, not the reviewers' cut of it. */
export const isCase = (c: Case | CaseView): c is Case => "surface" in c
