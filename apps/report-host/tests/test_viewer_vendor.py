"""The viewer must serve its own pdf.js, unmodified, with its licence."""

from __future__ import annotations

import hashlib
from pathlib import Path

import report_host

_VIEWER = Path(report_host.__file__).parent / "viewer"
_VENDOR = _VIEWER / "vendor"
_SHA256 = {
    "pdf.min.mjs": "27fc2a057a00f92a4334ad06e17dbd7259912954e9fb7f76400bcca5fd190a9c",
    "pdf.worker.min.mjs": "1baa1844c89c80a5b2797c916e75ab29254be46d8e9cb53cb6364d7aad84be36",
}


def test_vendored_pdfjs_matches_the_pinned_checksums() -> None:
    for name, expected in _SHA256.items():
        actual = hashlib.sha256((_VENDOR / name).read_bytes()).hexdigest()
        assert actual == expected, f"{name} differs from the pinned pdf.js build"


def test_vendored_pdfjs_ships_its_licence() -> None:
    licence = (_VENDOR / "pdfjs-LICENSE.txt").read_text()
    assert "Apache License" in licence, "the Apache-2.0 text must travel with pdf.js"


def test_viewer_pages_load_no_third_party_script() -> None:
    for page in (_VIEWER / "index.html", _VIEWER / "pdfjs-init.js", _VIEWER / "app.js"):
        text = page.read_text()
        assert "http://" not in text and "https://" not in text, f"{page.name} names a remote URL"
