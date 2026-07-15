from daimon.adapters.slack.attachments import (
    ProxyUrlContext,
    build_attachment_url_prefix,
    build_proxy_url,
)
from daimon.adapters.slack.vision import SlackFile
from daimon.core.slack_file_token import verify_file_token

FILE: SlackFile = {
    "id": "F1",
    "mimetype": "text/csv",
    "name": "data.csv",
    "size": 2048,
    "url_private": "https://files.slack.com/f/F1",
    "url_private_download": "https://files.slack.com/f/F1/download",
}
CTX = ProxyUrlContext(public_url="https://mcp.example.com", secret="s", team_id="T1", now=1000)


def test_build_proxy_url_embeds_a_verifiable_token_under_public_url():
    url = build_proxy_url(FILE, CTX)
    assert url.startswith("https://mcp.example.com/slack/file/"), "URL points at the proxy route"
    token = url.rsplit("/", 1)[1]
    ref = verify_file_token(token, secret="s", now=1000)
    assert ref is not None and ref.file_id == "F1" and ref.team_id == "T1", (
        "embedded token verifies to the file's identity"
    )


def test_build_proxy_url_strips_trailing_slash_on_public_url():
    ctx = ProxyUrlContext(public_url="https://mcp.example.com/", secret="s", team_id="T1", now=1000)
    url = build_proxy_url(FILE, ctx)
    assert "//slack/file" not in url, "no double slash when public_url has a trailing slash"


def test_build_attachment_url_prefix_empty_when_no_files():
    assert build_attachment_url_prefix([], CTX) == "", "no line for an empty file list"


def test_build_attachment_url_prefix_one_line_per_file_with_name_and_url():
    line = build_attachment_url_prefix([FILE], CTX)
    assert "data.csv" in line and "/slack/file/" in line, "line names the file and links the proxy"
    assert line.startswith("*system:"), "surfaced as a system line"
