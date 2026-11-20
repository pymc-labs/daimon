"""What Ask Daimon is told about the workflow a person has open."""

from __future__ import annotations

from pydantic import BaseModel

_NAME_CHARS = 120


class SelectedNode(BaseModel):
    id: str
    kind: str
    title: str


class WorkflowContext(BaseModel):
    """The open workflow as a turn's controls carry it; the model reads the rest with
    `get_workflow`."""

    id: str
    name: str
    revision: int
    live_version: int | None
    status: str
    errors: int
    warnings: int
    selected_node: SelectedNode | None = None


def clip(name: str) -> str:
    return name[:_NAME_CHARS]
