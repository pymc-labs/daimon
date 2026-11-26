"""The `daimon-mcp` tools each capability feature offers; off, a session is not offered them."""

from __future__ import annotations

from typing import Final

from daimon.core.data_tools import DATA_READS, DATA_TOOLS, PERSONAL_DRIVE, PERSONAL_DRIVE_TOOLS

CAPABILITY_TOOLS: Final[dict[str, frozenset[str]]] = {
    "watches": frozenset(
        {
            "create_watch",
            "preview_watch_edit",
            "update_watch",
            "list_watches",
            "show_watch",
            "pause_watch",
            "resume_watch",
            "rotate_watch_key",
            "delete_watch",
            "request_watch_credential",
            "forget_watch_credential",
        }
    ),
    "routines": frozenset(
        {
            "create_routine",
            "list_routines",
            "get_routine",
            "update_routine",
            "delete_routine",
            "set_routine_sandbox_tools",
            "run_routine",
            "report_routine_problem",
        }
    ),
    "long_jobs": frozenset({"start_long_job", "get_long_job", "list_long_jobs", "cancel_long_job"}),
    "compute": frozenset(
        {
            "compute_launch_session",
            "compute_extend_session",
            "compute_stop_session",
            "compute_run_bash",
            "compute_write_file",
            "compute_read_file",
            "compute_list_files",
            "compute_list_sessions",
            "compute_get_session_status",
            "compute_task_status",
            "compute_task_result",
            "compute_list_templates",
        }
    ),
    "notebooks": frozenset(
        {
            "create_notebook_upload_url",
            "create_attachment_upload_url",
            "delete_notebook",
            "list_notebooks",
        }
    ),
    "report_publish": frozenset({"publish_report", "delete_report"}),
    "secret_sharing": frozenset({"share_secret_with"}),
    "media": frozenset({"fetch_youtube_transcript"}),
    "microsoft_365": frozenset(
        {
            "connect_microsoft",
            "microsoft_search",
            "microsoft_get",
            "microsoft_write",
            "microsoft_send",
        }
    ),
    "github": frozenset(
        {
            "github_connect",
            "post_github_app_install_link",
            "remove_repo",
            "request_github_access",
            "set_working_repo",
        }
    ),
    "lead_intake": frozenset(
        {
            "lead_intake_submit",
            "lead_intake_resume",
            "lead_intake_read_source",
            "lead_intake_resolve",
            "lead_intake_get",
            "lead_intake_list",
            "lead_intake_health",
        }
    ),
    DATA_READS: DATA_TOOLS,
    PERSONAL_DRIVE: PERSONAL_DRIVE_TOOLS,
}
"""Feature key to the tools it switches. Every one binds to the session, so a tier's Off takes
the tools from the next message on; a test keeps these names equal to what the server registers."""

CAPABILITY_KEYS: Final[tuple[str, ...]] = tuple(CAPABILITY_TOOLS)
