"""Small, named changes to a draft spec, so an agent need not resend the whole workflow."""

from __future__ import annotations

import copy
from typing import Annotated, Any, Literal

from daimon.core.workflows.views import obj, seq
from pydantic import BaseModel, ConfigDict, Field


class EditError(ValueError):
    """An edit that cannot be applied; the text says which one and why."""


class _Op(BaseModel):
    model_config = ConfigDict(extra="forbid")


class AddNode(_Op):
    op: Literal["add_node"]
    id: str = Field(description="The new node's id, unique in the workflow.")
    node: dict[str, Any] = Field(description="The node: kind, title and its kind's fields.")


class UpdateNode(_Op):
    op: Literal["update_node"]
    id: str
    fields: dict[str, Any] = Field(description="Fields to set on the node; null removes one.")


class RemoveNode(_Op):
    op: Literal["remove_node"]
    id: str


class Connect(_Op):
    op: Literal["connect"]
    from_node: str
    to: str
    route: str | None = Field(
        default=None, description="The route to set (approval, branch, wait); empty sets `next`."
    )


class SetTrigger(_Op):
    op: Literal["set_trigger"]
    trigger: dict[str, Any] = Field(
        description="A trigger with an id; one with that id is replaced."
    )


class SetInputs(_Op):
    op: Literal["set_inputs"]
    inputs: dict[str, Any] = Field(description="The workflow's inputs schema, replacing the old.")


class SetStart(_Op):
    op: Literal["set_start"]
    id: str


Edit = Annotated[
    AddNode | UpdateNode | RemoveNode | Connect | SetTrigger | SetInputs | SetStart,
    Field(discriminator="op"),
]


def _nodes(spec: dict[str, Any]) -> dict[str, Any]:
    nodes = spec["nodes"] = obj(spec.get("nodes"))
    return nodes


def _need(nodes: dict[str, Any], node_id: str) -> dict[str, Any]:
    if node_id not in nodes:
        raise EditError(f"There is no node `{node_id}`.")
    return obj(nodes[node_id])


def _apply_one(spec: dict[str, Any], edit: Edit) -> None:
    nodes = _nodes(spec)
    match edit:
        case AddNode():
            if edit.id in nodes:
                raise EditError(f"Node `{edit.id}` already exists; use update_node.")
            nodes[edit.id] = edit.node
            spec.setdefault("start", edit.id)
        case UpdateNode():
            node = _need(nodes, edit.id)
            for key, value in edit.fields.items():
                if value is None:
                    node.pop(key, None)
                else:
                    node[key] = value
        case RemoveNode():
            _need(nodes, edit.id)
            del nodes[edit.id]
        case Connect():
            node = _need(nodes, edit.from_node)
            _need(nodes, edit.to)
            if edit.route:
                routes = node["routes"] = obj(node.get("routes"))
                routes[edit.route] = edit.to
            else:
                node["next"] = edit.to
        case SetTrigger():
            trigger_id = edit.trigger.get("id")
            kept = [t for t in seq(spec.get("triggers")) if obj(t).get("id") != trigger_id]
            spec["triggers"] = [*kept, edit.trigger]
        case SetInputs():
            spec["inputs"] = edit.inputs
        case SetStart():
            _need(nodes, edit.id)
            spec["start"] = edit.id


def apply(spec: dict[str, Any], edits: list[Edit]) -> dict[str, Any]:
    """`spec` with every edit applied in order, or an `EditError` naming the one that failed.

    Nothing is changed on failure: the edits run on a copy."""
    changed = copy.deepcopy(spec)
    for number, edit in enumerate(edits, start=1):
        try:
            _apply_one(changed, edit)
        except EditError as err:
            raise EditError(f"Edit {number} ({edit.op}): {err} Nothing was changed.") from err
    return changed
