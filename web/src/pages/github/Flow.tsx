import { useMutation, useQuery } from "@tanstack/react-query"
import { useEffect, useState } from "react"
import { useParams, useSearchParams } from "react-router"

import { connectApi } from "./api"
import { Editor } from "./Editor"
import { Picker } from "./Picker"
import { AlreadyConnectedPage, CancelledPage, DonePage, ErrorPage, ExpiredPage, InstallPage, NoReposPage, PendingPage, SavedAgentPage, TransientErrorPage } from "./Status"
import type { ConnectView, Outcome, Step } from "./types"

function View({ view, step, onRetry }: { view: ConnectView; step: Step; onRetry: () => void }) {
  switch (view.kind) {
    case "error": return <ErrorPage view={view} />
    case "transient_error": return <TransientErrorPage view={view} step={step} onRetry={onRetry} />
    case "cancelled": return <CancelledPage view={view} />
    case "expired": return <ExpiredPage view={view} />
    case "already_connected": return <AlreadyConnectedPage view={view} />
    case "install": return <InstallPage view={view} step={step} />
    case "pending": return <PendingPage view={view} step={step} />
    case "no_repos": return <NoReposPage view={view} />
    case "agent_editor": return <Editor key={view.snapshot} view={view} step={step} />
    case "workspace_picker": return <Picker view={view} step={step} />
    case "saved_agent": return <SavedAgentPage view={view} />
    case "done": return <DonePage view={view} />
  }
}

/** One step of the connect flow: run it once, then follow its redirect or show its page. */
function Flow({ id, load }: { id: string; load: () => Promise<Outcome> }) {
  const q = useQuery({ queryKey: ["github-connect", id], queryFn: load, staleTime: Infinity, retry: false })
  const [next, setNext] = useState<Outcome>()
  const mutation = useMutation({ mutationFn: (fn: () => Promise<Outcome>) => fn(), onSuccess: setNext, meta: { inline: true } })
  const step: Step = { run: mutation.mutate, pending: mutation.isPending, error: mutation.error?.message ?? null }
  const outcome = next ?? q.data
  useEffect(() => { if (outcome?.kind === "redirect") location.assign(outcome.url) }, [outcome])
  const retry = () => { setNext(undefined); void q.refetch() }
  if (q.error && !outcome) return <View view={{ kind: "transient_error", message: "Couldn't reach Daimon", status: 0, retry_url: null, retry_selection: null }} step={step} onRetry={retry} />
  if (outcome?.kind !== "page") return <div className="min-h-dvh bg-muted" role="status" aria-label="Loading" />
  return <View view={outcome.view} step={step} onRetry={retry} />
}

export const GithubConnect = () => {
  const { token = "" } = useParams()
  return <Flow id={`start:${token}`} load={() => connectApi.start(token)} />
}

export function GithubCallback() {
  const [q] = useSearchParams()
  const state = q.get("state") ?? "", code = q.get("code") ?? ""
  return <Flow id={`callback:${state}:${code}`} load={() => connectApi.callback(state, code)} />
}

/** GitHub can drop `state` from this redirect; the server then reads the one the browser kept. */
export function GithubSetup() {
  const state = useSearchParams()[0].get("state") ?? ""
  return <Flow id={`setup:${state}`} load={() => connectApi.setup(state)} />
}

export function GithubConfirm() {
  const [q] = useSearchParams()
  const state = q.get("state") ?? "", cancel = q.get("cancel") === "1"
  return <Flow id={`${cancel ? "cancel" : "view"}:${state}`} load={() => (cancel ? connectApi.cancel(state) : connectApi.view(state))} />
}
