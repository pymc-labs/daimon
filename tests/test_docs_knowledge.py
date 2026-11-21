"""Docs are also an agent's knowledge: pages, anchors, names and web text must stay true."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from daimon.core.features.registry import FEATURES
from daimon.core.self_knowledge import sections

ROOT = Path(__file__).resolve().parents[1]
DOCS = ROOT / "docs"
GENERATED = {"configuration.md", "mcp-tools.md", "features.md"}  # names come from code
OPEN, CLOSE = "<!-- knowledge: web -->", "<!-- /knowledge -->"
FENCE = re.compile(r"^```.*?^```", re.M | re.S)
CODE_SPAN = re.compile(r"`([^`\n]+)`")


def _anchors(path: Path) -> set[str]:
    return set(sections(path.read_text()))


def _pages() -> list[Path]:
    return sorted(DOCS.glob("*.md"))


def _is_web_page(text: str) -> bool:
    return text.startswith("---\nknowledge: web\n---")


def _marker_lines(text: str) -> list[tuple[int, str]]:
    """(line number, marker) for each marker line outside a code fence."""
    found, fenced = [], False
    for number, line in enumerate(text.splitlines(), 1):
        if line.strip().startswith(("```", "~~~")):
            fenced = not fenced
        elif not fenced and (OPEN in line or CLOSE in line):
            found.append((number, line))
    return found


def _unmarked(text: str) -> str:
    """The text a reader with the web app off sees: web blocks and fenced lines' markers aside."""
    out, hiding, fenced = [], False, False
    for line in text.splitlines():
        if line.strip().startswith(("```", "~~~")):
            fenced = not fenced
        elif not fenced and line.strip() in (OPEN, CLOSE):
            hiding = line.strip() == OPEN
            continue
        if not hiding:
            out.append(line)
    return "\n".join(out)


def test_every_feature_names_a_real_page_and_anchor() -> None:
    bad: list[str] = []
    for spec in FEATURES.values():
        page, _, anchor = spec.page.partition("#")
        path = DOCS / page
        if not path.is_file():
            bad.append(f"{spec.key}: no page {page}")
        elif anchor and anchor not in _anchors(path):
            bad.append(f"{spec.key}: no anchor {spec.page}")
    assert not bad, "\n".join(bad)


@pytest.mark.parametrize("path", _pages(), ids=lambda p: p.name)
def test_markers_are_paired_on_their_own_lines(path: Path) -> None:
    depth = 0
    for number, line in _marker_lines(path.read_text()):
        assert line.strip() in (OPEN, CLOSE), f"{path.name}:{number} marker shares a line"
        depth += 1 if line.strip() == OPEN else -1
        assert depth in (0, 1), f"{path.name}:{number} markers nest or close early"
    assert depth == 0, f"{path.name}: unclosed marker"


WEB_MENTION = re.compile(
    r"\bweb app\b|\bweb chat\b|\bweb model\b|\bweb (?:routines|settings)\b|(?<!search )\bthe web\b"
    r"|web\.md|DAIMON_WEB__|\bweb access\b|\bon the web\b",
    re.I,
)


@pytest.mark.parametrize("path", _pages(), ids=lambda p: p.name)
def test_web_app_is_mentioned_only_inside_markers(path: Path) -> None:
    text = path.read_text()
    if _is_web_page(text):
        pytest.skip("web-only page")
    for line in _unmarked(text).splitlines():
        assert not WEB_MENTION.search(line), f"{path.name}: web text outside markers: {line}"


# --- names in prose still exist in code ---

SRC = ROOT / "packages"
URL_WORDS = {  # slash-led words that are paths, not slash commands
    "api", "mcp", "healthz", "readyz", "oauth", "tmp", "mnt", "data", "var", "v", "proc",
    "organizations", "notes", "invite", "home", "slack", "discord", "workspace", "app",
    "etc", "usr", "bin", "opt", "repo", "skills", "files", "memory", "events", "teams",
    "authorize", "callback", "install", "connect", "login", "web", "docs", "static",
}  # fmt: skip


def _corpus() -> str:
    roots = [ROOT / d for d in ("packages", "apps", "scripts", "defaults")]
    files = [p for r in roots for p in r.rglob("*") if p.suffix in {".py", ".sh", ".yaml"}]
    files += [ROOT / "docker-compose.yml", ROOT / ".env.example"]
    return "\n".join(p.read_text(errors="ignore") for p in files if p.is_file())


def _hand_written() -> list[tuple[Path, str]]:
    out = []
    for path in _pages():
        text = path.read_text()
        if path.name not in GENERATED and not _is_web_page(text):
            out.append((path, text))
    return out


def _code(text: str) -> list[str]:
    return [*FENCE.findall(text), *CODE_SPAN.findall(FENCE.sub("", text))]


def _slash_commands() -> set[str]:
    found: set[str] = set()
    for path in SRC.rglob("*.py"):
        if "/tests/" in str(path) or "adapters/" not in str(path):
            continue
        src = path.read_text()
        found |= set(re.findall(r'app_commands\.command\(\s*name="([\w-]+)"', src))
        found |= set(re.findall(r'"/([a-z][\w-]*)"', src))
    return found


def test_slash_commands_in_docs_exist() -> None:
    known = _slash_commands() | URL_WORDS
    bad = []
    for path, text in _hand_written():
        for span in CODE_SPAN.findall(FENCE.sub("", text)):
            m = re.fullmatch(r"/([a-z][\w-]*)(?: .*)?", span)
            if m and m.group(1) not in known:
                bad.append(f"{path.name}: {span}")
    assert not bad, "\n".join(bad)


def test_env_vars_and_feature_keys_in_docs_exist() -> None:
    config = (DOCS / "configuration.md").read_text()
    tokens = set(re.findall(r"DAIMON_[A-Z0-9_]+", _corpus() + config))
    keys = {k.upper().replace(".", "_") for k in FEATURES}
    bad = []
    for path, text in _hand_written():
        for chunk in _code(text):
            for var in re.findall(r"DAIMON_[A-Z0-9_]+", chunk):
                if var.removeprefix("DAIMON_FEATURES__") not in (var, ""):
                    ok = var.removeprefix("DAIMON_FEATURES__") in keys
                else:  # an exact name, a trailing-underscore prefix, or a name built from one
                    ok = (
                        var in tokens
                        or (var.endswith("_") and any(t.startswith(var) for t in tokens))
                        or any(
                            t.endswith("_") and not t.endswith("__") and var.startswith(t)
                            for t in tokens
                        )
                    )
                if not ok:
                    bad.append(f"{path.name}: {var}")
            for key in re.findall(r"daimon features set ([a-z][\w.]*)", chunk):
                if key not in FEATURES:
                    bad.append(f"{path.name}: feature {key}")
    assert not bad, "\n".join(bad)


def _cli_commands() -> dict[str, set[str]]:
    import typer.main
    from daimon.adapters.cli.main import app

    root = typer.main.get_command(app)
    return {
        name: set(getattr(cmd, "commands", {}))
        for name, cmd in root.commands.items()  # pyright: ignore[reportAttributeAccessIssue]
    }


def test_cli_commands_in_docs_exist() -> None:
    cli = _cli_commands()
    bad = []
    for path, text in _hand_written():
        for chunk in _code(text):
            for line in chunk.splitlines():
                for m in re.finditer(r"(?:^|[\s`$])daimon ([a-z][\w-]*)(?: ([a-z][\w-]*))?", line):
                    first, second = m.groups()
                    if first not in cli:
                        bad.append(f"{path.name}: daimon {first}")
                    elif cli[first] and second and second not in cli[first]:
                        bad.append(f"{path.name}: daimon {first} {second}")
    assert not bad, "\n".join(bad)


def test_mcp_tool_names_in_docs_exist() -> None:
    catalogue = (DOCS / "mcp-tools.md").read_text()
    tools = set(re.findall(r"^\| `([a-z][a-z0-9_]+)` \|", catalogue, re.M))
    verbs = {t.partition("_")[0] for t in tools}
    corpus = _corpus()
    others = {k.replace(".", "_") for k in FEATURES}
    bad = []
    for path, text in _hand_written():
        for span in CODE_SPAN.findall(FENCE.sub("", text)):
            looks_like_tool = re.fullmatch(r"[a-z]+(?:_[a-z0-9]+)+", span) and (
                span.partition("_")[0] in verbs
            )
            if (
                looks_like_tool
                and span not in tools | others
                and not re.search(rf"\b{span}\b", corpus)
            ):
                bad.append(f"{path.name}: {span}")
    assert not bad, "\n".join(bad)
