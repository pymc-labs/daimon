"""Every posting tool goes through the outbound gate, and every raw send says whom it may ping."""

import ast
from pathlib import Path

_SRC = Path(__file__).resolve().parents[1] / "daimon" / "adapters" / "mcp"
_GATES = {"prepare_posts", "render_edit"}
_POSTING = ("send_", "post_", "create_thread", "edit_message", "reply")
# Tools whose posts carry only fixed text Daimon wrote, never the model's words.
_FIXED_TEXT_TOOLS = {"post_github_app_install_link"}
_RAW_SLACK_TEAMS = {"chat_postMessage", "chat_update", "chat_postEphemeral", "files_upload_v2"}
_RAW_SLACK_TEAMS |= {"send_activity"}
# Each module that calls a Slack or Teams send method itself, and why its text is safe.
_REVIEWED_RAW_SENDS = {
    "oauth_slack.py": "fixed connect result, to the person who connected",
    "tools/github_connect.py": "fixed connect button, ephemeral",
    "tools/github_request_delivery.py": "fixed GitHub request card",
    "tools/slack/_app_install_button.py": "fixed install card",
    "tools/slack/_credential_button.py": "fixed credential card",
    "tools/slack/_send.py": "transport; its tools call prepare_posts first",
    "tools/slack/_tidy.py": "transport; edit_message calls render_edit first",
    "tools/teams/_files.py": "a file card; Teams text carries no mention markup",
}

_Fn = ast.FunctionDef | ast.AsyncFunctionDef


def _modules() -> dict[str, ast.Module]:
    return {str(p.relative_to(_SRC)): ast.parse(p.read_text()) for p in sorted(_SRC.rglob("*.py"))}


def _callee(call: ast.Call) -> str | None:
    func = call.func
    return func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)


def _calls(node: ast.AST) -> list[ast.Call]:
    return [n for n in ast.walk(node) if isinstance(n, ast.Call)]


def _is_tool(fn: _Fn) -> bool:
    return any(
        isinstance(d, ast.Call) and _callee(d) == "tool" and "mcp" in ast.unparse(d.func)
        for d in fn.decorator_list
    )


def _reaches_gate(fn: _Fn, defs: dict[str, _Fn], seen: set[str]) -> bool:
    called = {name for c in _calls(fn) if (name := _callee(c))}
    if called & _GATES:
        return True
    seen.add(fn.name)
    return any(_reaches_gate(defs[n], defs, seen) for n in (called & defs.keys()) - seen)


def test_every_posting_tool_renders_through_the_outbound_gate() -> None:
    posting: dict[str, bool] = {}
    for tree in _modules().values():
        defs = {n.name: n for n in ast.walk(tree) if isinstance(n, _Fn)}
        for fn in defs.values():
            if _is_tool(fn) and fn.name.startswith(_POSTING):
                posting[fn.name] = _reaches_gate(fn, defs, set())
    assert {"send_message", "create_thread", "edit_message", "post_wizard"} <= posting.keys(), (
        f"the scan finds the known posting tools: {sorted(posting)}"
    )
    ungated = sorted(n for n, ok in posting.items() if not ok and n not in _FIXED_TEXT_TOOLS)
    assert not ungated, f"posting tools must call prepare_posts or render_edit: {ungated}"


def test_every_discord_client_and_mention_policy_never_pings_everyone_or_roles() -> None:
    bad: list[str] = []
    for path, tree in _modules().items():
        for call in _calls(tree):
            text = ast.unparse(call.func)
            kwargs = {k.arg: k.value for k in call.keywords}
            if text == "discord.Client" and "allowed_mentions" not in kwargs:
                bad.append(f"{path}:{call.lineno} Client without allowed_mentions")
            if text == "discord.AllowedMentions" and not all(
                isinstance(v := kwargs.get(k), ast.Constant) and v.value is False
                for k in ("everyone", "roles")
            ):
                bad.append(f"{path}:{call.lineno} AllowedMentions may ping everyone or a role")
    assert not bad, f"Discord sends must say whom they may ping: {bad}"


def test_raw_slack_and_teams_sends_live_only_in_reviewed_modules() -> None:
    raw = {
        path
        for path, tree in _modules().items()
        if any(_callee(c) in _RAW_SLACK_TEAMS for c in _calls(tree))
    }
    assert raw == _REVIEWED_RAW_SENDS.keys(), (
        f"review new raw sends (and drop stale entries): {sorted(raw ^ _REVIEWED_RAW_SENDS.keys())}"
    )
