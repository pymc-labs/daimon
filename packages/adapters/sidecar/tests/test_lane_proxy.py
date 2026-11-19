"""The lane proxy must let a callback link post through, up to the configured body cap."""

import re
from pathlib import Path

import yaml

LANES = Path(__file__).parents[4] / "deploy/lanes"
CADDYFILE = (LANES / "Caddyfile.sidecar").read_text()
WATCH = "0b9f3a6e-1c2d-4e5f-8a7b-9c0d1e2f3a4b"


def test_proxy_routes_token_posts_with_the_configured_body_cap() -> None:
    signed, token = re.findall(r"path_regexp (\S+)", CADDYFILE)
    link = f"/callbacks/watches/{WATCH}/" + "ab" * 16
    assert re.match(token, link) and not re.match(signed, link)
    assert re.match(signed, f"/callbacks/watches/{WATCH}")
    assert "max_size {$DAIMON_WATCHES__CALLBACK_MAX_BODY_BYTES:4096}" in CADDYFILE


def test_lane_compose_hands_the_body_cap_to_the_sidecar_and_the_proxy() -> None:
    services = yaml.safe_load((LANES / "compose.lane.yml").read_text())["services"]
    for name in ("sidecar", "sidecar-proxy"):
        assert "DAIMON_WATCHES__CALLBACK_MAX_BODY_BYTES" in services[name]["environment"], name
