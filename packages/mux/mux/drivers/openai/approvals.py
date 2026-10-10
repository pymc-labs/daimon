"""Agents browser-origin decisions, separate from secret-bearing sign-in."""

from mux.drivers.openai._common import text
from mux.drivers.openai.transport import Object, object_json


def origin_request(action: Object) -> Object | None:
    if action.get("type") != "computer_use_approval_request":
        return None
    request = object_json(action.get("request") or {})
    if request.get("type") != "browser_origin_access":
        return None
    text(action["request_id"])
    text(action["turn_id"])
    text(request["origin"])
    if request.get("reason") is not None and not isinstance(request["reason"], str):
        raise ValueError("invalid origin approval reason")
    return request
