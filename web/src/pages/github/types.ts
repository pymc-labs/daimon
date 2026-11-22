// The GitHub repo connect flow's views, mirroring `daimon.core.github_connect_views`.

export type Permission = "admin" | "maintain" | "write" | "triage" | "read"
export type Platform = "web" | "discord" | "slack" | "teams"
/** Where to go once done, added by the server to the views that end a flow; `url` is empty for Slack and Teams. */
export type Back = { url: string; platform: Platform } | null

export type ConnectRepo = {
  id: number; owner_id: number; installation_id: number; full_name: string
  /** The person's GitHub permission; null when none the flow can use, which hides the repo. */
  permission: Permission | null; private: boolean; archived: boolean
}
export type ConnectInstallation = {
  id: number; owner_id: number; owner_login: string; repository_selection: string; repos: ConnectRepo[]
  owner_type: string; avatar_url: string | null; state: "active" | "suspended"
  total: number; truncated: boolean; fetch_state: "ok" | "error"
}
export type AgentRepo = {
  repo_id: number; full_name: string; scope: "agent" | "shared"
  max_access: "read" | "write"; baseline_access: "none" | "read" | "write"; ceiling_access: "read" | "write"
  status: "active" | "suspended" | "revoked"
  added_by_account_id: string | null; added_at: string; granted_by_account_id: string | null; granted_at: string
  is_working_repo: boolean; grant_version: number; authorization_version: number; installation_id: number | null
}

/** One save, as the editor or picker posts it; the strings come back verbatim from the view. */
export type Selection = {
  state: string; invitation: string; receipt: string
  snapshot: string; snapshot_sig: string; visibility: string; visibility_sig: string
  repo: string[]; remove: string[]; access: string[]; working: string[]
  repo_access: Record<string, string[]>
}

export type ErrorView = { kind: "error"; message: string; status: number; retry_url: string | null; action: string | null }
export type TransientErrorView = {
  kind: "transient_error"; message: string; status: number; retry_url: string | null; retry_selection: Selection | null
}
export type CancelledView = { kind: "cancelled"; back: Back }
export type ExpiredView = { kind: "expired"; requester_label: string }
export type AlreadyConnectedView = { kind: "already_connected"; count: number | null; agent_name: string | null; back: Back }
export type InstallView = { kind: "install"; install_url: string; cancel_url: string }
export type PendingView = { kind: "pending"; check_url: string; cancel_url: string; organization: string | null }
export type NoReposView = { kind: "no_repos"; share_link: string; install_url: string }
export type AgentEditorView = {
  kind: "agent_editor"; agent_name: string; workspace_label: string; platform: string
  confirm_url: string; check_url: string; cancel_url: string; install_url: string
  state: string; invitation_hash: string; receipt: string
  snapshot: string; snapshot_sig: string; visibility: string; visibility_sig: string
  installations: ConnectInstallation[]; grants: AgentRepo[]
  /** The usable repos the person sees on GitHub; a grant outside them is hidden and can only be removed. */
  visible_repos: ConnectRepo[]
  /** Usable repos the agent does not have yet. */
  available: ConnectRepo[]
  repairable_repo_ids: number[]
  /** Repo id to whether it needs write to finish setup. */
  needs_write: Record<string, boolean>
  requested_repo_id: number | null; working_repo_id: number | null; past_cap_repo_ids: number[]
  owner_order: string[]; owner_limit: number; github_user_id: number | null
  pending_installation: { found: boolean; account_login: string | null } | null
  selection_error: string | null
}
export type WorkspacePickerView = {
  kind: "workspace_picker"; agent_name: string | null; workspace_label: string; platform: string
  confirm_url: string; cancel_url: string; install_url: string
  state: string; invitation_hash: string; receipt: string
  installations: ConnectInstallation[]; already_added_repo_ids: number[]
  needs_write: Record<string, boolean>; requested_repo_id: number | null; selection_error: string | null
}
export type SavedAgentView = {
  kind: "saved_agent"; agent_name: string; grants: AgentRepo[]; visible_repo_ids: number[]
  installations: ConnectInstallation[]; back: Back
}
export type DoneView = { kind: "done"; count: number; requester_label: string; same_person: boolean; agent_name: string | null; back: Back }

export type ConnectView =
  | ErrorView | TransientErrorView | CancelledView | ExpiredView | AlreadyConnectedView | InstallView | PendingView
  | NoReposView | AgentEditorView | WorkspacePickerView | SavedAgentView | DoneView

/** What a step answers: go to GitHub (or the next step), or show a page. The status is informational. */
export type Outcome = { kind: "redirect"; url: string } | { kind: "page"; view: ConnectView }

/** Runs one flow step (cancel, check again, save) and shows its outcome in place of the page. */
export type Step = { run: (fn: () => Promise<Outcome>) => void; pending: boolean; error: string | null }
